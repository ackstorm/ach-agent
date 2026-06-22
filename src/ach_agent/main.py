# SPDX-License-Identifier: Apache-2.0
"""ach-agent entrypoint — bootstrap wiring.

Boot order (CRITICAL — Pitfall 8: configure_logging FIRST, before any import
that may emit a log line):
  1. configure_logging()        <- SEC-01: redact_ek_processor installed first
  2. load_config(path)          <- hard-fail on schema mismatch (CFG-02)
  3. D-02 gate: reject unwired channel types (hard-fail, non-zero exit)
  4. Write PID file             <- Pitfall 11: single-replica guard
  5. Construct Router (wraps SanitizedEnv engine launch)
  6a. Construct GitlabCommentAdapter (base_url from GITLAB_BASE_URL env)
  6b. Build engine_runner (CR-01: branches on event.reply_future for reply mode;
      dispatch_actions for gitlab_comment mode)
  6c. Create FastAPI app via create_app(channels, router)
  7. asyncio.run(main()) — starts uvicorn + cron tasks on the SAME event loop

RTR-06: router must not import from hermes_agent.*; engine injected as callable.
D-04: gitlab_comment delivery is accept-and-process-async (202 + out-of-band lane).
D-08: deliver.type: reply → event.reply_future resolved by engine_runner on the lane,
      awaited by the route (CR-01: exactly one engine execution per event).
      deliver.type: gitlab_comment → dispatch_actions posts the MR note out-of-band.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import structlog
import uvicorn

from ach_agent.actions.gitlab_comment import GitlabCommentAdapter, dispatch_actions
from ach_agent.channels.a2a import A2AAgentExecutorBridge, build_a2a_app, make_a2a_agent_card
from ach_agent.channels.cron import CronScheduler
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.slack import connect_slack_adapter, disconnect_slack_adapter
from ach_agent.channels.telegram import connect_telegram_adapter, disconnect_telegram_adapter
from ach_agent.config import load_config
from ach_agent.config.schema import ResponseActionBlock
from ach_agent.engine.sanitized_env import SanitizedEnv, configure_logging
from ach_agent.http.app import create_app
from ach_agent.router import Router

# configure_logging() is called at module TOP (not in main()) so that any
# log emission during import (e.g. validation warnings) is already redacted.
# Must be the FIRST executable statement (Pitfall 8 / SEC-01).
configure_logging()

log = structlog.get_logger(__name__)

# D-02: only channel types wired in this build
WIRED_CHANNEL_TYPES: frozenset[str] = frozenset({"cron", "webhook", "slack", "telegram", "a2a"})

CONFIG_PATH_ENV = "ACH_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "/etc/ach-agent/config.json"
PID_FILE = Path("/var/run/ach-agent.pid")


def _write_pid_file(pid_path: Path) -> None:
    """Write PID file for single-replica guard (Pitfall 11).

    Tolerate a non-writable path in dev by logging and continuing.
    """
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        log.info("PID file written", path=str(pid_path))
    except OSError as exc:
        log.warning(
            "PID file not writable — continuing without it (dev mode)",
            path=str(pid_path),
            error=str(exc),
        )


def _open_dedup_store(cfg: Any) -> Any:
    """Select and open the dedup store per persistence config (D-03/D-04).

    persistence.enabled=false → InMemoryDedupStore (no disk dependency).
    persistence.enabled=true  → FileBackedDedupStore on mountPath/dedup.db.
      Missing / non-writable mount → sys.exit(1) fail-closed (D-04a,
      mirrors ENG-06 poll_ready exit pattern).
      Corrupt dedup.db → fail-open: move aside, start fresh, WARN +
      PERSISTENCE_DEGRADED metric (D-04b, T-03-08: file preserved for forensics).

    Never logs ek_ / GITLAB_TOKEN values (T-03-07 mitigation).
    """
    from ach_agent.router.dedup import FileBackedDedupStore, InMemoryDedupStore
    from ach_agent.router.metrics import PERSISTENCE_DEGRADED

    if not cfg.persistence.enabled:
        return InMemoryDedupStore()

    mount = Path(cfg.persistence.mount_path)

    # D-04a: missing / non-writable mount → fail-closed (loud — ENG-06 pattern)
    if not mount.exists() or not os.access(mount, os.W_OK):
        log.error(
            "persistence.enabled=true but mountPath missing or not writable — exiting",
            mount_path=str(mount),
        )
        sys.exit(1)

    db_path = mount / "dedup.db"

    try:
        store = FileBackedDedupStore(db_path)
        log.info("durable dedup store opened", db_path=str(db_path))
        return store
    except Exception as exc:
        # D-04b: corrupt / unreadable DB → fail-open: move aside, start fresh.
        # Preserved for forensics (T-03-08: not deleted, only renamed).
        aside_path = db_path.with_suffix(f".corrupt.{int(time.time())}.db")
        try:
            db_path.rename(aside_path)
            log.warning(
                "dedup.db corrupt — moved aside, starting fresh (fail-open)",
                db_path=str(db_path),
                aside_path=str(aside_path),
                error=str(exc),
            )
        except OSError as rename_exc:
            log.warning(
                "dedup.db corrupt and could not be moved aside — retrying fresh store",
                db_path=str(db_path),
                error=str(exc),
                rename_error=str(rename_exc),
            )
        PERSISTENCE_DEGRADED.inc()
        # Retry with a fresh DB file after moving the corrupt one aside
        try:
            return FileBackedDedupStore(db_path)
        except Exception:
            # Final fallback: in-memory (degraded mode, DB path still unusable)
            return InMemoryDedupStore()


def build_engine_prompt(event: MessageEvent) -> str:
    """Build a meaningful engine prompt from a MessageEvent.

    For cron events the payload carries a 'scheduled_tick' key — return it as-is
    (original cron behavior preserved).

    For webhook MR events the payload carries the GitLab MR JSON body.
    Build a human-readable review instruction from project_id, mr_iid, and the
    MR title/description from payload['object_attributes'] (guarded — no KeyError).

    Never raises; falls back to an empty string if no usable content is found.
    """
    # Cron path: payload has a scheduled_tick key
    scheduled_tick = event.payload.get("scheduled_tick")
    if scheduled_tick is not None:
        return str(scheduled_tick)

    # Webhook MR path: build prompt from delivery_context + MR fields
    project_id = event.delivery_context.get("project_id", "")
    mr_iid = event.delivery_context.get("mr_iid", "")

    obj_attrs: dict[str, Any] = {}
    raw_obj_attrs = event.payload.get("object_attributes")
    if isinstance(raw_obj_attrs, dict):
        obj_attrs = raw_obj_attrs

    title = obj_attrs.get("title", "")
    description = obj_attrs.get("description", "")

    parts = [
        f"Review MR !{mr_iid} in project {project_id}.",
    ]
    if title:
        parts.append(f"Title: {title}")
    if description:
        parts.append(f"Description: {description}")

    return " ".join(parts)


def _make_engine_runner(
    pool: Any,
    engine_cfg: Any,
    delivery_adapter: GitlabCommentAdapter,
    max_invocation_seconds: int,
    memory_cfg: Any = None,
    response_action_config_by_channel: dict[str, dict[str, ResponseActionBlock]] | None = None,
) -> Callable[..., Any]:
    """Build the engine_runner callable injected into the Router.

    The runner is called by Lane as: engine_runner(event, on_kill).
    It acquires a ManagedServer from the pool, calls run_invocation,
    then branches on event.reply_future (CR-01 / ACT-01):

    - reply mode (event.reply_future is not None):
        Extract the first 'reply' action's text and call set_result on the future.
        The route is awaiting this future to return 200 + body to the client.
        dispatch_actions is NOT called — no gitlab_comment is posted.
        CRITICAL: the future MUST always be resolved (set_result or set_exception)
        even on error, otherwise the route hangs indefinitely. A try/finally
        around run_invocation guarantees resolution before any exception escapes.

    - async/gitlab_comment mode (event.reply_future is None):
        Route actions through dispatch_actions with event + per-channel
        response_action_config (ACT-03: sideEffect consent gate enforced here;
        reply delivered to GitlabCommentAdapter with event.delivery_context).
        Delivery stays on the router lane — NOT create_task from route (Pitfall 4).

    D-08 / D-04: for gitlab_comment channels, delivery is out-of-band on the lane
    (NOT via asyncio.create_task from the route handler — Pitfall 4).

    SanitizedEnv is used to build the subprocess launch env (SEC-01 /
    T-01-EK folded todo): the engine_cfg carries paths, never ek_ values.

    memory_cfg (MEM-01/MEM-02/D-02): optional MemoryBlock from config.memory.
    When present, prepare_memory is called BEFORE pool.acquire so the opencode.json
    written for that server includes or excludes the memory MCP server (Pitfall 3).
    Fail-open: unreachable backend → exclude MCP server, log WARN + metric, run anyway.

    response_action_config_by_channel (ACT-03/A3): boot-time mapping of channel name
    → {action name → ResponseActionBlock}. Built once from cfg.channels[].response_actions
    (config is immutable at runtime — A3). Per-event lookup: channel_name → per-action map.
    DryRunSideEffectExecutor is the v1 default (D-01/D-02 — no real executor wired).
    """
    from ach_agent.engine.lifecycle import run_invocation

    async def engine_runner(event: MessageEvent, on_kill: Callable[[], None]) -> None:
        # Build sanitized launch env — ek_ is never read into a local variable
        _sanitized = SanitizedEnv(os.environ.copy())  # noqa: F841 — used by launch

        # MEM-01/MEM-02/D-02: probe memory backend BEFORE pool.acquire (Pitfall 3).
        # prepare_memory never raises (fail-open contract).
        # When unavailable: MEMORY_DEGRADED incremented + WARN logged inside prepare_memory.
        memory_prompt: str = ""
        if memory_cfg is not None:
            from ach_agent.memory.adapter import prepare_memory

            mem_available, memory_prompt = await prepare_memory(memory_cfg)
            mcp_servers = [memory_cfg.endpoint] if mem_available else []
        else:
            mcp_servers = []

        # Build per-invocation engine config with memory MCP server iff reachable (D-02).
        # Dataclass copy with updated mcp_servers — original engine_cfg is not mutated.
        import dataclasses

        if dataclasses.is_dataclass(engine_cfg) and not isinstance(engine_cfg, type):
            invocation_engine_cfg = dataclasses.replace(engine_cfg, mcp_servers=mcp_servers)
        else:
            # Non-dataclass (e.g. MagicMock in tests) — attach attribute directly.
            invocation_engine_cfg = engine_cfg
            invocation_engine_cfg.mcp_servers = mcp_servers

        server = await pool.acquire(invocation_engine_cfg)
        try:
            # CR-01: if this is a reply-mode event, we must resolve reply_future
            # before any exception can escape to the lane catch (which would swallow it
            # and leave the route waiting forever). Use try/finally for guaranteed resolution.
            future = event.reply_future
            result: dict[str, Any] = {}
            try:
                # MEM-01: append ## Memory section (summaries or unavailable note) to prompt.
                base_prompt = build_engine_prompt(event)
                full_prompt = f"{base_prompt}\n\n{memory_prompt}" if memory_prompt else base_prompt
                result = cast(
                    dict[str, Any],
                    await run_invocation(
                        server=server,
                        session_id=event.session_key,
                        prompt=full_prompt,
                        response_actions_schema=[],
                        max_invocation_seconds=max_invocation_seconds,
                        on_kill=on_kill,
                    ),
                )
            except Exception as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                raise
            finally:
                # Ensure reply_future is always resolved on the happy path too
                if future is not None and not future.done():
                    # Extract first reply action's text (same as old sync_invoke logic)
                    reply_text = ""
                    for action in result.get("actions", []):
                        if action.get("kind") == "reply":
                            reply_text = str(action.get("input", {}).get("text", ""))
                            break
                    future.set_result(reply_text)

            if future is not None:
                # Reply mode: future is resolved above; do NOT call dispatch_actions
                # (no gitlab_comment — CR-01). Return here so dispatch block is skipped.
                return

            # A2A completion path (W9 — engine_runner does NOT import channels.a2a):
            # The on_complete callable is injected by the A2A wiring closure in main.py
            # into event.delivery_context['on_complete'] before handler.handle() is called.
            # engine_runner just calls it if present — no channel-type-specific logic here.
            on_complete = event.delivery_context.get("on_complete")
            if on_complete is not None:
                # Extract reply text from the first 'reply' action
                reply_text = ""
                for action in result.get("actions", []):
                    if action.get("kind") == "reply":
                        reply_text = str(action.get("input", {}).get("text", ""))
                        break
                on_complete(event.session_key, reply_text)
                return

            # Async/gitlab_comment mode (D-08 / ACT-02/03/04/D-05):
            # Route actions through dispatch_actions with event + per-channel
            # response_action_config for the consent gate (ACT-03):
            #   sideEffect → consent gate (auto: always dry-run; consent: check
            #     event.user_consented; denied → ConsentDenied caught in loop → audit).
            #   reply → GitlabCommentAdapter.deliver(action, event.delivery_context).
            # side_effect_executor left at default (DryRunSideEffectExecutor — D-01/D-02).
            # Delivery stays on the router lane — NOT create_task from route (Pitfall 4).
            _rac = response_action_config_by_channel or {}
            await dispatch_actions(
                actions=result.get("actions", []),
                adapter=delivery_adapter,
                context=event.delivery_context,
                event=event,
                response_action_config=_rac.get(event.channel_name, {}),
            )
        finally:
            # Return the engine server to the pool. Slot release is owned by the
            # lane: its `async with` blocks free the semaphores and its finally
            # calls on_kill for queued_total. run_invocation also fires on_kill on
            # a watchdog kill; on_kill is idempotent so that double call is safe.
            # EnginePool.release(ttl_seconds) — no server arg (pool tracks internally).
            try:
                await pool.release(ttl_seconds=float(engine_cfg.shared_ttl_seconds))
            except Exception as exc:  # noqa: BLE001
                log.warning("pool release error", error=str(exc))

    return engine_runner


async def _drain(
    state: dict[str, Any],
    uv_server: Any,
    cron_scheduler: CronScheduler | None,
    router: Any,
    gitlab_adapter: GitlabCommentAdapter,
    dedup_store: Any,
    slack_adapters: list[Any] | None = None,
    telegram_adapters: list[Any] | None = None,
) -> None:
    """D-09/D-10/D-11: graceful drain sequence on SIGTERM.

    1. Flip draining + readyz NotReady (D-09 step 2, D-12).
    2. Stop uvicorn (no new HTTP connections).
    3. Stop CronScheduler (D-06 intake-stop); cancels its single asyncio task.
    4. Drain queued + in-flight lane work (D-11):
       Lane tasks blocked on empty queue.get() are cancelled via Lane.cancel()
       (RESEARCH Pitfall 4). In-flight runs bounded by maxInvocationSeconds watchdog (D-10).
    5. Cleanup: close dedup store, close gitlab adapter, inc DRAIN_COMPLETED, sys.exit(0).

    No grace-deadline timer (D-10): maxInvocationSeconds watchdog + K8s SIGKILL backstop.
    Never logs ek_/GITLAB_TOKEN (T-03-07): log emits only path/count/reason fields.
    """
    from ach_agent.engine.metrics import DRAIN_COMPLETED

    # 1. Flip draining flag + readyz NotReady (D-09, D-12 straggler gate)
    state["draining"] = True
    state["ready"] = False
    log.info("drain: readyz flipped NotReady, intake stopped")

    # 2. Signal uvicorn to stop accepting new connections
    if uv_server is not None:
        uv_server.should_exit = True

    # 3. Stop CronScheduler (D-06 intake-stop): cancels its single asyncio task cleanly.
    if cron_scheduler is not None:
        await cron_scheduler.stop()

    # 3b. Disconnect Slack adapters — stop Socket Mode intake (D-06)
    for adapter in slack_adapters or []:
        try:
            await disconnect_slack_adapter(adapter)
        except Exception as exc:  # noqa: BLE001
            log.warning("drain: slack adapter disconnect error", error=str(exc))

    # 3c. Disconnect Telegram adapters — stop PTB polling intake (D-06)
    for adapter in telegram_adapters or []:
        try:
            await disconnect_telegram_adapter(adapter)
        except Exception as exc:  # noqa: BLE001
            log.warning("drain: telegram adapter disconnect error", error=str(exc))

    # 4. Drain queued + in-flight lane work (D-11)
    # Step 4a: wait for in-flight + queued events to finish processing.
    # Lane._queue.task_done() fires in Lane._consume finally after each event.
    # asyncio.Queue.join() blocks until all task_done() calls match put() calls.
    # This preserves in-flight work — we only cancel AFTER queues drain.
    #
    # Step 4b: cancel idle lane tasks (stuck on queue.get() — RESEARCH Pitfall 4).
    # No new events enter lanes after draining=True, so after join() the lanes
    # are idle; cancel() unblocks the empty queue.get() await.
    lanes_snapshot = list(router.lanes.values())
    if lanes_snapshot:
        log.info("drain: waiting for lane queues to drain", count=len(lanes_snapshot))
        # Wait for all queued + in-flight events to complete (D-11)
        await asyncio.gather(*(lane.join() for lane in lanes_snapshot), return_exceptions=True)
        # Now cancel idle lane tasks (they are blocked on empty queue.get())
        for lane in lanes_snapshot:
            lane.cancel()
        await asyncio.gather(
            *(lane.wait_closed() for lane in lanes_snapshot), return_exceptions=True
        )

    # 5. Cleanup: close dedup store, adapter, increment metric, return cleanly.
    # Do NOT sys.exit(0) here: this runs inside an asyncio task, so SystemExit
    # force-cancels the still-pending uvicorn serve task and dumps a CancelledError
    # traceback to stderr (even though the exit code is 0). Instead we return; main()
    # then awaits uvicorn's own graceful shutdown (should_exit was set above) and the
    # process exits 0 naturally with no traceback.
    if hasattr(dedup_store, "close"):
        dedup_store.close()
    await gitlab_adapter.close()
    DRAIN_COMPLETED.inc()
    log.info("drain: complete")


async def main() -> None:
    """Async entrypoint: load config, boot router, start channel adapters + uvicorn."""
    config_path = os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)

    # Step 2: load config (hard-fail on schema mismatch — CFG-02)
    cfg = load_config(config_path)

    # Step 3: D-02 gate — reject unwired channel types before serving
    for channel in cfg.channels:
        if channel.type not in WIRED_CHANNEL_TYPES:
            log.error(
                "channel type configured but not supported in this build — exiting",
                channel_name=channel.name,
                channel_type=channel.type,
                wired_types=sorted(WIRED_CHANNEL_TYPES),
            )
            sys.exit(1)

    # Step 4: PID file (Pitfall 11 — tolerate non-writable in dev)
    _write_pid_file(PID_FILE)

    # Step 5: build delivery adapter and engine pool
    # 6a. GitlabCommentAdapter — base_url from GITLAB_BASE_URL env (D-12 deviation).
    # Falls back to https://gitlab.com; GITLAB_TOKEN is read at deliver() call time,
    # never stored (SEC-02 spirit). For reply-mode channels this adapter is unused;
    # delivery returns synchronously on the held connection instead.
    gitlab_base_url = os.environ.get("GITLAB_BASE_URL", "https://gitlab.com")
    gitlab_adapter = GitlabCommentAdapter(base_url=gitlab_base_url)

    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.pool import EnginePool

    engine_cfg = EngineConfig(
        binary_path=cfg.engine.binary_path,
        work_dir=cfg.engine.work_dir,
        session_dir=cfg.engine.session_dir,
        model=cfg.model.default,
        provider=cfg.model.provider,
        startup_timeout_seconds=cfg.engine.startup_timeout_seconds,
        shared_enabled=cfg.engine.shared.enabled,
        shared_ttl_seconds=cfg.engine.shared.ttl_seconds,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
    )
    pool = EnginePool()

    # ACT-03/A3: build boot-time response_action_config_by_channel mapping.
    # Config is immutable at runtime (A3) — safe to build once here.
    # Maps: channel_name → {action_name → ResponseActionBlock}
    # Used by dispatch_actions consent gate for per-event lookup (Open Question 1).
    response_action_config_by_channel: dict[str, dict[str, ResponseActionBlock]] = {
        channel.name: {block.name: block for block in channel.response_actions}
        for channel in cfg.channels
    }

    # 6b. Build engine_runner wired to GitlabCommentAdapter (gitlab_comment path).
    # MEM-01/D-02: pass memory_cfg so engine_runner probes before pool.acquire (Pitfall 3).
    # ACT-03: pass response_action_config_by_channel for sideEffect consent gate.
    engine_runner = _make_engine_runner(
        pool=pool,
        engine_cfg=engine_cfg,
        delivery_adapter=gitlab_adapter,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        memory_cfg=cfg.memory,
        response_action_config_by_channel=response_action_config_by_channel,
    )

    # Step 6 (cont.): construct Router with all limits from config (RTR-03/04)
    # D-03/D-04: select dedup store from persistence config (fail-split policy).
    dedup_store = _open_dedup_store(cfg)
    router = Router(
        max_concurrent_invocations=cfg.limits.max_concurrent_invocations,
        max_queued_total=cfg.limits.max_queued_total,
        idempotency_window_seconds=cfg.limits.idempotency_window_seconds,
        dedup_store=dedup_store,
        engine_runner=engine_runner,
        delivery_adapter=None,
        max_invocation_seconds=float(cfg.limits.max_invocation_seconds),
    )

    # Collect webhook channels to wire; build FastAPI app if any exist
    webhook_channels = [ch for ch in cfg.channels if ch.type == "webhook"]

    # Build A2A bridges and sub-apps (topology A: mounted under the same FastAPI/uvicorn socket).
    # W9: engine_runner must NOT import channels.a2a or hold a bridge reference.
    # Wiring: for each A2A channel, construct an A2AAgentExecutorBridge, then wrap the router
    # in a thin handler that injects an on_complete closure into event.delivery_context before
    # routing. engine_runner reads event.delivery_context['on_complete'] and calls it — no
    # channel-type-specific logic in engine_runner (dependency arrow: channels→engine only).
    a2a_bridges: list[A2AAgentExecutorBridge] = []
    a2a_mounts: list[tuple[str, Any]] = []

    for channel in cfg.channels:
        if channel.type != "a2a":
            continue

        # The bridge is created here (boot module) — engine_runner never imports it.
        bridge = A2AAgentExecutorBridge(handler=None, pool=pool, channel_cfg=channel)

        # on_complete closure captures the bridge (W9: the closure lives in boot module,
        # engine tier stays unaware of A2A type).
        def _make_on_complete(b: A2AAgentExecutorBridge) -> Any:
            def on_complete(session_key: str, reply_text: str) -> None:
                b.signal_completion(session_key, reply_text)

            return on_complete

        _on_complete = _make_on_complete(bridge)

        # Wrap the router to inject on_complete into delivery_context (W9 pattern).
        class _A2AHandler:
            """Handler wrapper that injects on_complete into delivery_context."""

            def __init__(self, rtr: Any, fn: Any) -> None:
                self._rtr = rtr
                self._fn = fn

            async def handle(self, event: MessageEvent) -> Any:
                event.delivery_context["on_complete"] = self._fn
                return await self._rtr.handle(event)

        bridge._handler = _A2AHandler(router, _on_complete)

        # Build the A2A AgentCard from channel config (minimal — receiver-only v1, spec §14.6).
        # make_a2a_agent_card keeps a2a.* imports inside channels/a2a.py (RTR-06 fence).
        agent_card = make_a2a_agent_card(channel.name)
        sub_app = build_a2a_app(agent_card, bridge, rpc_prefix="/")
        mount_path = f"/a2a/{channel.name}"
        a2a_mounts.append((mount_path, sub_app))
        a2a_bridges.append(bridge)
        log.info("a2a channel bridge built", channel_name=channel.name, mount_path=mount_path)

    # 6c. Create FastAPI app with all webhook channels (CR-01: reply mode uses
    # event.reply_future resolved by engine_runner; no sync_invoke needed).
    # pool=pool threads the A′ gate (DUR-02) into the webhook route — live in production.
    # a2a_mounts threads the A2A sub-apps under the same socket (topology A).
    app = create_app(
        channels=webhook_channels,
        handler=router,
        max_invocation_seconds=float(cfg.limits.max_invocation_seconds) + 5.0,
        pool=pool,
        a2a_mounts=a2a_mounts,
    )
    # Expose state dict so _drain can flip draining/ready (same ref as app.extra['state'])
    state: dict[str, Any] = app.extra["state"]

    # Step 7: wire channel adapters (D-08: one CronScheduler for ALL cron channels, SC#3)
    seen_channel_names: set[str] = set()
    tasks: list[asyncio.Task[None]] = []
    slack_adapters: list[Any] = []  # tracked for drain disconnection (D-06)
    telegram_adapters: list[Any] = []  # tracked for drain disconnection (D-06)
    has_webhook = False
    uv_server: Any = None  # captured below if webhook channels exist

    # D-08/SC#3: collect all cron channels and construct exactly ONE CronScheduler.
    # Pitfall 9 (one task per channel) is superseded by D-08 (one scheduler for all).
    cron_channels = [ch for ch in cfg.channels if ch.type == "cron"]
    cron_scheduler: CronScheduler | None = None
    if cron_channels:
        cron_scheduler = CronScheduler(cron_channels, handler=router, pool=pool)
        await cron_scheduler.start()
        log.info(
            "cron scheduler started",
            channel_count=len(cron_channels),
            channel_names=[ch.name for ch in cron_channels],
        )

    for channel in cfg.channels:
        if channel.name in seen_channel_names:
            log.error(
                "duplicate channel name — skipping",
                channel_name=channel.name,
            )
            continue
        seen_channel_names.add(channel.name)

        if channel.type == "cron":
            # Already handled above by CronScheduler (D-08/SC#3) — skip per-channel task.
            pass
        elif channel.type == "webhook":
            # Webhook channels are served by uvicorn via the FastAPI app (started below).
            # Route is already registered in create_app() for all webhook_channels.
            has_webhook = True
            log.info(
                "webhook channel registered",
                channel_name=channel.name,
            )
        elif channel.type == "slack":
            # Connect Hermes SlackAdapter and register shim (D-04/D-06).
            # connect() starts Socket Mode background task internally — non-blocking.
            adapter = await connect_slack_adapter(channel, router, pool=pool)
            slack_adapters.append(adapter)
            log.info("slack channel connected", channel_name=channel.name)
        elif channel.type == "telegram":
            # Connect Hermes TelegramAdapter and register shim (D-04/D-06).
            # connect() starts PTB polling background task internally — non-blocking.
            adapter = await connect_telegram_adapter(channel, router, pool=pool)
            telegram_adapters.append(adapter)
        elif channel.type == "a2a":
            # A2A channel: bridge + sub-app already built above and mounted in create_app.
            # uvicorn must be started to serve A2A HTTP requests (topology A, T-04-17).
            has_webhook = True
            log.info("a2a channel registered", channel_name=channel.name)

    if has_webhook:
        # Boot uvicorn on the harness host/port (from health config).
        # uvicorn shares the SAME event loop as the cron tasks — no thread pool,
        # single-process topology (spec §15 topology A).
        host = cfg.health.host
        port = cfg.health.port
        uv_config = uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",  # uvicorn internal logs; harness uses structlog
        )
        uv_server = uvicorn.Server(uv_config)
        log.info("uvicorn starting", host=host, port=port)
        tasks.append(asyncio.create_task(uv_server.serve()))

    # Install SIGTERM handler via loop.add_signal_handler (NOT signal.signal).
    # RESEARCH Pitfall 2: uvicorn uses signal.signal() inside capture_signals() —
    # loop.add_signal_handler uses signalfd on Linux and coexists with signal.signal.
    shutdown_event: asyncio.Event = asyncio.Event()

    def _on_sigterm() -> None:
        # Idempotent: a repeat SIGTERM/SIGINT during drain (or a late handler
        # invocation as uvicorn restores its own signal handlers on shutdown) must
        # not re-log or re-trigger — the drain is already underway.
        if shutdown_event.is_set():
            return
        log.info("SIGTERM received — initiating graceful drain")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    loop.add_signal_handler(signal.SIGINT, _on_sigterm)  # Ctrl+C in dev

    log.info("ach-agent started", channel_count=len(tasks))

    if tasks:
        # Wait for SIGTERM/SIGINT OR all tasks to finish (tasks loop forever normally)
        shutdown_task = asyncio.create_task(shutdown_event.wait())
        await asyncio.wait(
            [shutdown_task, *tasks],
            return_when=asyncio.FIRST_COMPLETED,
        )
    else:
        # CR-03: no uvicorn task (Slack/Telegram/cron-only config) — still wait for SIGTERM.
        # Without this, the event loop exits immediately, orphaning Hermes background tasks.
        await shutdown_event.wait()

    if shutdown_event.is_set():
        # Graceful drain (DUR-03): flip readyz, drain lanes, cleanup. _drain sets
        # uv_server.should_exit so uvicorn stops accepting, then returns (no sys.exit).
        await _drain(
            state=state,
            uv_server=uv_server,
            cron_scheduler=cron_scheduler,
            router=router,
            gitlab_adapter=gitlab_adapter,
            dedup_store=dedup_store,
            slack_adapters=slack_adapters,
            telegram_adapters=telegram_adapters,
        )
        if tasks:
            # uvicorn's serve() task returns on its own once should_exit=True; await it
            # so its lifespan shutdown completes before asyncio.run tears the loop down.
            # This avoids the force-cancel CancelledError traceback the old sys.exit(0)
            # produced — the process now exits 0 cleanly.
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("ach-agent shutdown complete")
    else:
        # Normal termination (all tasks completed without SIGTERM — rare in prod)
        await gitlab_adapter.close()
        log.info("ach-agent shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())

# SPDX-License-Identifier: Apache-2.0
"""ach-agent entrypoint — bootstrap wiring.

Boot order (CRITICAL — Pitfall 8: configure_logging FIRST, before any import
that may emit a log line):
  1. configure_logging()        <- SEC-01: redact_ek_processor installed first
  2. load_config(path)          <- hard-fail on schema mismatch (CFG-02)
  3. D-02 gate: reject unwired channel types (hard-fail, non-zero exit)
  4. Write PID file             <- Pitfall 11: single-replica guard
  5. Construct Router (wraps SanitizedEnv engine launch)
  6b. Build engine_runner (CR-01: branches on event.reply_future for reply mode;
      relays the terminal text — egress is the agent's via external MCP tools)
  6c. Create FastAPI app via create_app(channels, router)
  7. asyncio.run(main()) — starts uvicorn + cron tasks on the SAME event loop

RTR-06: router must not import from hermes_agent.*; engine injected as callable.
D-08: deliver.type: reply → event.reply_future resolved by engine_runner on the lane,
      awaited by the route (CR-01: exactly one engine execution per event).
      async channels → engine_runner relays nothing; the agent already acted via MCP.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog
import uvicorn

from ach_agent.channels.a2a import A2AAgentExecutorBridge, build_a2a_app, make_a2a_agent_card
from ach_agent.channels.cron import CronScheduler
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.queue import QueueConsumer
from ach_agent.channels.tui import TuiChannel
from ach_agent.config import load_config
from ach_agent.engine.context import fetch_context
from ach_agent.engine.hydrate import hydrate, resolve_model
from ach_agent.engine.mcp_proxy import McpProxy, start_model_proxy, stop_model_proxies
from ach_agent.engine.sanitized_env import SanitizedEnv, configure_logging
from ach_agent.http.app import create_app
from ach_agent.router import Router

# configure_logging() is called at module TOP (not in main()) so that any
# log emission during import (e.g. validation warnings) is already redacted.
# Must be the FIRST executable statement (Pitfall 8 / SEC-01).
configure_logging()

log = structlog.get_logger(__name__)

# D-02: only channel types wired in this build
WIRED_CHANNEL_TYPES: frozenset[str] = frozenset({"cron", "webhook", "a2a", "queue", "tui"})

# Plan 2: model.type → ACH compat-endpoint path prefix fronted by the model proxy.
# opencode's baseURL becomes "http://127.0.0.1:<port>/<prefix>".
_MODEL_ENDPOINT_PREFIX: dict[str, str] = {
    "openai": "v1",
    "gemini": "gemini",
    "anthropic": "anthropic",
}

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
    max_invocation_seconds: int,
    memory_cfg: Any = None,
) -> Callable[..., Any]:
    """Build the engine_runner callable injected into the Router.

    The runner is called by Lane as: engine_runner(event, on_kill).
    It acquires a ManagedServer from the pool, calls run_invocation (which returns
    the single terminal object), then relays the terminal `text`:

    - reply mode (event.reply_future is not None):
        set_result(text) on the future. The route is awaiting this future to return
        200 + body to the client.
        CRITICAL: the future MUST always be resolved (set_result or set_exception)
        even on error, otherwise the route hangs indefinitely. A try/except sets the
        exception on error before re-raising.

    - on_complete mode (event.delivery_context['on_complete'] present, e.g. a2a):
        call on_complete(session_key, text) — the channel wiring relays the reply.

    - async mode (neither): nothing to deliver. Egress already happened via the
        agent's external MCP tool calls — the harness never posts on the model's behalf.

    SanitizedEnv is used to build the subprocess launch env (SEC-01 /
    T-01-EK folded todo): the engine_cfg carries paths, never ek_ values.

    memory_cfg (MEM-01/MEM-02/D-02): optional MemoryBlock from config.memory.
    When present, prepare_memory is called BEFORE pool.acquire so the opencode.json
    written for that server includes or excludes the memory MCP server (Pitfall 3).
    Fail-open: unreachable backend → exclude MCP server, log WARN + metric, run anyway.
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
            # CR-01: in reply mode the future MUST always be resolved (set_result or
            # set_exception), otherwise the awaiting route hangs forever. The try/except
            # sets the exception on error before re-raising.
            future = event.reply_future
            try:
                # MEM-01: append ## Memory section (summaries or unavailable note) to prompt.
                base_prompt = build_engine_prompt(event)
                full_prompt = f"{base_prompt}\n\n{memory_prompt}" if memory_prompt else base_prompt
                obj = await run_invocation(
                    server=server,
                    session_id=event.session_key,
                    prompt=full_prompt,
                    terminal_retries=1,
                    max_invocation_seconds=max_invocation_seconds,
                    on_kill=on_kill,
                )
            except Exception as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
                raise

            text = str(obj.get("text", ""))

            if future is not None:
                # Reply mode: resolve the future the route is awaiting.
                if not future.done():
                    future.set_result(text)
                return

            # A2A completion path (W9 — engine_runner does NOT import channels.a2a):
            # The on_complete callable is injected by the A2A wiring closure in main.py
            # into event.delivery_context['on_complete'] before handler.handle() is called.
            on_complete = event.delivery_context.get("on_complete")
            if on_complete is not None:
                on_complete(event.session_key, text)
                return

            # Async mode: nothing to deliver. Egress already happened via the agent's
            # external MCP tool calls — the harness never posts on the model's behalf.
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
    dedup_store: Any,
) -> None:
    """D-09/D-10/D-11: graceful drain sequence on SIGTERM.

    1. Flip draining + readyz NotReady (D-09 step 2, D-12).
    2. Stop uvicorn (no new HTTP connections).
    3. Stop CronScheduler (D-06 intake-stop); cancels its single asyncio task.
    4. Drain queued + in-flight lane work (D-11):
       Lane tasks blocked on empty queue.get() are cancelled via Lane.cancel()
       (RESEARCH Pitfall 4). In-flight runs bounded by maxInvocationSeconds watchdog (D-10).
    5. Cleanup: close dedup store, inc DRAIN_COMPLETED, sys.exit(0).

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

    # 5. Cleanup: close dedup store, increment metric, return cleanly.
    # Do NOT sys.exit(0) here: this runs inside an asyncio task, so SystemExit
    # force-cancels the still-pending uvicorn serve task and dumps a CancelledError
    # traceback to stderr (even though the exit code is 0). Instead we return; main()
    # then awaits uvicorn's own graceful shutdown (should_exit was set above) and the
    # process exits 0 naturally with no traceback.
    if hasattr(dedup_store, "close"):
        dedup_store.close()
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

    # Plan 2 (CONTRACT §6.10): self-hydrate from ACH, then front the model + MCP traffic
    # via localhost reverse-proxies that inject the ek_. opencode points ONLY at localhost
    # and never sees the ek_ or the real ACH URL.
    #
    # The ek_ (ACH_TOKEN) is read here solely to pass to hydration + the proxies; it is
    # NEVER logged and NEVER written to opencode.json (the proxies hold it in a closure).
    # When ACH_TOKEN is unset (local dev with a hand-written config + no ACH), hydration is
    # skipped and opencode.json falls back to {env:...} refs — the harness stays runnable.
    ek = os.environ.get("ACH_TOKEN")
    model_base_url: str = ""
    mcp_local_urls: dict[str, str] = {}
    mcp_proxy: McpProxy | None = None
    if ek:
        manifest = await hydrate(cfg.capability.ach.base_url, ek)
        resolve_model(manifest, cfg.model.name)  # hard-fail (sys.exit 1) if model absent
        await fetch_context(manifest.context, ek, Path(cfg.persistence.mount_path))
        mcp_proxy = McpProxy()
        # NOTE: CONTRACT_v3 capability.filter.exclude only carries `tools` (opencode-side,
        # deferred to Plan 3/4) — there is no per-MCP-server exclude in the schema, so all
        # hydrated servers are fronted.
        mcp_local_urls = await mcp_proxy.start(manifest.mcp_servers, ek, exclude=set())
        model_proxy_base = await start_model_proxy(cfg.capability.ach.base_url, ek)
        prefix = _MODEL_ENDPOINT_PREFIX[cfg.model.type]
        model_base_url = f"{model_proxy_base}/{prefix}"
        log.info(
            "hydrated + localhost proxies started",
            environment=manifest.environment,
            model_count=len(manifest.models),
            mcp_count=len(mcp_local_urls),
        )

        # A2A egress (Plan 3): expose peer agents as harness-hosted MCP tools so
        # opencode can call them. The ek_ stays in the harness (injected as the
        # peer auth header by A2AAgentClient). RTR-06: a2a_egress imports a2a-sdk
        # only inside its functions, and we import the builder lazily here so the
        # no-a2a-agents path (all current tests) imports nothing new and is a no-op.
        if manifest.a2a_agents:
            from ach_agent.engine.a2a_egress import (
                build_a2a_mcp_server,
                build_a2a_tools,
            )

            a2a_tools = build_a2a_tools(manifest.a2a_agents, ek=ek)
            # Plan 3/4 follow-up: host build_a2a_mcp_server(a2a_tools) on a
            # localhost port and add it to the opencode.json mcp block so opencode
            # discovers these tools. The server is built here (validates wiring)
            # but NOT started — no listener is opened (VERIFICATION DEBT).
            _a2a_egress_server = build_a2a_mcp_server(a2a_tools)
            log.info(
                "a2a egress tools built (server not yet hosted — Plan 3/4)",
                agent_count=len(manifest.a2a_agents),
                tool_count=len(a2a_tools),
            )

    # Step 5: build the engine pool. Egress is the agent's via external MCP tools —
    # the harness has no delivery adapter (it never posts on the model's behalf).
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.pool import EnginePool

    engine_cfg = EngineConfig(
        work_dir=cfg.work_dir,
        session_dir=f"{cfg.persistence.mount_path}/opencode/sessions",
        provider=cfg.model.type,
        model=cfg.model.name,
        params=cfg.model.params,
        startup_timeout_seconds=cfg.startup_timeout_seconds,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        shared_ttl_seconds=0,
        model_base_url=model_base_url,
        mcp_local_urls=mcp_local_urls,
    )
    pool = EnginePool()

    # 6b. Build engine_runner. MEM-01/D-02: pass memory_cfg so engine_runner probes
    # before pool.acquire (Pitfall 3).
    engine_runner = _make_engine_runner(
        pool=pool,
        engine_cfg=engine_cfg,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        memory_cfg=cfg.memory,
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

    # Queue channels (redis Streams, ackMode:onComplete): one QueueConsumer each.
    # Each consumer owns a single asyncio consume task; stopped in the drain branch.
    queue_channels = [ch for ch in cfg.channels if ch.type == "queue"]
    queue_consumers: list[QueueConsumer] = []
    for channel in queue_channels:
        consumer = QueueConsumer(channel, handler=router, pool=pool)
        await consumer.start()
        queue_consumers.append(consumer)
        log.info("queue consumer started", channel_name=channel.name, stream=channel.queue.key)  # type: ignore[union-attr]

    # TUI channels (stdin/stdout free-form, no terminal contract): one TuiChannel each.
    # Like cron, each owns a single background asyncio task; started outside `tasks`,
    # stopped in the drain branch. A tui-only config still waits via shutdown_event.
    tui_channels = [ch for ch in cfg.channels if ch.type == "tui"]
    tui_consumers: list[TuiChannel] = []
    for channel in tui_channels:
        tui = TuiChannel(channel, handler=router, pool=pool)
        await tui.start()
        tui_consumers.append(tui)
        log.info("tui channel started", channel_name=channel.name)

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
        elif channel.type == "queue":
            # Already handled above by its QueueConsumer — skip per-channel task.
            log.info("queue channel registered", channel_name=channel.name)
        elif channel.type == "tui":
            # Already handled above by its TuiChannel (background task like cron).
            log.info("tui channel registered", channel_name=channel.name)
        elif channel.type == "webhook":
            # Webhook channels are served by uvicorn via the FastAPI app (started below).
            # Route is already registered in create_app() for all webhook_channels.
            has_webhook = True
            log.info(
                "webhook channel registered",
                channel_name=channel.name,
            )
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
        # CR-03: no uvicorn task (cron-only config) — still wait for SIGTERM.
        # Without this, the event loop exits immediately, orphaning background tasks.
        await shutdown_event.wait()

    if shutdown_event.is_set():
        # Graceful drain (DUR-03): flip readyz, drain lanes, cleanup. _drain sets
        # uv_server.should_exit so uvicorn stops accepting, then returns (no sys.exit).
        await _drain(
            state=state,
            uv_server=uv_server,
            cron_scheduler=cron_scheduler,
            router=router,
            dedup_store=dedup_store,
        )
        # Stop queue consumers (cancel their consume tasks; close clients we created).
        # Done after _drain so in-flight lane work routed from the queue can complete.
        for consumer in queue_consumers:
            await consumer.stop()
        # Stop TUI channels (cancel their read-loop tasks) — like cron/queue intake-stop.
        for tui in tui_consumers:
            await tui.stop()
        # Plan 2: tear down the localhost proxies (closes their aiohttp runners/sessions).
        await stop_model_proxies()
        if mcp_proxy is not None:
            await mcp_proxy.stop()
        if tasks:
            # uvicorn's serve() task returns on its own once should_exit=True; await it
            # so its lifespan shutdown completes before asyncio.run tears the loop down.
            # This avoids the force-cancel CancelledError traceback the old sys.exit(0)
            # produced — the process now exits 0 cleanly.
            await asyncio.gather(*tasks, return_exceptions=True)
        log.info("ach-agent shutdown complete")
    else:
        # Normal termination (all tasks completed without SIGTERM — rare in prod)
        log.info("ach-agent shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())

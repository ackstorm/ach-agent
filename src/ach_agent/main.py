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
from urllib.parse import urlsplit

import structlog
import uvicorn

from ach_agent.channels.a2a import A2AAgentExecutorBridge, build_a2a_app, make_a2a_agent_card
from ach_agent.channels.cron import CronScheduler
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.queue import QueueConsumer
from ach_agent.channels.tui import run_one_shot, run_tui_console
from ach_agent.config import load_config
from ach_agent.config.schema import CodememMemory, HindsightMemory, Memory
from ach_agent.engine.context import fetch_context
from ach_agent.engine.hydrate import hydrate, resolve_model
from ach_agent.engine.mcp_proxy import McpProxy, start_model_proxy, stop_model_proxies
from ach_agent.engine.sanitized_env import SanitizedEnv, configure_logging
from ach_agent.http.app import create_app
from ach_agent.router import Router
from ach_agent.templating import build_template_context, render_template

# configure_logging() is called at module TOP (not in main()) so that any
# log emission during import (e.g. validation warnings) is already redacted.
# Must be the FIRST executable statement (Pitfall 8 / SEC-01).
configure_logging()

log = structlog.get_logger(__name__)

# D-02: only channel types wired in this build
WIRED_CHANNEL_TYPES: frozenset[str] = frozenset({"cron", "webhook", "a2a", "queue"})

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


def build_engine_prompt(
    event: MessageEvent,
    channel_cfg: Any = None,
    agent_name: str = "",
    memory_bank: str = "",
) -> str:
    """Build a meaningful engine prompt from a MessageEvent.

    When the channel declares a `prompt` template, it wins: it is rendered through the
    {{ }} engine against the event payload + harness internals (channel.prompt is the
    contract-specified per-channel instruction). Otherwise the legacy fallback applies:
    cron `scheduled_tick`, free-form `payload['text']`, or a built MR review instruction.

    Never raises; falls back to an empty string if no usable content is found.
    """
    # Channel-prompt path: render the contract-authored template (CONTRACT §2 channel.prompt)
    if channel_cfg is not None and getattr(channel_cfg, "prompt", None):
        ctx = build_template_context(
            event.payload,
            channel_name=event.channel_name,
            channel_type=getattr(channel_cfg, "type", "") or "",
            channel_source=getattr(channel_cfg, "source", "") or "",
            agent_name=agent_name,
            memory_bank=memory_bank,
            event_id=event.idempotency_key,
            session_key=event.session_key,
        )
        return render_template(channel_cfg.prompt, ctx)

    # Cron path: payload has a scheduled_tick key
    scheduled_tick = event.payload.get("scheduled_tick")
    if scheduled_tick is not None:
        return str(scheduled_tick)

    # Free-form text path: the --tui console (and queue/a2a) carry the prompt verbatim
    # in payload['text']. In console mode the typed line IS the prompt.
    text = event.payload.get("text")
    if text:
        return str(text)

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


def resolve_engine_paths(cfg: Any) -> tuple[str, str]:
    """Resolve the opencode HOME and the agent workDir from the contract.

    Both are definable (engine.home / engine.workDir). When omitted:
      - home → <mountPath>/home if persistence.enabled (persistent), else /tmp/ach-home.
      - work_dir → <home>/workspace.
    Static state (config, skills, sessions) lives under HOME; HOME under mountPath persists.
    """
    home = cfg.engine.home
    if not home:
        home = f"{cfg.persistence.mount_path}/home" if cfg.persistence.enabled else "/tmp/ach-home"
    work_dir = cfg.engine.work_dir or f"{home}/workspace"
    return home, work_dir


# resolve_codemem_wiring has moved to ach_agent.memory.codemem; re-exported here for
# back-compat with existing callers (tests/integration/test_codemem_wiring.py, etc.).
from ach_agent.memory.codemem import resolve_codemem_wiring as resolve_codemem_wiring  # noqa: E402


def ach_state_dir(home: str) -> Path:
    """The single hydration state root: <home>/.ach-state (prompts + artifacts)."""
    return Path(home) / ".ach-state"


def link_ach_state(home: str, work_dir: str) -> Path:
    """Create <home>/.ach-state and, when workDir differs, a <workDir>/.ach-state symlink.

    The symlink gives the agent's shell (cwd = workDir) one stable path to hydrated
    artifacts; HOME stays the canonical read-only root. Best-effort: a symlink failure
    (e.g. unsupported FS) is non-fatal — the agent can still reach state under HOME.
    """
    state = ach_state_dir(home)
    state.mkdir(parents=True, exist_ok=True)
    if work_dir and Path(work_dir).resolve() != Path(home).resolve():
        link = Path(work_dir) / ".ach-state"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            try:
                link.symlink_to(state, target_is_directory=True)
            except OSError as e:
                log.warning("workDir .ach-state symlink failed (non-fatal)", error=str(e))
    return state


def resolve_system_prompt(prompt_block: Any, state_dir: Path) -> str:
    """Resolve prompt.system (text | file | ach | None) into the persona string.

    text → the inline text. file → <state_dir>/<file>. ach → the named hydrated prompt at
    <state_dir>/prompts/<ach>/ (its sole file, or the given `file` subpath). For every
    on-disk form the resolved REAL path is re-checked to stay inside state_dir (defense in
    depth over the schema validator, which only sees the literal path), and a missing file
    is a hard startup failure — a persona the operator declared but hydration did not deliver
    is a misconfiguration, not fail-open. None → "" (no persona).
    """
    if prompt_block is None or prompt_block.system is None:
        return ""
    system = prompt_block.system
    if system.type == "text":
        return str(system.text)
    root = state_dir.resolve()
    if system.type == "file":
        target = (root / str(system.file)).resolve()
    else:  # ach — resolve the named prompt dir, then pick its file
        prompt_dir = (root / "prompts" / str(system.ach)).resolve()
        if not prompt_dir.is_relative_to(root) or not prompt_dir.is_dir():
            log.error("prompt.system.ach not hydrated under .ach-state/prompts", ach=system.ach)
            sys.exit(1)
        if system.file:
            target = (prompt_dir / str(system.file)).resolve()
        else:
            files = sorted(p for p in prompt_dir.rglob("*") if p.is_file())
            if len(files) != 1:
                log.error(
                    "prompt.system.ach needs an explicit `file:` — the prompt dir has 0 or "
                    ">1 files",
                    ach=system.ach,
                    count=len(files),
                    files=[f.name for f in files],
                )
                sys.exit(1)
            target = files[0].resolve()
    if not target.is_relative_to(root):
        log.error("prompt.system file escapes .ach-state", path=str(target))
        sys.exit(1)
    if not target.is_file():
        log.error("prompt.system file not found under .ach-state", path=str(target))
        sys.exit(1)
    return target.read_text(encoding="utf-8")


async def select_memory_wiring_async(
    memory_cfg: Memory | None,
) -> tuple[list[str], str]:
    """Resolve (mcp_servers, memory_prompt) for one invocation — HINDSIGHT only.

    Hindsight probes its endpoint per-invocation (fail-open): register the endpoint as a
    remote MCP server iff reachable, plus the '## Memory' prompt. codemem is NOT handled here
    — it is static per-agent and resolved once at boot (resolve_codemem_wiring → engine_cfg).
    """
    if not isinstance(memory_cfg, HindsightMemory):
        return [], ""

    from ach_agent.memory.adapter import prepare_memory

    mem_available, memory_prompt = await prepare_memory(memory_cfg)
    mcp_servers = [memory_cfg.hindsight.endpoint] if mem_available else []
    return mcp_servers, memory_prompt


def _make_engine_runner(
    pool: Any,
    engine_cfg: Any,
    max_invocation_seconds: int,
    terminal_output_retries: int = 1,
    memory_cfg: Any = None,
    channel_ttl: dict[str, float] | None = None,
    channels_by_name: dict[str, Any] | None = None,
    agent_name: str = "",
    memory_bank: str = "",
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

    channel_ttl: {channel_name: idle_ttl_seconds} — the wait after a conversation ends
    before the opencode server is stopped, a per-channel constant (see _CHANNEL_IDLE_TTL_S).
    Unknown channels (e.g. the --tui console) default to 0 = stop immediately. --tui pins a
    held ref so 0 never actually stops it mid-session (see the console-mode pre-warm).
    """
    from ach_agent.engine.lifecycle import run_invocation

    ttl_by_channel = channel_ttl or {}
    channels_by_name = channels_by_name or {}

    async def engine_runner(event: MessageEvent, on_kill: Callable[[], None]) -> None:
        # Build sanitized launch env — ek_ is never read into a local variable
        _sanitized = SanitizedEnv(os.environ.copy())  # noqa: F841 — used by launch

        # Resolve channel cfg early so ctx can be built before the memory probe.
        ch_cfg = channels_by_name.get(event.channel_name)
        ctx = build_template_context(
            event.payload,
            channel_name=event.channel_name,
            channel_type=getattr(ch_cfg, "type", "") or "",
            channel_source=getattr(ch_cfg, "source", "") or "",
            agent_name=agent_name,
            memory_bank=memory_bank,
            event_id=event.idempotency_key,
            session_key=event.session_key,
        )

        # Bank (hindsight) — render before the probe so the rendered value flows into both
        # the hindsight fetch and the memory section of the prompt. Probe stays BEFORE
        # pool.acquire (MEM-01/MEM-02/D-02, "Pitfall 3"). When bank has no {{ token,
        # effective_* aliases are the original values — zero behavior change.
        effective_memory_cfg = memory_cfg
        effective_bank = memory_bank
        if isinstance(memory_cfg, HindsightMemory) and "{{" in memory_cfg.hindsight.bank:
            effective_bank = render_template(memory_cfg.hindsight.bank, ctx)
            updated_hindsight = memory_cfg.hindsight.model_copy(update={"bank": effective_bank})
            effective_memory_cfg = memory_cfg.model_copy(update={"hindsight": updated_hindsight})

        # MEM-01/MEM-02/D-02: probe memory backend BEFORE pool.acquire (Pitfall 3).
        # prepare_memory never raises (fail-open contract).
        # When unavailable: MEMORY_DEGRADED incremented + WARN logged inside prepare_memory.
        mcp_servers, memory_prompt = await select_memory_wiring_async(effective_memory_cfg)

        # Build per-invocation engine config with the (dynamic) hindsight MCP server iff
        # reachable (D-02). codemem fields are static per-agent and already on engine_cfg from
        # boot — dataclasses.replace preserves them. Original engine_cfg is not mutated.
        import dataclasses

        if dataclasses.is_dataclass(engine_cfg) and not isinstance(engine_cfg, type):
            invocation_engine_cfg = dataclasses.replace(engine_cfg, mcp_servers=mcp_servers)
        else:
            # Non-dataclass (e.g. MagicMock in tests) — attach attribute directly.
            invocation_engine_cfg = engine_cfg
            invocation_engine_cfg.mcp_servers = mcp_servers

        # Project (codemem) — render after mcp_servers replace, before acquire. Keyed pool reuses
        # one agente per session_key, so codemem_project is fixed by the first event — correct
        # for a session-invariant template.
        if (
            isinstance(memory_cfg, CodememMemory)
            and "{{" in memory_cfg.codemem.project
            and dataclasses.is_dataclass(engine_cfg)
            and not isinstance(engine_cfg, type)
        ):
            rendered_project = render_template(memory_cfg.codemem.project, ctx)
            invocation_engine_cfg = dataclasses.replace(
                invocation_engine_cfg, codemem_project=rendered_project
            )

        server = await pool.acquire(event.session_key, invocation_engine_cfg)
        try:
            # CR-01: in reply mode the future MUST always be resolved (set_result or
            # set_exception), otherwise the awaiting route hangs forever. The try/except
            # sets the exception on error before re-raising.
            future = event.reply_future
            try:
                # MEM-01: append ## Memory section (summaries or unavailable note) to prompt.
                base_prompt = build_engine_prompt(
                    event,
                    channel_cfg=ch_cfg,
                    agent_name=agent_name,
                    memory_bank=effective_bank,
                )
                full_prompt = f"{base_prompt}\n\n{memory_prompt}" if memory_prompt else base_prompt
                # Free-form channels (--tui console) carry no terminal contract: return
                # the raw reply, no terminal extraction/repair (delivery_context marker).
                free_form = bool(event.delivery_context.get("free_form"))
                # Optional live-text sink (the --debug console sets this to stream the reply
                # as it's produced, so a slow trailing tool call doesn't hide the text).
                on_text = event.delivery_context.get("on_text")
                # Optional tool-lifecycle sink (the --debug console shows "⚙ running <tool>"
                # so a long-blocking tool call isn't dead air).
                on_tool = event.delivery_context.get("on_tool")
                reuse = getattr(ch_cfg, "session", "auto") != "none"
                obj = await run_invocation(
                    server=server,
                    session_id=event.session_key,
                    prompt=full_prompt,
                    terminal_retries=terminal_output_retries,
                    max_invocation_seconds=max_invocation_seconds,
                    on_kill=on_kill,
                    free_form=free_form,
                    on_text=on_text,
                    on_tool=on_tool,
                    reuse=reuse,
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
            on_fail = event.delivery_context.get("on_fail")
            if on_complete is not None or on_fail is not None:
                action = obj.get("action")
                if action == "a2a_reply" and text.strip():
                    if on_complete is not None:
                        on_complete(event.session_key, text)
                else:
                    reason = (
                        f"invalid terminal output (action={action!r}, "
                        f"empty_text={not text.strip()})"
                    )
                    if on_fail is not None:
                        on_fail(event.session_key, reason)
                return

            # Async mode: nothing to deliver. Egress already happened via the agent's
            # external MCP tool calls — the harness never posts on the model's behalf.
        finally:
            # Return the engine server to the pool. Slot release is owned by the
            # lane: its `async with` blocks free the semaphores and its finally
            # calls on_kill for queued_total. run_invocation also fires on_kill on
            # a watchdog kill; on_kill is idempotent so that double call is safe.
            # EnginePool.release(session_key, ttl_seconds) — pool tracks server by key internally.
            # TTL is a per-channel constant (0 for all v1 channels → stop on conversation end).
            try:
                await pool.release(
                    event.session_key,
                    ttl_seconds=ttl_by_channel.get(event.channel_name, 0.0),
                )
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


# Per-channel idle TTL (seconds the opencode server lingers after a conversation ends
# before it is stopped). A constant keyed by channel type — all v1 channels are 0, so the
# server stops as soon as the conversation ends. Future channels may set a non-zero linger.
# (--tui is NOT a channel: it pre-warms opencode at boot and holds it until Ctrl-C — see main.)
_CHANNEL_IDLE_TTL_S: dict[str, float] = {
    "webhook": 0.0,
    "cron": 0.0,
    "queue": 0.0,
    "a2a": 0.0,
}


def _harness_log_dir() -> Path:
    """Volatile dir for transient harness logs (e.g. the --tui attach log).

    Lives under /tmp, never the opencode HOME — harness logs are throwaway and must not
    pollute the persistent home/state tree.
    """
    d = Path("/tmp/ach-harness")
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _run_opencode_attach(
    router: Any, *, binary_path: str, port: int, ephemeral_home: Path
) -> None:
    """`--tui`: hand the terminal to opencode's native TUI, attached to our serve.

    Shells out to `opencode attach http://127.0.0.1:<port>` — opencode's own full-screen
    client driving the SAME serve process the harness pre-warmed. Egress hygiene is
    preserved: the server still routes model + MCP traffic through the localhost proxies
    that inject the ek_ (opencode never sees it). Loopback is used even when serve binds
    0.0.0.0 — the attach client is always co-located.

    Harness logging (structlog → sys.stderr, plus the serve-drain + proxy request logs)
    would corrupt opencode's alt-screen, so sys.stderr is redirected to a file for the
    session. The subprocess inherits the real terminal fds (0/1/2) at the OS level, so
    opencode renders normally; only Python-level harness logging is diverted.

    Falls back to the plain REPL if the opencode binary is not found.
    """
    import shutil

    binary = shutil.which(binary_path)
    if not binary:
        log.error(
            "opencode binary not found for attach — falling back to plain REPL",
            binary=binary_path,
        )
        await run_tui_console(router)
        return

    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "HOME": str(ephemeral_home), "TMPDIR": str(ephemeral_home)}
    log_path = _harness_log_dir() / "tui-attach.log"
    log.info("ach-agent: --tui → opencode attach", url=url, log_file=str(log_path))

    real_stderr = sys.stderr
    with open(log_path, "a", encoding="utf-8") as log_fh:
        sys.stderr = log_fh
        try:
            proc = await asyncio.create_subprocess_exec(binary, "attach", url, "--pure", env=env)
            await proc.wait()
        finally:
            sys.stderr = real_stderr


async def main(
    tui_mode: bool = False, one_shot_prompt: str | None = None, debug_mode: bool = False
) -> None:
    """Async entrypoint: load config, boot router, start channel adapters + uvicorn.

    Three launch modifiers boot the engine/proxies/hydration but IGNORE the configured
    channels (the typed/passed line IS the prompt — no terminal contract):
      - tui_mode (`--tui`): hands the terminal to opencode's native TUI attached to our
        pre-warmed serve (see _run_opencode_attach).
      - debug_mode (`--debug`): the plain stdin/stdout REPL (see run_tui_console) — the
        minimal console, easiest to pipe/debug. Takes precedence over tui_mode.
      - one_shot_prompt (`--prompt TEXT`): run a single prompt non-interactively, print
        the reply, and exit (see run_one_shot). Highest precedence.
    """
    console_mode = tui_mode or debug_mode or one_shot_prompt is not None
    config_path = os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)

    # Step 2: load config (hard-fail on schema mismatch — CFG-02)
    cfg = load_config(config_path)
    engine_home, engine_work_dir = resolve_engine_paths(cfg)
    state_dir = link_ach_state(engine_home, engine_work_dir)

    # Step 3: D-02 gate — reject unwired channel types before serving.
    # Skipped under --tui/--prompt: configured channels are ignored in console mode.
    for channel in cfg.channels if not console_mode else []:
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
    # ACH_TOKEN is REQUIRED — opencode reaches the model only through the localhost proxy,
    # which is created during hydration. A missing ek_ is hard-failed below (no model endpoint).
    ek = os.environ.get("ACH_TOKEN")
    model_base_url: str = ""
    mcp_local_urls: dict[str, str] = {}
    mcp_proxy: McpProxy | None = None
    if ek:
        manifest = await hydrate(cfg.capability.ach.base_url, ek)
        # hard-fail (sys.exit 1) if model absent; returns the entry (with its real endpoint).
        model_entry = resolve_model(manifest, cfg.model.name)
        # capability.filter.exclude — governance gate ABOVE the model. Skills are dropped
        # from the hydrated context BEFORE fetch (never downloaded); MCP servers are excluded
        # from the localhost proxy (never fronted, so opencode never discovers them).
        _exclude = cfg.capability.filter.exclude
        _exclude_skills = set(_exclude.skills)
        if _exclude_skills:
            manifest.context.skills = [
                s for s in manifest.context.skills if s.name not in _exclude_skills
            ]
            log.info("filter: skills excluded", excluded=sorted(_exclude_skills))
        await fetch_context(
            manifest.context,
            ek,
            state_dir,
            Path(engine_home) / ".config" / "opencode" / "skills",
        )
        mcp_proxy = McpProxy()
        _exclude_servers = set(_exclude.mcp_servers)
        mcp_local_urls = await mcp_proxy.start(manifest.mcp_servers, ek, exclude=_exclude_servers)
        if _exclude_servers:
            log.info("filter: mcp servers excluded", excluded=sorted(_exclude_servers))
        # Model-proxy upstream override (dev/test only — A/B a different model backend,
        # e.g. litellm direct, to isolate forwarder buffering). MODEL-ONLY: hydration + MCP
        # stay on the ACH coords above; only the model proxy's upstream + auth swap. The
        # token is injected VERBATIM as the header value (carry `Bearer ` in it if the
        # backend needs it). SECURITY: this path uses a raw provider key, NOT the ek_ — it
        # bypasses ACH governance/ek-hygiene and is for local testing, never production.
        model_up_base = os.environ.get("ACH_MODEL_BASE_URL") or cfg.capability.ach.base_url
        model_up_header = os.environ.get("ACH_MODEL_HEADER", "x-ach-key")
        model_up_token = os.environ.get("ACH_MODEL_TOKEN") or ek
        if os.environ.get("ACH_MODEL_BASE_URL") or os.environ.get("ACH_MODEL_HEADER"):
            log.info(
                "model proxy upstream override (dev/test)",
                base_url=model_up_base,
                auth_header=model_up_header,
            )
            # Catch the classic 'No api key passed in' 401: ACH_MODEL_TOKEN set but its
            # credential is empty — e.g. `Bearer ${LITELLM_API_KEY}` where LITELLM_API_KEY
            # was never exported, so it expanded to a bare scheme. Strip a leading scheme
            # word (Bearer/Token/…) + whitespace; warn if nothing is left. The credential
            # itself is NEVER logged.
            _raw = os.environ.get("ACH_MODEL_TOKEN", "")
            _cred = _raw.split(" ", 1)[1] if " " in _raw.strip() else _raw
            if _raw and not _cred.strip():
                log.warning(
                    "ACH_MODEL_TOKEN has an empty credential — only a scheme word, no key. "
                    "Likely an unexpanded ${...} var. The upstream will 401 'No api key "
                    "passed in.' Export the key in the env that launches the container.",
                    auth_header=model_up_header,
                )
        model_proxy_base = await start_model_proxy(model_up_base, model_up_token, model_up_header)
        # Use the hydrated model's REAL endpoint path (ACH may serve a gemini.* model at
        # /v1, not /gemini). Fall back to the type→prefix map only when the hydrated set is
        # empty (local dev) or carries no endpoint.
        prefix = _MODEL_ENDPOINT_PREFIX[cfg.model.type]
        if model_entry is not None and model_entry.endpoint:
            endpoint_path = urlsplit(model_entry.endpoint).path.strip("/")
            if endpoint_path:
                prefix = endpoint_path
        model_base_url = f"{model_proxy_base}/{prefix}"
        _ctx = manifest.context
        log.info(
            "hydrated + localhost proxies started",
            environment=manifest.environment,
            model_count=len(manifest.models),
            models=manifest.models,
            mcp_count=len(mcp_local_urls),
            mcp_servers=list(mcp_local_urls.keys()),
            skills=[s.name for s in _ctx.skills],
            prompts=[p.name for p in _ctx.prompts],
            artifacts=[a.name for a in _ctx.artifacts],
        )
        # Boot-time tool probe (best-effort): list the tools each MCP server actually
        # exposes for this ek and warn on empty. Surfaces an empty/unauthorized server
        # (e.g. OAuth not granted) at boot instead of letting the model discover it
        # mid-invocation and flail. Never breaks boot (list_mcp_tools swallows errors).
        from ach_agent.engine.hydrate import list_mcp_tools

        for _srv in manifest.mcp_servers:
            _tools = await list_mcp_tools(_srv.endpoint, ek)
            if _tools:
                log.info("mcp tools", server=_srv.id, count=len(_tools), tools=_tools)
            else:
                log.warning(
                    "mcp server exposes 0 tools — check ACH provisioning / OAuth consent",
                    server=_srv.id,
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

    # ACH_TOKEN (ek_) is REQUIRED: opencode always reaches the model through the localhost
    # model-proxy, which only exists once we hydrate with the ek_. Without it there is no
    # model endpoint at all (model_base_url is set only inside the `if ek:` block above).
    if not model_base_url:
        log.error(
            "no model endpoint — set ACH_TOKEN (ek_) so the harness can hydrate and front "
            "the model via the localhost proxy. opencode points only at that proxy; there "
            "is no direct-gateway fallback."
        )
        sys.exit(1)

    # Step 5: build the engine pool. Egress is the agent's via external MCP tools —
    # the harness has no delivery adapter (it never posts on the model's behalf).
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.pool import EnginePool

    # opencode `serve` always binds loopback (127.0.0.1) on a free ephemeral port the pool
    # picks — only reachable inside the container/host. `--tui` drives it via `opencode attach`
    # (co-located, loopback); nothing is published off-host.
    # codemem is static per-agent: resolve db_path + project once at boot (needs persistence
    # context). Fail-open ("","") when not codemem or the binary is absent (MEM-02/D-02).
    codemem_db_path, codemem_project = resolve_codemem_wiring(cfg)

    # Boot-static system prompt: persona + active backend's TOOLS_SPEC (appended once at boot).
    _persona = resolve_system_prompt(cfg.prompt, state_dir)
    from ach_agent.memory import tools_spec_for

    _spec = tools_spec_for(cfg.memory)
    _system_prompt = f"{_persona}\n\n## Memory Tools\n{_spec}" if _spec else _persona

    engine_cfg = EngineConfig(
        home=engine_home,
        work_dir=engine_work_dir,
        codemem_db_path=codemem_db_path,
        codemem_project=codemem_project,
        provider=cfg.model.type,
        model=cfg.model.name,
        params=cfg.model.params,
        # prompt.system = the inline agent persona + per-backend TOOLS_SPEC (boot-static).
        system_prompt=_system_prompt,
        steps=cfg.limits.max_steps,
        startup_timeout_seconds=cfg.engine.startup_timeout_seconds,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        # Idle TTL is now a per-channel constant resolved in engine_runner (see
        # _CHANNEL_IDLE_TTL_S). This field is unused by the runner and kept at 0 for
        # back-compat with EngineConfig consumers.
        shared_ttl_seconds=0,
        model_base_url=model_base_url,
        mcp_local_urls=mcp_local_urls,
        # SEC-01 / ek-hygiene: opencode's env is clean-slate (base allowlist only). Extra
        # var names the operator wants forwarded come from engine.forwardEnv.
        forward_env=cfg.engine.forward_env,
        # capability.filter.exclude.tools — disabled in opencode.json (withheld from model).
        exclude_tools=cfg.capability.filter.exclude.tools,
    )
    pool = EnginePool()

    # 6b. Build engine_runner. MEM-01/D-02: pass memory_cfg so engine_runner probes
    # before pool.acquire (Pitfall 3).
    # Per-channel idle TTL (constant by channel type; all v1 channels are 0).
    channel_ttl = {ch.name: _CHANNEL_IDLE_TTL_S.get(ch.type, 0.0) for ch in cfg.channels}
    channels_by_name = {c.name: c for c in cfg.channels}
    memory_bank = cfg.memory.hindsight.bank if isinstance(cfg.memory, HindsightMemory) else ""
    engine_runner = _make_engine_runner(
        pool=pool,
        engine_cfg=engine_cfg,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        terminal_output_retries=cfg.limits.terminal_output_retries,
        memory_cfg=cfg.memory,
        channel_ttl=channel_ttl,
        channels_by_name=channels_by_name,
        agent_name=cfg.agent.name,
        memory_bank=memory_bank,
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
        channel_concurrency={ch.name: ch.concurrency for ch in cfg.channels},
    )

    # --tui / --prompt launch modifiers: ignore the configured channels and drive the
    # engine directly. The engine + proxies + hydration are already wired above; the
    # first prompt warms the engine via the router→pool path (no HTTP A′ gate involved).
    if console_mode:
        if one_shot_prompt is not None:
            log.info("ach-agent: --prompt one-shot mode (configured channels ignored)")
        elif debug_mode:
            log.info("ach-agent: --debug plain console mode (configured channels ignored)")
        else:
            log.info("ach-agent: --tui console mode (configured channels ignored)")
        try:
            if one_shot_prompt is not None:
                await run_one_shot(router, one_shot_prompt)
            else:
                # --tui/--debug: launch opencode at boot (not lazily on the first prompt) + hold a
                # ref for the whole REPL, so per-invocation release(0) never stops it between
                # prompts — there is no idle TTL; only Ctrl-C / EOF ends the session (the
                # finally below stops it). Probe memory first so the pre-warmed server's
                # opencode.json wires the memory MCP exactly as engine_runner would.
                import dataclasses

                # codemem is already on engine_cfg from boot (static); only hindsight's
                # remote MCP server is resolved here so the pre-warmed opencode.json matches.
                warm_mcp_servers: list[str] = []
                if isinstance(cfg.memory, HindsightMemory):
                    from ach_agent.memory.adapter import prepare_memory

                    _mem_ok, _ = await prepare_memory(cfg.memory)
                    if _mem_ok:
                        warm_mcp_servers = [cfg.memory.hindsight.endpoint]
                from ach_agent.channels.tui import _CONSOLE_SESSION_KEY

                warm_codemem_project = engine_cfg.codemem_project
                if isinstance(cfg.memory, CodememMemory) and "{{" in engine_cfg.codemem_project:
                    warm_ctx = build_template_context(
                        {},
                        channel_name="tui",
                        channel_type="tui",
                        channel_source="",
                        agent_name=cfg.agent.name,
                        memory_bank="",
                        event_id="",
                        session_key=_CONSOLE_SESSION_KEY,
                    )
                    # Keyed pool reuses this warm server for the whole console session, so the
                    # project must be rendered HERE — engine_runner's later render is discarded.
                    warm_codemem_project = render_template(engine_cfg.codemem_project, warm_ctx)
                warm_cfg = dataclasses.replace(
                    engine_cfg, mcp_servers=warm_mcp_servers, codemem_project=warm_codemem_project
                )
                warm_server = await pool.acquire(_CONSOLE_SESSION_KEY, warm_cfg)
                # No stdout banner — opencode's own --print-logs already announces the
                # listening address. Keep one structured info line with the loopback address.
                log.info(
                    "ach-agent: opencode serve listening",
                    url=f"http://127.0.0.1:{warm_server.port}",
                )
                # --debug → the plain stdin/stdout REPL (minimal, pipe-friendly). --tui →
                # the full-screen Textual UI, but only on a real TTY (docker compose run
                # tty:true); piped/non-TTY stdin falls back to the plain REPL.
                if debug_mode or not sys.stdout.isatty():
                    await run_tui_console(router)
                else:
                    await _run_opencode_attach(
                        router,
                        binary_path=engine_cfg.binary_path,
                        port=warm_server.port,
                        ephemeral_home=warm_server.ephemeral_home,
                    )
        finally:
            # Stop any warm-held engine server (idle TTL may not have elapsed at EOF).
            await pool.stop_all()
            await stop_model_proxies()
            if mcp_proxy is not None:
                await mcp_proxy.stop()
            if hasattr(dedup_store, "close"):
                dedup_store.close()
        log.info("ach-agent: session ended")
        return

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

        # on_fail closure mirrors on_complete: emits a FAILED event when the terminal
        # output is unusable (action != a2a_reply, or empty reply text).
        def _make_on_fail(b: A2AAgentExecutorBridge) -> Any:
            def on_fail(session_key: str, reason: str) -> None:
                b.signal_failure(session_key, reason)

            return on_fail

        _on_fail = _make_on_fail(bridge)

        # Wrap the router to inject on_complete + on_fail into delivery_context (W9 pattern).
        class _A2AHandler:
            """Handler wrapper that injects on_complete/on_fail into delivery_context."""

            def __init__(self, rtr: Any, fn: Any, fn_fail: Any) -> None:
                self._rtr = rtr
                self._fn = fn
                self._fn_fail = fn_fail

            async def handle(self, event: MessageEvent) -> Any:
                event.delivery_context["on_complete"] = self._fn
                event.delivery_context["on_fail"] = self._fn_fail
                return await self._rtr.handle(event)

        bridge._handler = _A2AHandler(router, _on_complete, _on_fail)

        # Build the A2A AgentCard from channel config (minimal — receiver-only v1, spec §14.6).
        # make_a2a_agent_card keeps a2a.* imports inside channels/a2a.py (RTR-06 fence).
        agent_card = make_a2a_agent_card(channel.name)
        sub_app = build_a2a_app(agent_card, bridge, rpc_prefix="/")
        mount_path = f"/a2a/{channel.name}"
        a2a_mounts.append((mount_path, sub_app))
        a2a_bridges.append(bridge)
        log.info("a2a channel bridge built", channel_name=channel.name, mount_path=mount_path)

    # 6c. Create FastAPI app with all webhook channels.
    # pool=pool threads the A′ gate (DUR-02) into the webhook route — live in production.
    # a2a_mounts threads the A2A sub-apps under the same socket (topology A).
    app = create_app(
        channels=webhook_channels,
        handler=router,
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
        # Stop queue consumers BEFORE draining. Unlike HTTP/cron intake, queue consumers
        # do NOT honor the `draining` flag — they keep xreadgroup'ing redis and routing
        # events into lanes. If left running during _drain, they feed new events into lanes
        # that _drain is cancelling: those events fail admission, stay unacked, and get
        # redelivered on the next boot (redelivery churn, defeats graceful drain). Stopping
        # consumers first guarantees no new events enter lanes; events already routed are
        # still drained to completion by _drain's lane.join() below.
        for consumer in queue_consumers:
            await consumer.stop()
        # Graceful drain (DUR-03): flip readyz, drain lanes, cleanup. _drain sets
        # uv_server.should_exit so uvicorn stops accepting, then returns (no sys.exit).
        await _drain(
            state=state,
            uv_server=uv_server,
            cron_scheduler=cron_scheduler,
            router=router,
            dedup_store=dedup_store,
        )
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


def _parse_cli(argv: list[str]) -> tuple[bool, str | None, bool]:
    """Parse launch modifiers from argv.

    `--tui` → full-screen Textual UI. `--debug` → plain stdin/stdout REPL (minimal,
    pipe-friendly). `--prompt TEXT` (or `--prompt=TEXT`) → single non-interactive prompt
    then exit. All ignore the configured channels; precedence is `--prompt` > `--debug` > `--tui`.
    """
    tui = "--tui" in argv
    debug = "--debug" in argv
    prompt: str | None = None
    for i, arg in enumerate(argv):
        if arg == "--prompt" and i + 1 < len(argv):
            prompt = argv[i + 1]
            break
        if arg.startswith("--prompt="):
            prompt = arg[len("--prompt=") :]
            break
    return tui, prompt, debug


if __name__ == "__main__":
    # `--tui` / `--debug` / `--prompt` launch modifiers: drive the engine directly.
    _tui_mode, _one_shot, _debug_mode = _parse_cli(sys.argv[1:])
    asyncio.run(main(tui_mode=_tui_mode, one_shot_prompt=_one_shot, debug_mode=_debug_mode))

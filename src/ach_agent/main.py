# SPDX-License-Identifier: Apache-2.0
"""ach-agent entrypoint — bootstrap wiring.

Boot order (CRITICAL — Pitfall 8: configure_logging FIRST, before any import
that may emit a log line):
  1. configure_logging()        <- SEC-01: redact_ek_processor installed first
  2. load_config(path)          <- hard-fail on schema mismatch (CFG-02)
  3. D-02 gate: reject unwired channel types (hard-fail, non-zero exit)
  4. Write PID file             <- Pitfall 11: single-replica guard
  5. Construct Router
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

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn

if TYPE_CHECKING:
    from ach_agent.engine.base.driver import EngineDriver
    from ach_agent.engine.events import OpenCodeToolUpdate
    from ach_agent.engine.hydrate import McpServer

from ach_agent.channels.a2a import A2AAgentExecutorBridge, build_a2a_app, make_a2a_agent_card
from ach_agent.channels.cron import CronScheduler
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.queue import QueueConsumer
from ach_agent.channels.tui import run_one_shot, run_tui_console
from ach_agent.config import load_config
from ach_agent.config.schema import (
    CodememMemory,
    HindsightMemory,
    LocalMcpServer,
    McpServerConfig,
    Memory,
    RemoteMcpServer,
    RepoCheckoutParams,
    RepoCheckoutServer,
)
from ach_agent.engine.context import fetch_context
from ach_agent.engine.hydrate import hydrate, resolve_model
from ach_agent.engine.mcp_passthrough import to_opencode_entry
from ach_agent.engine.mcp_proxy import McpProxy, start_model_proxy, stop_model_proxies
from ach_agent.engine.metrics import DRAIN_COMPLETED, ENGINE_LAUNCH_FAILURES
from ach_agent.engine.sanitized_env import add_secret_redaction, configure_logging
from ach_agent.http.app import create_app
from ach_agent.memory.facade import MemoryFacade
from ach_agent.memory.hindsight import prepare_memory, provision_memory
from ach_agent.router import Router
from ach_agent.security.preflight import run_preflight
from ach_agent.templating import build_template_context, render_template

# configure_logging() is called at module TOP (not in main()) so that any
# log emission during import (e.g. validation warnings) is already redacted.
# Must be the FIRST executable statement (Pitfall 8 / SEC-01).
configure_logging()

log = structlog.get_logger(__name__)

# D-02: only channel types wired in this build
WIRED_CHANNEL_TYPES: frozenset[str] = frozenset({"cron", "webhook", "a2a", "queue"})

# model.type → ACH compat-endpoint path prefix fronted by the model proxy. opencode's
# provider baseURL becomes "http://127.0.0.1:<port>/<prefix>". Each type hits its NATIVE wire:
# openai → /v1 (chat/completions), gemini → /gemini/v1beta (generateContent), anthropic →
# /anthropic (messages). The type is authoritative — the harness does NOT round-trip a gemini
# model through the OpenAI wire (that leaks gemini thought-signatures into tool_call ids).
_MODEL_ENDPOINT_PREFIX: dict[str, str] = {
    "openai": "v1",
    "gemini": "gemini/v1beta",
    "anthropic": "anthropic",
}

CONFIG_PATH_ENV = "ACH_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "/etc/ach-agent/config.json"
PID_FILE = Path("/tmp/ach-agent.pid")


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
    persistence.enabled=true  → FileBackedDedupStore on mountPath/state/state.db
      (shared harness sqlite; dedup is its first table, more may follow).
      Missing / non-writable mount → sys.exit(1) fail-closed (D-04a,
      mirrors ENG-06 poll_ready exit pattern).
      Corrupt state.db → fail-open: move aside, start fresh, WARN +
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

    db_path = mount / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

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
                "state.db corrupt — moved aside, starting fresh (fail-open)",
                db_path=str(db_path),
                aside_path=str(aside_path),
                error=str(exc),
            )
        except OSError as rename_exc:
            log.warning(
                "state.db corrupt and could not be moved aside — retrying fresh store",
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


def _open_session_store(cfg: Any) -> Any:
    """Select the pool's session_key → opencode-session map per persistence config.

    persistence.enabled=false → in-memory _LRUSessionMap (volatile, current behavior).
    persistence.enabled=true  → _SqliteSessionMap on mountPath/state/state.db, so
      channel.session='auto' continuity survives a full harness restart; bounded to
      maxsize rows (LRU by last_used).

    Fail-OPEN (unlike _open_dedup_store, which fail-CLOSES): a missing mount or a DB
    error degrades to the in-memory map + WARN + PERSISTENCE_DEGRADED, because losing
    conversational continuity is a soft degrade, not a duplicate-firing hazard.

    Call AFTER _open_dedup_store: that opens/repairs state.db first, so this second WAL
    connection just adds the oc_sessions table to an already-valid file.
    """
    from ach_agent.engine.pool import _LRUSessionMap, _SqliteSessionMap
    from ach_agent.router.metrics import PERSISTENCE_DEGRADED

    if not cfg.persistence.enabled:
        return _LRUSessionMap()

    db_path = Path(cfg.persistence.mount_path) / "state" / "state.db"
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = _SqliteSessionMap(db_path)
        log.info("durable session map opened", db_path=str(db_path), rows=len(store))
        return store
    except Exception as exc:  # noqa: BLE001 — fail-open to in-memory (degraded, not fatal)
        log.warning(
            "session map open failed — using in-memory (fail-open)",
            db_path=str(db_path),
            error=str(exc),
        )
        PERSISTENCE_DEGRADED.inc()
        return _LRUSessionMap()


def _clean_tool_name(name: str) -> str:
    """Collapse opencode's doubled MCP prefix for readability.

    opencode ids MCP tools as ``<server>_<server>_<tool>`` (the server segment repeats,
    e.g. ``mcp-gitlab-ro_mcp-gitlab-ro_gitlab_get_merge_request``). Render it as
    ``<server>/<tool>``. Native tools (``grep``, ``bash``) have no such prefix and pass through.
    """
    parts = name.split("_", 2)
    if len(parts) == 3 and parts[0] == parts[1]:
        return f"{parts[0]}/{parts[2]}"
    return name


def _tool_detail(raw: str) -> str:
    """Best-effort decode of a tool result for readable logging.

    gitlab-mcp (and friends) return ``{"result": "<json-string>"}`` — doubly JSON-encoded,
    which structlog then repr-escapes into an unreadable ``{\\n \\"...`` blob. Parse it, unwrap
    a lone ``result`` string, and re-dump compact single-line JSON. Non-JSON output (file
    reads, truncation notices) falls through to the raw text. Always truncated to 300 chars.
    """
    text = raw.strip()
    try:
        obj = json.loads(text)
    except (ValueError, TypeError):
        return text[:300]
    if isinstance(obj, dict) and list(obj) == ["result"] and isinstance(obj["result"], str):
        try:
            obj = json.loads(obj["result"])
        except (ValueError, TypeError):
            obj = obj["result"]
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))[:300]


def _log_engine_tool(update: OpenCodeToolUpdate) -> None:
    """Default on_tool sink for channel invocations.

    run_invocation calls this as each tool moves running→completed/error. Wired only when
    the channel provides no on_tool of its own (--debug/console keep their own streaming
    sinks), so a channel turn shows the tools it ran — the action and its result — instead
    of dead air. The ``running`` transition is skipped so each tool logs ONCE (on
    completed/error); the result is JSON-decoded for readability and both fields are bounded.
    """
    state = update.state
    if state.status == "running":
        return  # one line per tool — the completed/error transition carries the result
    fields: dict[str, Any] = {
        "tool": _clean_tool_name(update.tool_name),
        "status": state.status,
    }
    action = getattr(state, "title", "")
    if action:
        fields["action"] = action[:200]
    detail = getattr(state, "output", "") or getattr(state, "error", "")
    if detail:
        fields["detail"] = _tool_detail(detail)
    log.info("engine: tool", **fields)


def _make_tool_recorder(
    inner: Callable[[OpenCodeToolUpdate], None],
    tool_sink: Any,
    event: MessageEvent,
    model: str,
) -> Callable[[OpenCodeToolUpdate], None]:
    """Wrap an on_tool sink to also record one ToolStat per tool call (Tier 1 agent trace).

    Stamps a monotonic start on the ``running`` transition; on the ``completed``/``error``
    transition computes the duration and records once per call_id, then delegates to ``inner``
    (the channel's sink or _log_engine_tool). Per-invocation state — a fresh map each turn.
    """
    from ach_agent.stats.sink import build_tool_stat

    starts: dict[str, float] = {}
    done: set[str] = set()
    source = getattr(event, "source", event.channel_name)

    def on_tool(update: OpenCodeToolUpdate) -> None:
        cid = update.call_id or update.part_id
        status = update.state.status
        if status == "running":
            starts.setdefault(cid, time.monotonic())
        elif status in ("completed", "error") and cid not in done:
            done.add(cid)
            start = starts.pop(cid, None)
            # ponytail: duration from SSE arrival (running→terminal), not opencode's own tool
            # clock — needs the running event; missing it → duration None (count still recorded).
            dur_ms = int((time.monotonic() - start) * 1000) if start is not None else None
            display = _clean_tool_name(update.tool_name)
            tool_type = "mcp" if "/" in display else "builtin"
            tool_sink.record(
                build_tool_stat(
                    update,
                    session_key=event.session_key,
                    channel=event.channel_name,
                    source=source,
                    model=model,
                    tool=display,
                    tool_type=tool_type,
                    duration_ms=dur_ms,
                    ts_ms=int(time.time() * 1000),
                )
            )
        inner(update)

    return on_tool


def _checkout_hint(project_id: Any, head_sha: str) -> str:
    return (
        f" You can copy the repo locally for deep analysis: "
        f"checkout_repo(project={project_id}, ref={head_sha}) — returns a path with the full "
        f"tree for rg/tests/build (read-only snapshot, no .git)."
    )


def build_engine_prompt(
    event: MessageEvent,
    channel_cfg: Any = None,
    agent_name: str = "",
    memory_bank: str = "",
    repo_checkout_enabled: bool = False,
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

    # Webhook path: build prompt from delivery_context + payload, per event kind.
    # Missing "kind" defaults to merge_request (back-compat with pre-Task-1 events).
    dc = event.delivery_context
    project_id = dc.get("project_id", "")
    kind = dc.get("kind", "merge_request")

    obj_attrs: dict[str, Any] = {}
    raw_obj_attrs = event.payload.get("object_attributes")
    if isinstance(raw_obj_attrs, dict):
        obj_attrs = raw_obj_attrs

    if kind == "note":
        # A comment on an MR or issue: give the agent the note body + the target reference
        # so it can fetch context via MCP. Never emit an empty "Review MR !." line.
        target_type = dc.get("target_type", "")
        if target_type == "issue":
            ref = f"issue #{dc.get('issue_iid', '')}"
        else:
            ref = f"MR !{dc.get('mr_iid', '')}"
        raw_user = event.payload.get("user")
        user = raw_user.get("username", "") if isinstance(raw_user, dict) else ""
        note = obj_attrs.get("note", "")
        header = f"New comment on {ref} in project {project_id}"
        header = f"{header} by {user}:" if user else f"{header}:"
        parts = [header]
        if note:
            parts.append(str(note))
        head_sha = dc.get("head_sha", "")
        if repo_checkout_enabled and head_sha and target_type != "issue":
            parts.append(_checkout_hint(project_id, str(head_sha)))
        return " ".join(parts)

    title = obj_attrs.get("title", "")
    description = obj_attrs.get("description", "")

    if kind == "issue":
        issue_iid = dc.get("issue_iid", "")
        parts = [f"Review issue #{issue_iid} in project {project_id}."]
    else:  # merge_request (default)
        mr_iid = dc.get("mr_iid", "")
        parts = [f"Review MR !{mr_iid} in project {project_id}."]
    if title:
        parts.append(f"Title: {title}")
    if description:
        parts.append(f"Description: {description}")

    head_sha = dc.get("head_sha", "")
    if repo_checkout_enabled and head_sha and kind != "issue":
        parts.append(_checkout_hint(project_id, str(head_sha)))
    return " ".join(parts)


# Harness-owned terminal-contract directive, appended per channel class to every
# structured turn (NOT free-form tui). The terminal JSON envelope is harness IP — the
# harness parses it — so operators never hand-write it in channel.prompt. This is the
# ONLY per-turn place the model is told which action its final object must carry; without
# it a model can emit a valid-but-wrong {"action":"none"} on an a2a turn, which
# extract_terminal accepts and the a2a path (main.py) then delivers to the caller as a
# FAILURE (on_fail). See CONTRACT_v3 §8.
#
# Each block exposes ONLY the action its channel class expects — the a2a block never
# names "none" (naming the wrong action just plants it: pink-elephant). The matching
# lifecycle repair/wrap turns are kept action-consistent via run_invocation(terminal_action=…).
A2A_OUTPUT_INSTRUCTIONS = (
    "<output_format>\n"
    "Do your reasoning and tool work first. Then END your reply with exactly one compact "
    "JSON object on its own line — this object is the ONLY thing delivered to the caller:\n"
    '{"action":"a2a_reply","text":"<RESULT>","thoughts":"<OPTIONAL>"}\n'
    '"text" is what the caller reads — keep it non-empty. Single line, keys in this order, '
    "no code fences.\n"
    "</output_format>"
)

NONE_OUTPUT_INSTRUCTIONS = (
    "<output_format>\n"
    "Do your reasoning and tool work first. Then END your reply with exactly one compact "
    "JSON object on its own line:\n"
    '{"action":"none","text":"<SUMMARY>","thoughts":"<OPTIONAL>"}\n'
    "Do all real work through your tools; this object only reports completion. Single line, "
    "keys in this order, no code fences.\n"
    "</output_format>"
)


def terminal_action_for(channel_cfg: Any, free_form: bool) -> str:
    """The terminal action this turn's channel class expects — the single source of truth
    the harness reuses for both the up-front <output_format> block and the lifecycle
    repair/wrap turns. a2a → 'a2a_reply'; every other class (and a missing type) → 'none'.
    free_form (--tui) has no contract and skips extraction, so its value is unused ('none').
    """
    if not free_form and getattr(channel_cfg, "type", None) == "a2a":
        return "a2a_reply"
    return "none"


def build_output_instructions(channel_cfg: Any, free_form: bool) -> str:
    """Return the harness-owned <output_format> block for this turn, or "".

    free_form (--tui console) → "" (no terminal contract). Otherwise the block for the
    channel class's expected action (terminal_action_for): a2a_reply for a2a, none else.
    """
    if free_form:
        return ""
    if terminal_action_for(channel_cfg, free_form) == "a2a_reply":
        return A2A_OUTPUT_INSTRUCTIONS
    return NONE_OUTPUT_INSTRUCTIONS


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


def collect_passthrough_mcp(
    mcp_servers: dict[str, McpServerConfig],
) -> dict[str, dict[str, object]]:
    """Normalize every local/remote entry to an opencode.json mcp.<name> value.

    repoCheckout entries are skipped — the harness hosts those itself (facade), they are not
    passed through to opencode.
    """
    out: dict[str, dict[str, object]] = {}
    for name, spec in mcp_servers.items():
        if isinstance(spec, (LocalMcpServer, RemoteMcpServer)):
            out[name] = to_opencode_entry(spec)
    return out


def find_repo_checkout(
    mcp_servers: dict[str, McpServerConfig],
) -> tuple[str, RepoCheckoutParams] | None:
    """The (name, params) of the repoCheckout entry, or None.

    ponytail: one repoCheckout facade per agent (the only real case). If several are declared,
    take the first and WARN — supporting N facades is unneeded plumbing until asked.
    """
    found: tuple[str, RepoCheckoutParams] | None = None
    for name, spec in mcp_servers.items():
        if isinstance(spec, RepoCheckoutServer):
            if found is not None:
                log.warning("multiple repoCheckout mcpServers — using first", ignored=name)
                continue
            found = (name, spec.repo_checkout)
    return found


def resolve_repo_archive_endpoint(mcp_servers: list[McpServer], server_id: str) -> str | None:
    """The endpoint of the hydrated McpServer whose id == server_id, or None."""
    for s in mcp_servers:
        if s.id == server_id:
            return s.endpoint
    return None


async def select_memory_wiring_async(
    memory_cfg: Memory | None,
    facade_url: str | None,
) -> tuple[list[str], str]:
    """Probe memory + build the prompt section; return the FACADE url (not the raw endpoint).

    The agent only ever reaches Hindsight through the harness facade, so the mcp_servers list
    carries the facade URL. Gated by prepare_memory's probe (D-02 fail-open) AND by the facade
    actually being up. codemem is NOT handled here — it is static per-agent and resolved once
    at boot (resolve_codemem_wiring → engine_cfg).
    """
    if not isinstance(memory_cfg, HindsightMemory):
        return [], ""

    mem_available, memory_prompt = await prepare_memory(memory_cfg)
    mcp_servers = [facade_url] if (mem_available and facade_url) else []
    return mcp_servers, memory_prompt


def _make_engine_runner(
    pool: Any,
    driver: EngineDriver,
    engine_cfg: Any,
    max_invocation_seconds: int,
    terminal_output_retries: int = 1,
    max_tool_calls: int = 0,
    memory_cfg: Any = None,
    channel_ttl: dict[str, float] | None = None,
    channels_by_name: dict[str, Any] | None = None,
    agent_name: str = "",
    memory_bank: str = "",
    stats_sink: Any = None,
    tool_sink: Any = None,
    memory_facade_url: str | None = None,
    repo_facade_url: str | None = None,
    a2a_facade_url: str | None = None,
) -> Callable[..., Any]:
    """Build the engine_runner callable injected into the Router.

    The runner is called by Lane as: engine_runner(event, on_kill).
    It acquires a ManagedServer from the pool, calls run_contract_turn(driver, ...)
    (which returns the single terminal object), then relays the terminal `text`:

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

    The subprocess launch env is built by build_opencode_env (SEC-01): the
    engine_cfg carries paths, never ek_ values.

    memory_cfg (MEM-01/MEM-02/D-02): optional MemoryBlock from config.memory.
    When present, prepare_memory is called BEFORE pool.acquire so the opencode.json
    written for that server includes or excludes the memory MCP server (Pitfall 3).
    Fail-open: unreachable backend → exclude MCP server, log WARN + metric, run anyway.

    channel_ttl: {channel_name: idle_ttl_seconds} — the wait after a conversation ends
    before the opencode server is stopped, built at boot from engine.idle_ttl_seconds.
    Unknown channels (e.g. the --tui console) default to 0 = stop immediately. --tui pins a
    held ref so 0 never actually stops it mid-session (see the console-mode pre-warm).
    """
    from ach_agent.engine.base.terminal import run_contract_turn
    from ach_agent.engine.events import InvocationTimeout
    from ach_agent.stats.sink import build_session_stat

    ttl_by_channel = channel_ttl or {}
    channels_by_name = channels_by_name or {}

    async def engine_runner(event: MessageEvent, on_kill: Callable[[], None]) -> None:
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

        # MEM-01/MEM-02/D-02: probe memory backend BEFORE pool.acquire (Pitfall 3).
        # prepare_memory never raises (fail-open contract).
        # When unavailable: MEMORY_DEGRADED incremented + WARN logged inside prepare_memory.
        # bank is static (T-04-03, schema-enforced: no {{ }}) — the mental-model fetch, the
        # boot-started facade, and the prompt's {{ memory.bank }} all use the SAME value, so
        # there is no per-event bank rendering to keep them in sync.
        mcp_servers, memory_prompt = await select_memory_wiring_async(memory_cfg, memory_facade_url)
        # The repo-checkout facade (if enabled) is a static localhost MCP server; append it to
        # every invocation alongside the (dynamic) memory facade — the agent reaches gitlab-mcp's
        # archive resource ONLY through it (ek injected harness-side).
        if repo_facade_url:
            mcp_servers = [*mcp_servers, repo_facade_url]
        # a2a egress facade (SP1 §6): a static localhost MCP server carried on every invocation,
        # so the agent can call peer agents. Same wiring as the memory/repo facades.
        if a2a_facade_url:
            mcp_servers = [*mcp_servers, a2a_facade_url]

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

        # CR-01: in reply mode the future MUST always be resolved (set_result or
        # set_exception), otherwise the awaiting route hangs forever. The except branches
        # below resolve it on every failure path.
        future = event.reply_future
        # on_fail (a2a) MUST be signalled on every failure path too — otherwise the a2a
        # executor's completion.wait() (no timeout) hangs forever. Read it here so the
        # success branch AND the except branches below all resolve it.
        on_fail = event.delivery_context.get("on_fail")
        server = None
        timed_out = False
        acquired = False
        try:
            server = await pool.acquire(event.session_key, invocation_engine_cfg)
            acquired = True
            # MEM-01: append ## Memory section (summaries or unavailable note) to prompt.
            base_prompt = build_engine_prompt(
                event,
                channel_cfg=ch_cfg,
                agent_name=agent_name,
                memory_bank=memory_bank,
                # Advertise checkout_repo only when the facade is actually wired (started),
                # not merely config-enabled — else we'd hint a tool the agent can't call.
                repo_checkout_enabled=repo_facade_url is not None,
            )
            full_prompt = f"{base_prompt}\n\n{memory_prompt}" if memory_prompt else base_prompt
            # Free-form channels (--tui console) carry no terminal contract: return
            # the raw reply, no terminal extraction/repair (delivery_context marker).
            free_form = bool(event.delivery_context.get("free_form"))
            # Harness-owned terminal-contract directive, per channel class. Appended LAST
            # (after the message + any memory block) so it wins on recency; tui gets none.
            # The same action drives the lifecycle repair/wrap turns (terminal_action below)
            # so an a2a repair turn never re-exposes 'none'.
            _terminal_action = terminal_action_for(ch_cfg, free_form)
            _output_instructions = build_output_instructions(ch_cfg, free_form)
            if _output_instructions:
                full_prompt = f"{full_prompt}\n\n{_output_instructions}"
            # Optional live-text sink (the --debug console sets this to stream the reply
            # as it's produced, so a slow trailing tool call doesn't hide the text).
            on_text = event.delivery_context.get("on_text")
            # Optional tool-lifecycle sink (the --debug console shows "⚙ running <tool>"
            # so a long-blocking tool call isn't dead air).
            on_tool = event.delivery_context.get("on_tool")
            # Default observability sink: channels wire no on_tool (only --debug does), so
            # without this a channel turn shows nothing about the tools it ran.
            if on_tool is None:
                on_tool = _log_engine_tool
            # Tier 1 agent trace: record one ToolStat per tool call (metrics always; ach:tools
            # stream when ACH_STATS_REDIS_URL is set). Wraps whatever on_tool renders/logs.
            if tool_sink is not None:
                on_tool = _make_tool_recorder(on_tool, tool_sink, event, engine_cfg.model)
            # Conversation identity (session block). The router lane key
            # (event.session_key) is NOT affected — only which opencode session
            # this turn reuses. No ch_cfg (--tui console) → auto: REPL continuity.
            session_cfg = getattr(ch_cfg, "session", None)
            conv_key = event.session_key
            if session_cfg is None or session_cfg.type == "auto":
                reuse = True
            elif session_cfg.type == "none":
                reuse = False
            else:  # custom: render the key template per event (validator guarantees key set)
                tmpl = session_cfg.key or ""
                rendered = render_template(tmpl, ctx).strip()
                if rendered:
                    conv_key, reuse = rendered, True
                else:
                    log.warning(
                        "session: template rendered empty — falling back to none",
                        channel=event.channel_name,
                        template=tmpl,
                    )
                    reuse = False
            log.info(
                "engine: prompt",
                channel=event.channel_name,
                session_key=event.session_key,
                prompt=full_prompt,
            )
            turn_stats: dict[str, Any] = {}
            obj = await run_contract_turn(
                driver,
                server,
                conv_key=conv_key,
                prompt=full_prompt,
                reuse=reuse,
                sessions=pool.sessions,
                free_form=free_form,
                terminal_action=_terminal_action,
                terminal_retries=terminal_output_retries,
                on_text=on_text,
                on_tool=on_tool,
                max_tool_calls=max_tool_calls,
                stats=turn_stats,
            )

            text = str(obj.get("text", ""))
            log.info(
                "engine: response",
                channel=event.channel_name,
                session_key=event.session_key,
                action=obj.get("action"),
                text=text,
            )
            _usage = turn_stats.get("usage")
            log.info(
                "engine: summary",
                channel=event.channel_name,
                session_key=event.session_key,
                tools=turn_stats.get("tool_count", 0),
                input_tokens=getattr(_usage, "input_tokens", 0),
                output_tokens=getattr(_usage, "output_tokens", 0),
                cost_usd=getattr(_usage, "cost", 0.0),
                duration_ms=getattr(_usage, "duration_ms", 0),
            )
            # Post-turn session hygiene. Skipped on timeout (this code is not reached
            # when the lane cancels the turn) — that orphan is accepted, the
            # server is force-killed anyway.
            _sid = turn_stats.get("session_ref", "")
            if _sid and not reuse:
                # key='none' (or empty template render): stateless turn leaves no residue.
                await driver.discard_session(server, _sid)
            elif (
                _sid
                and session_cfg is not None
                and session_cfg.max_tokens is not None
                and getattr(_usage, "input_tokens", 0) > session_cfg.max_tokens
            ):
                if session_cfg.overflow == "compact":
                    log.info(
                        "session: maxTokens exceeded — compacting",
                        session_key=event.session_key,
                        session_ref=_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    await driver.compact_session(server, _sid)
                else:  # rotate: drop the map entry + delete the old session (clean)
                    log.info(
                        "session: maxTokens exceeded — rotating",
                        session_key=event.session_key,
                        session_ref=_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    pool.sessions.pop(conv_key, None)
                    await driver.discard_session(server, _sid)
            if stats_sink is not None:
                stats_sink.record(
                    build_session_stat(
                        event,
                        obj,
                        turn_stats,
                        model=engine_cfg.model,
                        ts_ms=int(time.time() * 1000),
                    )
                )

            if future is not None:
                # Reply mode: resolve the future the route is awaiting.
                if not future.done():
                    future.set_result(text)
                return

            # A2A completion path (W9 — engine_runner does NOT import channels.a2a):
            # The on_complete callable is injected by the A2A wiring closure in main.py
            # into event.delivery_context['on_complete'] before handler.handle() is called.
            on_complete = event.delivery_context.get("on_complete")
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
            return
        except asyncio.CancelledError:
            # The lane's maxInvocationSeconds deadline (or a shutdown) cancelled us.
            # Force-kill the runaway (finally releases with ttl=0) so a warm TTL is never
            # armed on a timed-out server, and release the awaiting caller so it can't hang.
            timed_out = True
            if future is not None and not future.done():
                future.set_exception(InvocationTimeout(max_invocation_seconds))
            if on_fail is not None:
                on_fail(event.session_key, f"invocation timed out after {max_invocation_seconds}s")
            raise
        except Exception as exc:
            if not acquired:
                # pool.acquire itself failed — the agente could not be launched for
                # this session_key. Explicit metric + WARN (no silent drop): acceptance
                # is decoupled from engine readiness, so this is where a launch failure
                # first surfaces. Never log ek_/tokens — session_key + error string only.
                ENGINE_LAUNCH_FAILURES.inc()
                log.warning(
                    "engine: launch failed (pool.acquire)",
                    session_key=event.session_key,
                    task_id=event.task_id,
                    error=str(exc),
                )
            if future is not None and not future.done():
                future.set_exception(exc)
            if on_fail is not None:
                on_fail(event.session_key, f"engine failure: {exc}")
            raise
        finally:
            # Return the engine server to the pool. Slot release is owned by the lane:
            # its `async with` blocks free the semaphores and its finally calls on_kill
            # for queued_total. A timed-out invocation ALWAYS releases with ttl=0 (force
            # kill of the runaway); otherwise the channel's warm idle TTL is applied so
            # session:auto persists the server across events. `if server is not None`
            # guards a cancel during a cold-start acquire.
            if server is not None:
                ttl = 0.0 if timed_out else ttl_by_channel.get(event.channel_name, 0.0)
                try:
                    await pool.release(event.session_key, ttl_seconds=ttl)
                except Exception as exc:  # noqa: BLE001
                    log.warning("pool release error", task_id=event.task_id, error=str(exc))

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


class _A2AHandler:
    """Router wrapper injecting on_complete/on_fail into delivery_context (W9 pattern)."""

    def __init__(self, rtr: Any, fn: Any, fn_fail: Any) -> None:
        self._rtr = rtr
        self._fn = fn
        self._fn_fail = fn_fail

    async def handle(self, event: MessageEvent) -> Any:
        event.delivery_context["on_complete"] = self._fn
        event.delivery_context["on_fail"] = self._fn_fail
        return await self._rtr.handle(event)


def _harness_log_dir() -> Path:
    """Volatile dir for transient harness logs (e.g. the --tui attach log).

    Lives under /tmp, never the opencode HOME — harness logs are throwaway and must not
    pollute the persistent home/state tree.
    """
    d = Path("/tmp/ach-harness")
    d.mkdir(parents=True, exist_ok=True)
    return d


def collect_secret_env_names(cfg: Any) -> list[str]:
    """Every secret.env name across webhook + a2a channel auth + the memory admin secret."""
    names: list[str] = []
    for ch in cfg.channels:
        wh = getattr(ch, "webhook", None)
        if wh is not None and wh.auth.secret is not None and wh.auth.secret.env:
            names.append(wh.auth.secret.env)
        a2a = getattr(ch, "a2a", None)
        if a2a is not None and a2a.auth.secret is not None and a2a.auth.secret.env:
            names.append(a2a.auth.secret.env)
    # memory.hindsight.auth: the admin secret joins the same forwardEnv-strip + log-redaction
    # path as channel secrets. No-auth memory config → nothing appended.
    mem = getattr(cfg, "memory", None)
    if isinstance(mem, HindsightMemory) and mem.hindsight.auth is not None:
        names.append(mem.hindsight.auth.env)
    return names


def strip_forwarded_secrets(cfg: Any) -> list[str]:
    """Fail-SAFE: remove any secret.env name from engine.forwardEnv so a misconfig can never
    leak the secret into opencode's env. Returns the cleaned forward-env list; logs a WARN for
    each stripped name (operator agreement: strip + warn, NOT hard-fail).
    """
    secret_names = set(collect_secret_env_names(cfg))
    cleaned: list[str] = []
    stripped: list[str] = []
    for name in cfg.engine.forward_env:
        (stripped if name in secret_names else cleaned).append(name)
    if stripped:
        log.warning(
            "secret env name(s) present in engine.forwardEnv — stripped so they never reach the "
            "agent (fix the config)",
            names=sorted(stripped),
        )
    return cleaned


async def _run_opencode_attach(
    router: Any,
    *,
    binary_path: str,
    port: int,
    ephemeral_home: Path,
    config_path: Path | None = None,
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
    env = {**os.environ, "HOME": str(ephemeral_home), "TMPDIR": "/tmp"}
    if config_path is not None:
        env["OPENCODE_CONFIG"] = str(config_path)
    log_path = _harness_log_dir() / "tui-attach.log"
    log.info("ach-agent: --tui → opencode attach", url=url, log_file=str(log_path))

    real_stderr = sys.stderr
    with open(log_path, "a", encoding="utf-8") as log_fh:
        sys.stderr = log_fh
        try:
            proc = await asyncio.create_subprocess_exec(binary, "attach", url, "--pure", env=env)
            # opencode owns the terminal and quits on Ctrl+C itself. Both processes share the
            # foreground process group, so terminal SIGINT hits us too — ignore it here (AFTER
            # spawn, so the child inherited the default disposition) or it cancels main() mid
            # proc.wait(), tears through the shutdown cleanup below, and leaves aiohttp sessions
            # unclosed. Not restored on purpose: mashing Ctrl+C during the post-attach cleanup
            # would re-cancel it. The process is exiting right after — nothing else needs SIGINT.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
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
    # SEC: harden this process (dumpable=0 + no_new_privs) and refuse an unsafe host
    # BEFORE the opencode peer is spawned and before ek_ is read into a Python local
    # below (dumpable=0 also reowns the ek_ already in /proc/self/environ). Fail-closed
    # unless ACH_INSECURE_ALLOW_DEGRADED=1. See security/preflight.py.
    run_preflight()
    console_mode = tui_mode or debug_mode or one_shot_prompt is not None
    config_path = os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)

    # Step 2: load config (hard-fail on schema mismatch — CFG-02)
    cfg = load_config(config_path)
    # SEC: secret.env values must never reach opencode's env or the logs. Strip any
    # secret.env name a misconfig also listed in engine.forwardEnv (fail-safe, WARN not
    # hard-fail — see strip_forwarded_secrets), and register the secret names' CURRENT
    # values for generic log redaction (the hardcoded ek_/GITLAB_TOKEN processors don't
    # catch arbitrary secret.env NAMES).
    effective_forward_env = strip_forwarded_secrets(cfg)
    add_secret_redaction(collect_secret_env_names(cfg))
    engine_home, engine_work_dir = resolve_engine_paths(cfg)
    state_dir = link_ach_state(engine_home, engine_work_dir)

    # Boot-once memory provisioning (ensure bank + mental models). Fail-open (never raises).
    await provision_memory(cfg.memory)

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
    # Harness-hosted memory facade: the agent reaches Hindsight ONLY through this localhost
    # MCP server (bank_id + admin auth injected). Started beside the proxies below; None when
    # memory is not a Hindsight config or its auth env is unset (fail-open, run without memory).
    memory_facade: MemoryFacade | None = None
    memory_facade_url: str | None = None
    # Harness-hosted repo-checkout facade: exposes `checkout_repo`, reading gitlab-mcp's archive
    # resource harness-side (ek as x-ach-key, never seen by the agent). None when disabled or the
    # gitlab endpoint/ek is missing (fail-open, run without the tool). Declared here so shutdown
    # can stop it even though it is constructed inside the `if ek:` block below.
    repo_facade: Any = None
    repo_facade_url: str | None = None
    a2a_facade: Any = None
    a2a_facade_url: str | None = None
    if ek:
        manifest = await hydrate(cfg.capability.ach.base_url, ek)
        # hard-fail (sys.exit 1) if the configured model is absent from the hydrated set.
        resolve_model(manifest, cfg.model.name)
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
        # Start the memory facade beside the proxies. The agent points at THIS url, never at
        # Hindsight. Uses the admin secret (NOT the ek_); secret may be None (internal URL).
        if isinstance(cfg.memory, HindsightMemory):
            from ach_agent.memory.hindsight import resolve_memory_secret

            _ok, _mem_secret = resolve_memory_secret(cfg.memory.hindsight)
            if _ok:
                memory_facade = MemoryFacade(
                    cfg.memory.hindsight.endpoint, _mem_secret, cfg.memory.hindsight.bank
                )
                memory_facade_url = await memory_facade.start()
            else:
                log.warning(
                    "memory: auth configured but env unset — facade not started; "
                    "running without memory"
                )
        # Start the repo-checkout facade beside the proxies (mcpServers type=repoCheckout).
        # It fronts gitlab-mcp's archive resource with the ek_ (x-ach-key), so the agent gets a
        # local checkout without ever seeing the ek_ or the raw endpoint.
        _rc = find_repo_checkout(cfg.mcp_servers)
        if _rc is not None:
            _rc_name, _rc_params = _rc
            gl_endpoint = resolve_repo_archive_endpoint(
                manifest.mcp_servers, _rc_params.source_mcp_server_id
            )
            if gl_endpoint:
                from ach_agent.engine.repo_facade import RepoCheckoutFacade

                repo_facade = RepoCheckoutFacade(
                    gl_endpoint, ek, _rc_params.tmp_base, _rc_params.ttl_seconds
                )
                repo_facade_url = await repo_facade.start()
            else:
                log.warning(
                    "repoCheckout: source mcp server not in manifest — tool not wired",
                    source_mcp_server_id=_rc_params.source_mcp_server_id,
                )
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
        # model.type is authoritative for the wire. The hydration manifest reports every model
        # at /v1 (openai-compat) even for gemini, so we DON'T read the manifest endpoint here —
        # doing so forced a type:gemini model onto /v1/chat/completions. resolve_model above
        # still validates membership (hard-fails if the name is absent).
        prefix = _MODEL_ENDPOINT_PREFIX[cfg.model.type]
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
        # A2A egress (Plan 3, completed in SP1): expose peer agents as harness-hosted MCP
        # tools on a loopback FastMCP so BOTH engines discover them. The ek_ stays in the
        # harness (peer auth header via A2AAgentClient); only the loopback URL is written into
        # engine config. RTR-06: a2a-sdk imports stay function-scoped; import the builder lazily.
        if manifest.a2a_agents:
            from ach_agent.engine.a2a_egress import A2AEgressFacade, build_a2a_tools

            a2a_tools = build_a2a_tools(manifest.a2a_agents, ek=ek)
            a2a_facade = A2AEgressFacade(a2a_tools)
            a2a_facade_url = await a2a_facade.start()
            log.info(
                "a2a egress facade started",
                agent_count=len(manifest.a2a_agents),
                tool_count=len(a2a_tools),
                url=a2a_facade_url,
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
    from ach_agent.engine.base.driver import EngineConfig
    from ach_agent.engine.base.pool import EnginePool
    from ach_agent.engine.opencode.driver import OpencodeDriver

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

    passthrough_mcp = collect_passthrough_mcp(cfg.mcp_servers)
    engine_cfg = EngineConfig(
        home=engine_home,
        work_dir=engine_work_dir,
        codemem_db_path=codemem_db_path,
        codemem_project=codemem_project,
        model=cfg.model.name,
        model_type=cfg.model.type,
        params=cfg.model.params,
        # prompt.system = the inline agent persona + per-backend TOOLS_SPEC (boot-static).
        system_prompt=_system_prompt,
        # prompt.compose: append (top-level instructions) | replace (agent.build.prompt).
        compose=cfg.prompt.compose if cfg.prompt else "append",
        steps=cfg.limits.max_steps,
        startup_timeout_seconds=cfg.engine.startup_timeout_seconds,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        model_base_url=model_base_url,
        mcp_local_urls=mcp_local_urls,
        # SEC-01 / ek-hygiene: opencode's env is clean-slate (base allowlist only). Extra
        # var names the operator wants forwarded come from engine.forwardEnv, with any
        # secret.env name stripped (strip_forwarded_secrets, computed above).
        forward_env=effective_forward_env,
        # capability.filter.exclude.tools — disabled in opencode.json (withheld from model).
        exclude_tools=cfg.capability.filter.exclude.tools,
        extra_mcp_servers=passthrough_mcp,
        engine_type=cfg.engine.type,
        binary_path=(
            cfg.engine.pi.binary_path
            if cfg.engine.type == "pi" and cfg.engine.pi is not None
            else "opencode"
        ),
        pi_mcp_adapter_path=(
            cfg.engine.pi.mcp_adapter_path
            if cfg.engine.type == "pi" and cfg.engine.pi is not None
            else ""
        ),
    )
    # D-03/D-04: dedup store first — it opens/repairs state.db (fail-closed on a bad
    # mount). Then the session map shares that now-valid file (fail-open). The pool
    # owns the session map so run_invocation reuses opencode sessions across restarts.
    dedup_store = _open_dedup_store(cfg)
    session_store = _open_session_store(cfg)
    if cfg.engine.type == "pi":
        from ach_agent.engine.pi.driver import PiDriver

        driver: EngineDriver = PiDriver()
    else:
        driver = OpencodeDriver()
    pool = EnginePool(driver=driver, sessions_map=session_store)

    # Best-effort stats sink (harness-local, ACH_STATS_* — never part of CONTRACT_v3).
    # Unset ACH_STATS_REDIS_URL → Prometheus-only, no queue/writer.
    from ach_agent.stats import metrics as stats_metrics
    from ach_agent.stats.sink import StatsSink

    _stats_redis = os.environ.get("ACH_STATS_REDIS_URL")
    _stats_retention = int(os.environ.get("ACH_STATS_RETENTION", "3024000"))
    stats_sink = StatsSink(_stats_redis, retention_s=_stats_retention)
    await stats_sink.start()
    # Tier 1 agent trace: same writer machinery pointed at ach:tools with the per-tool metrics.
    tool_sink = StatsSink(
        _stats_redis,
        stream="ach:tools",
        on_record=stats_metrics.observe_tool,
        retention_s=_stats_retention,
    )
    await tool_sink.start()

    # 6b. Build engine_runner. MEM-01/D-02: pass memory_cfg so engine_runner probes
    # before pool.acquire (Pitfall 3).
    # engine.idle_ttl_seconds (default 60) keeps a keyed server warm after its last release
    # so channel.session=auto persists the opencode session across events for the same
    # session_key. Applied to every configured channel; an unknown channel_name still
    # defaults to 0 at the release site (engine_runner). --tui is NOT a channel — it pins a
    # held ref for the whole REPL, so this TTL never stops it mid-session.
    channel_ttl = {ch.name: cfg.engine.idle_ttl_seconds for ch in cfg.channels}
    channels_by_name = {c.name: c for c in cfg.channels}
    memory_bank = cfg.memory.hindsight.bank if isinstance(cfg.memory, HindsightMemory) else ""
    engine_runner = _make_engine_runner(
        pool=pool,
        driver=driver,
        engine_cfg=engine_cfg,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        terminal_output_retries=cfg.limits.terminal_output_retries,
        max_tool_calls=cfg.engine.max_tool_calls,
        memory_cfg=cfg.memory,
        channel_ttl=channel_ttl,
        channels_by_name=channels_by_name,
        agent_name=cfg.agent.name,
        memory_bank=memory_bank,
        stats_sink=stats_sink,
        tool_sink=tool_sink,
        memory_facade_url=memory_facade_url,
        repo_facade_url=repo_facade_url,
        a2a_facade_url=a2a_facade_url,
    )

    # Step 6 (cont.): construct Router with all limits from config (RTR-03/04)
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
                    _mem_ok, _ = await prepare_memory(cfg.memory)
                    if _mem_ok and memory_facade_url:
                        warm_mcp_servers = [memory_facade_url]
                # The repo-checkout facade is static (no probe) — include it in the pre-warmed
                # opencode.json so the console session sees checkout_repo from the first prompt.
                if repo_facade_url:
                    warm_mcp_servers = [*warm_mcp_servers, repo_facade_url]
                if a2a_facade_url:
                    warm_mcp_servers = [*warm_mcp_servers, a2a_facade_url]
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
                        config_path=warm_server.config_path,
                    )
        finally:
            # Stop any warm-held engine server (idle TTL may not have elapsed at EOF).
            await pool.stop_all()
            if hasattr(pool.oc_sessions, "close"):
                pool.oc_sessions.close()
            await stop_model_proxies()
            if mcp_proxy is not None:
                await mcp_proxy.stop()
            if memory_facade is not None:
                await memory_facade.stop()
            if repo_facade is not None:
                await repo_facade.stop()
            if a2a_facade is not None:
                await a2a_facade.stop()
            if hasattr(dedup_store, "close"):
                dedup_store.close()
            await stats_sink.stop()
            await tool_sink.stop()
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
        bridge = A2AAgentExecutorBridge(handler=None, channel_cfg=channel)

        # on_complete/on_fail (W9: bound here in the boot module, engine tier stays
        # unaware of A2A type). on_fail mirrors on_complete: emits a FAILED event when
        # the terminal output is unusable (action != a2a_reply, or empty reply text).
        _on_complete = bridge.signal_completion
        _on_fail = bridge.signal_failure

        # Wrap the router to inject on_complete + on_fail into delivery_context (W9 pattern).
        bridge._handler = _A2AHandler(router, _on_complete, _on_fail)

        # Build the A2A AgentCard from channel config (minimal — receiver-only v1, spec §14.6).
        # make_a2a_agent_card keeps a2a.* imports inside channels/a2a.py (RTR-06 fence).
        agent_card = make_a2a_agent_card(channel.name)
        sub_app = build_a2a_app(agent_card, bridge)
        mount_path = f"/a2a/{channel.name}"
        a2a_mounts.append((mount_path, sub_app))
        a2a_bridges.append(bridge)
        log.info("a2a channel bridge built", channel_name=channel.name, mount_path=mount_path)

    # 6c. Create FastAPI app with all webhook channels.
    # a2a_mounts threads the A2A sub-apps under the same socket (topology A).
    app = create_app(
        channels=webhook_channels,
        handler=router,
        a2a_mounts=a2a_mounts,
    )
    # Expose state dict so _drain can flip draining/ready (same ref as app.extra['state'])
    state: dict[str, Any] = app.extra["state"]

    # Step 7: wire channel adapters (D-08: one CronScheduler for ALL cron channels, SC#3)
    tasks: list[asyncio.Task[None]] = []
    uv_server: Any = None  # captured below when uvicorn boots (always)

    # D-08/SC#3: collect all cron channels and construct exactly ONE CronScheduler.
    # Pitfall 9 (one task per channel) is superseded by D-08 (one scheduler for all).
    cron_channels = [ch for ch in cfg.channels if ch.type == "cron"]
    cron_scheduler: CronScheduler | None = None
    if cron_channels:
        cron_scheduler = CronScheduler(cron_channels, handler=router)
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
        consumer = QueueConsumer(channel, handler=router)
        await consumer.start()
        queue_consumers.append(consumer)
        log.info("queue consumer started", channel_name=channel.name, stream=channel.queue.key)  # type: ignore[union-attr]

    log.info("channels registered", names=[ch.name for ch in cfg.channels])

    # Boot uvicorn UNCONDITIONALLY (CONTRACT §4): healthz/readyz/metrics MUST always be
    # reachable, even for cron-only or queue-only configs with no inbound HTTP channel —
    # otherwise k8s liveness/readiness probes fail and the pod is killed. Webhook + a2a
    # channels additionally serve their routes on this same socket (topology A).
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

    # Wait for SIGTERM/SIGINT OR all tasks to finish (tasks loop forever normally).
    # uvicorn boots unconditionally, so `tasks` is never empty.
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    await asyncio.wait(
        [shutdown_task, *tasks],
        return_when=asyncio.FIRST_COMPLETED,
    )

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
        # Stop every warm keyed opencode server BEFORE the proxies. With
        # engine.idle_ttl_seconds > 0 a recently-used server lingers past its last release
        # with a pending _expire task; without this its subprocess (start_new_session=True,
        # own process group) would survive the harness exit and orphan (leaking the port).
        # Idempotent; also cancels the pending TTL tasks.
        await pool.stop_all()
        if hasattr(pool.oc_sessions, "close"):
            pool.oc_sessions.close()
        # Plan 2: tear down the localhost proxies (closes their aiohttp runners/sessions).
        await stop_model_proxies()
        if mcp_proxy is not None:
            await mcp_proxy.stop()
        if memory_facade is not None:
            await memory_facade.stop()
        if repo_facade is not None:
            await repo_facade.stop()
        if a2a_facade is not None:
            await a2a_facade.stop()
        await stats_sink.stop()
        await tool_sink.stop()
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
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--tui", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--prompt")
    args, _unknown = parser.parse_known_args(argv)
    return args.tui, args.prompt, args.debug


if __name__ == "__main__":
    # `--tui` / `--debug` / `--prompt` launch modifiers: drive the engine directly.
    _tui_mode, _one_shot, _debug_mode = _parse_cli(sys.argv[1:])
    try:
        asyncio.run(main(tui_mode=_tui_mode, one_shot_prompt=_one_shot, debug_mode=_debug_mode))
    except KeyboardInterrupt:
        # ponytail: Ctrl+C in the console/REPL modes — exit quietly, no traceback.
        pass

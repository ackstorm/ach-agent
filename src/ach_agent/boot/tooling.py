# SPDX-License-Identifier: Apache-2.0
"""Engine tool-call observability: turn opencode tool-lifecycle events into logs and stats.

Observability never breaks a turn: stat/metric sinks swallow their own exceptions and never
await in the hot path (see ``StatsSink.record``). Tool output/error text can carry secrets;
what reaches a stats stream is scrubbed and truncated elsewhere (``tool_detail`` truncation,
``ToolStat`` storing sizes not raw payloads) — these helpers must not widen what gets emitted.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.engine.events import OpenCodeToolUpdate
from ach_agent.stats.sink import StatsSink

log = structlog.get_logger(__name__)


def clean_tool_name(name: str) -> str:
    """Collapse opencode's doubled MCP prefix for readability.

    opencode ids MCP tools as ``<server>_<server>_<tool>`` (the server segment repeats,
    e.g. ``mcp-gitlab-ro_mcp-gitlab-ro_gitlab_get_merge_request``). Render it as
    ``<server>/<tool>``. Native tools (``grep``, ``bash``) have no such prefix and pass through.
    """
    parts = name.split("_", 2)
    if len(parts) == 3 and parts[0] == parts[1]:
        return f"{parts[0]}/{parts[2]}"
    return name


def tool_detail(raw: str) -> str:
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


def log_engine_tool(update: OpenCodeToolUpdate) -> None:
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
        "tool": clean_tool_name(update.tool_name),
        "status": state.status,
    }
    # state is a ToolState union (Running/Completed/Error); title/output/error are declared
    # on some members but not others, so attribute access must stay dynamic here.
    action = getattr(state, "title", "")
    if action:
        fields["action"] = action[:200]
    detail = getattr(state, "output", "") or getattr(state, "error", "")
    if detail:
        fields["detail"] = tool_detail(detail)
    log.info("engine: tool", **fields)


def make_tool_recorder(
    inner: Callable[[OpenCodeToolUpdate], None],
    tool_sink: StatsSink,
    event: MessageEvent,
    model: str,
) -> Callable[[OpenCodeToolUpdate], None]:
    """Wrap an on_tool sink to also record one ToolStat per tool call (Tier 1 agent trace).

    Stamps a monotonic start on the ``running`` transition; on the ``completed``/``error``
    transition computes the duration and records once per call_id, then delegates to ``inner``
    (the channel's sink or log_engine_tool). Per-invocation state — a fresh map each turn.
    """
    from ach_agent.stats.sink import build_tool_stat

    starts: dict[str, float] = {}
    done: set[str] = set()
    # MessageEvent declares no `source` field — some channels stash one dynamically via
    # delivery machinery, so this stays a getattr with channel_name as the fallback.
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
            display = clean_tool_name(update.tool_name)
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

# SPDX-License-Identifier: Apache-2.0
"""TUI console runner — the `--tui` launch modifier (NOT a channel).

`--tui` is a launch modifier, not a channel: when set, the harness boots fully
(config, hydration, localhost proxies, opencode) but IGNORES the configured
channels and opens a console REPL instead. Each line you type/paste IS the prompt
sent straight to the agent — so you can simulate a cron tick by pasting its prompt,
a webhook/hook by pasting that instruction, etc.

Each line becomes a MessageEvent (source_trait="sync") with a fresh reply_future on a
single stable session (conversational continuity), routed through the bounded lane;
engine_runner resolves the future with the engine's free-form text, which is printed
verbatim — there is NO terminal contract.

Testability: run_tui_console accepts optional reader/writer. The default reader wraps
sys.stdin (blocking readline in a thread executor); the default writer writes to stdout.

RTR-06: NEVER import from hermes_agent.* or engine.* here.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler

log = structlog.get_logger(__name__)

# Stable session for the whole console session → all lines share one FIFO lane
# (serialized) and one logical session for continuity.
_CONSOLE_SESSION_KEY = "tui-console"


def _default_write(text: str) -> None:
    """Default writer: write text + newline to stdout and flush."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


# opencode prefixes mcp tool ids with the server id (sometimes twice):
# "mcp-google-calendar-ro_mcp-google-calendar-ro_auth_wait" → "auth_wait".
_MCP_TOOL_PREFIX = re.compile(r"^(mcp-[a-z0-9-]+_)+")


def _clean_tool_name(name: str) -> str:
    """Strip opencode's mcp-server prefixes from a tool id for display."""
    return _MCP_TOOL_PREFIX.sub("", name) or name


def _tool_detail(state: Any) -> str:
    """Short descriptive suffix for a tool line: opencode's `title`, else a compact arg hint.

    The agent's own tool args (calendar query, etc.) — truncated, no secrets handled here.
    """
    title = str(getattr(state, "title", "")).strip()
    if title:
        return title
    inp = getattr(state, "input", None)
    if isinstance(inp, dict) and inp:
        try:
            s = json.dumps(inp, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return ""
        return s[:120] + ("…" if len(s) > 120 else "")
    return ""


def _format_tool(event: Any) -> str | None:
    """Render a tool-lifecycle event to a one-line chrome string, or None if nothing to show.

    Duck-typed (RTR-06: no engine import) — reads .tool_name / .state.status/.title/.input/.error.
    """
    state = getattr(event, "state", None)
    status = getattr(state, "status", "")
    name = _clean_tool_name(getattr(event, "tool_name", ""))
    if status == "running":
        detail = _tool_detail(state)
        return f"[TOOL] ⚙ {name}…" + (f" {detail}" if detail else "")
    if status == "error":
        err = str(getattr(state, "error", ""))[:200]
        return f"[TOOL] ⚠ {name} failed: {err}"
    return None  # completed / pending → no chrome


class _ConsoleRenderer:
    """Coordinates streamed reply text (stdout) and tool chrome (stderr) on one TTY.

    The two go to different streams (so a piped stdout stays a clean reply), but on a shared
    terminal they interleave. Tracking whether stdout is mid-line lets a tool indicator (and
    the next text) start on its own line with a blank-line break — no "…texto⚙ tool" glue.
    """

    def __init__(self) -> None:
        self._at_line_start = True

    def text(self, delta: str) -> None:
        sys.stdout.write(delta)
        sys.stdout.flush()
        if delta:
            self._at_line_start = delta.endswith("\n")

    def tool(self, event: Any) -> None:
        line = _format_tool(event)
        if line is None:
            return
        # Blank-line separate from a dangling streamed text line; consecutive tools (already
        # at line start) get no extra gap.
        sep = "" if self._at_line_start else "\n\n"
        sys.stderr.write(f"{sep}{line}\n")
        sys.stderr.flush()
        self._at_line_start = True


class _StdinReader:
    """Async-iterable wrapper over sys.stdin: readline() in a thread executor.

    sys.stdin.readline() is blocking, so it is run in the default executor to avoid
    stalling the event loop. Returns "" on EOF (matches the loop's stop condition).
    """

    async def readline(self) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, sys.stdin.readline)


async def _handle_line(
    handler: MessageHandler,
    line: str,
    writer: Callable[[str], None],
    session_key: str,
    stream_sink: Callable[[str], None] | None = None,
    tool_sink: Callable[[Any], None] | None = None,
) -> None:
    """Dispatch one console line to the router and write the engine reply.

    Builds a MessageEvent (source_trait="sync") with a fresh reply_future, routes it,
    then awaits the future for the engine's free-form text. The line IS the prompt
    (payload['text']); build_engine_prompt returns it verbatim. idempotency_key is a
    ms-timestamp string (unique per line, never empty).

    When ``stream_sink`` is set, text DELTAS are written live as the engine produces them
    (the reply appears immediately, before any slow trailing tool call); the full reply is
    then NOT re-written (only a closing newline). When unset, the full reply is written
    once via ``writer`` (the non-streaming path used by tests).
    """
    reply_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    delivery_context: dict[str, object] = {"free_form": True}
    streamed = False
    if stream_sink is not None:

        def _on_text(delta: str) -> None:
            nonlocal streamed
            streamed = True
            stream_sink(delta)

        delivery_context["on_text"] = _on_text
    if tool_sink is not None:
        delivery_context["on_tool"] = tool_sink
    event = MessageEvent(
        idempotency_key=str(int(time.time() * 1000)),
        session_key=session_key,
        channel_name="tui-console",
        payload={"text": line},
        delivery_context=delivery_context,
        source_trait="sync",
        reply_future=reply_future,
    )
    await handler.handle(event)
    text = await reply_future
    if stream_sink is not None and streamed:
        stream_sink("\n\n")  # close the streamed line + a blank line between turns
    else:
        writer(text)  # nothing streamed (no deltas / tests) → write the full reply


async def run_one_shot(
    handler: MessageHandler,
    prompt: str,
    *,
    writer: Callable[[str], None] | None = None,
    session_key: str = _CONSOLE_SESSION_KEY,
) -> None:
    """Run a single free-form prompt (the `--prompt` one-shot modifier) and write the reply.

    Same routing path as run_tui_console (one free-form MessageEvent, no terminal
    contract) but non-interactive: route once, write the reply, return. Lets you script a
    pre-prod dry-run or simulate a cron/hook tick by passing that prompt — no TTY needed.
    """
    wrt = writer if writer is not None else _default_write
    # Stream live to stdout for the real one-shot (writer not injected); a test that
    # injects a writer gets the full reply via that writer (no streaming).
    renderer = _ConsoleRenderer() if writer is None else None
    snk = renderer.text if renderer is not None else None
    tsnk = renderer.tool if renderer is not None else None
    await _handle_line(handler, prompt, wrt, session_key, stream_sink=snk, tool_sink=tsnk)


async def run_tui_console(
    handler: MessageHandler,
    *,
    reader: Any = None,
    writer: Callable[[str], None] | None = None,
    session_key: str = _CONSOLE_SESSION_KEY,
) -> None:
    """Run the console REPL until EOF (Ctrl-D) or cancellation.

    Reads stdin lines; each non-blank line is routed to the engine and the reply is
    written back. Blank lines are skipped. EOF or CancelledError ends the session.
    """
    rdr = reader if reader is not None else _StdinReader()
    wrt = writer if writer is not None else _default_write
    # Interactive (real stdin): stream replies live. Tests inject a reader/writer →
    # non-interactive → full reply written once via the writer.
    interactive = reader is None and writer is None
    # One renderer for the whole console session: it coordinates stdout text + stderr tool
    # chrome across every prompt (line-state carries over between turns).
    renderer = _ConsoleRenderer() if interactive else None
    snk = renderer.text if renderer is not None else None
    tsnk = renderer.tool if renderer is not None else None
    log.info("tui console started — type a prompt, Ctrl-D to exit")
    try:
        while True:
            raw = await rdr.readline()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if raw == "":
                # EOF — end the session cleanly.
                return
            line = raw.rstrip("\n")
            if not line:
                continue
            # A single invocation failing (engine error, opencode crash, timeout) must
            # NOT kill the console: catch, print a one-line error, keep the REPL alive so
            # the next prompt still works. CancelledError still ends the session.
            try:
                await _handle_line(handler, line, wrt, session_key, stream_sink=snk, tool_sink=tsnk)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("tui: invocation failed", error=str(exc))
                wrt(f"[error] {exc}")
    except asyncio.CancelledError:
        return

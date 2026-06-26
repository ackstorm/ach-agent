"""TUI console-runner unit tests — the `--tui` modifier (stdin/stdout, no contract).

Drives run_tui_console with a fake reader (queued lines, "" = EOF) against a fake
handler that resolves the event's reply_future, and a capturing writer.

Verifies:
  - The engine's free-form reply text is written to the writer (no terminal contract).
  - Blank lines are skipped; EOF ends the session.
  - The MessageEvent carries source_trait="sync", a non-empty idempotency_key, the
    console channel_name/session_key, a reply_future, and payload["text"] == the line.
"""

from __future__ import annotations

from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.tui import run_one_shot, run_tui_console
from ach_agent.router.router import RouterAdmitResult


class FakeHandler:
    """Resolves the event reply_future with a fixed reply and records the event."""

    def __init__(self, reply: str = "ENGINE REPLY") -> None:
        self._reply = reply
        self.events: list[MessageEvent] = []

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        assert event.reply_future is not None
        event.reply_future.set_result(self._reply)
        return RouterAdmitResult.ACCEPTED


class FakeReader:
    """Async readline over a queue of raw lines; returns "" (EOF) when exhausted."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    async def readline(self) -> str:
        return self._lines.pop(0) if self._lines else ""


async def test_console_writes_engine_reply() -> None:
    """A console line → engine reply text written to the writer (no terminal contract)."""
    handler = FakeHandler("ENGINE REPLY")
    captured: list[str] = []

    await run_tui_console(handler, reader=FakeReader(["hello\n"]), writer=captured.append)

    assert any("ENGINE REPLY" in line for line in captured)


async def test_blank_lines_skipped_and_event_fields() -> None:
    """Blank lines are skipped; the routed event carries the expected console fields."""
    handler = FakeHandler()
    captured: list[str] = []

    # "\n" (blank → skipped), then "hi\n" (routed), then EOF.
    await run_tui_console(handler, reader=FakeReader(["\n", "hi\n"]), writer=captured.append)

    assert len(handler.events) == 1, "blank line must be skipped — only 'hi' routed"
    event = handler.events[0]
    assert event.source_trait == "sync"
    assert event.idempotency_key != ""
    assert event.channel_name == "tui-console"
    assert event.session_key == "tui-console"
    assert event.reply_future is not None
    assert event.payload == {"text": "hi"}


class StreamingHandler:
    """Invokes the event's on_text sink with deltas, then resolves the reply_future."""

    def __init__(self, deltas: list[str], reply: str) -> None:
        self._deltas = deltas
        self._reply = reply
        self.events: list[MessageEvent] = []

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        on_text = event.delivery_context.get("on_text")
        if callable(on_text):
            for d in self._deltas:
                on_text(d)
        assert event.reply_future is not None
        event.reply_future.set_result(self._reply)
        return RouterAdmitResult.ACCEPTED


async def test_handle_line_streams_deltas_and_skips_full_write() -> None:
    """With a stream_sink, deltas stream live + a closing newline; full reply NOT re-written."""
    from ach_agent.channels.tui import _handle_line

    handler = StreamingHandler(["Hola ", "mundo"], "Hola mundo")
    streamed: list[str] = []
    written: list[str] = []

    await _handle_line(handler, "hi", written.append, "tui-console", stream_sink=streamed.append)

    assert "".join(streamed) == "Hola mundo\n\n", "deltas streamed live + closing blank line"
    assert written == [], "streamed reply must not be re-written in full (no duplicate)"


class _ToolEvent:
    """Duck-typed stand-in for OpenCodeToolUpdate (the tui sink must not import engine)."""

    def __init__(self, tool_name: str, status: str, error: str = "") -> None:
        self.tool_name = tool_name
        self.state = type("S", (), {"status": status, "error": error})()


class ToolingHandler:
    """Fires the event's on_tool sink with a tool lifecycle, then resolves the reply."""

    def __init__(self, tools: list, reply: str) -> None:
        self._tools = tools
        self._reply = reply
        self.events: list[MessageEvent] = []

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        on_tool = event.delivery_context.get("on_tool")
        if callable(on_tool):
            for t in self._tools:
                on_tool(t)
        assert event.reply_future is not None
        event.reply_future.set_result(self._reply)
        return RouterAdmitResult.ACCEPTED


async def test_handle_line_renders_tool_via_tool_sink() -> None:
    """A tool_sink receives the on_tool events; the formatter cleans + labels the line."""
    from ach_agent.channels.tui import _format_tool, _handle_line

    running = _ToolEvent("mcp-google-calendar-ro_mcp-google-calendar-ro_auth_wait", "running")
    handler = ToolingHandler([running], "done")
    seen: list = []

    await _handle_line(
        handler, "hi", lambda _s: None, "tui-console", stream_sink=lambda _d: None,
        tool_sink=seen.append,
    )

    assert seen == [running], "on_tool sink received the tool lifecycle event"
    # Formatter strips the doubled mcp-…_ prefix and labels the line.
    assert _format_tool(running) == "[TOOL] ⚙ auth_wait…"
    # Error variant.
    err_line = _format_tool(_ToolEvent("calendar_list_events", "error", "boom"))
    assert err_line == "[TOOL] ⚠ calendar_list_events failed: boom"
    # Completed → no chrome.
    assert _format_tool(_ToolEvent("x", "completed")) is None


async def test_one_shot_writes_engine_reply_and_event_fields() -> None:
    """run_one_shot routes exactly one free-form prompt and writes the reply (no contract)."""
    handler = FakeHandler("ONE SHOT REPLY")
    captured: list[str] = []

    await run_one_shot(handler, "review this", writer=captured.append)

    assert captured == ["ONE SHOT REPLY"]
    assert len(handler.events) == 1, "one-shot routes exactly one event"
    event = handler.events[0]
    assert event.source_trait == "sync"
    assert event.payload == {"text": "review this"}
    # free_form marker → engine_runner skips terminal extraction / repair turn
    assert event.delivery_context.get("free_form") is True

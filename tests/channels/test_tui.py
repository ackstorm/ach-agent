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
from ach_agent.channels.tui import run_tui_console
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

"""TUI channel runtime unit tests (stdin/stdout free-form, no terminal contract).

Fast suite — drives the channel via the exposed `_handle_line()` helper against a
fake handler that resolves the event's reply_future, and a capturing writer.

Verifies:
  - The engine's free-form reply text is written to the writer (no terminal contract).
  - The MessageEvent passed to the handler has source_trait == "sync", a non-empty
    idempotency_key, channel_name == <cfg name>, and a non-None reply_future.
"""

from __future__ import annotations

from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.tui import TuiChannel
from ach_agent.config.schema import ChannelConfig
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


def _make_channel_cfg() -> ChannelConfig:
    return ChannelConfig.model_validate({"name": "console", "type": "tui"})


async def test_handle_line_writes_engine_reply() -> None:
    """A line read from stdin → engine reply text written to the writer."""
    cfg = _make_channel_cfg()
    handler = FakeHandler("ENGINE REPLY")
    captured: list[str] = []

    tui = TuiChannel(cfg, handler=handler, writer=captured.append)
    await tui._handle_line("hello")

    assert any("ENGINE REPLY" in line for line in captured)


async def test_message_event_fields() -> None:
    """The MessageEvent crossing the seam carries the expected TUI fields."""
    cfg = _make_channel_cfg()
    handler = FakeHandler()
    captured: list[str] = []

    tui = TuiChannel(cfg, handler=handler, writer=captured.append)
    await tui._handle_line("hello")

    assert len(handler.events) == 1
    event = handler.events[0]
    assert event.source_trait == "sync"
    assert event.idempotency_key != ""
    assert event.channel_name == "console"
    assert event.reply_future is not None
    assert event.payload == {"text": "hello"}

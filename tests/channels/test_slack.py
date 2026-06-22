"""Slack channel adapter unit tests (CHN-03, D-03) — Plan 04-02 GREEN.

Covers:
  - Hermes event → canonical MessageEvent shim translation
  - lane/session_key derivation: channel + thread_ts (fallback to channel)
  - A′ cold-start gate drops event before engine ready
  - FULL_QUEUE async_no_retry drop path
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.router import RouterAdmitResult


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class FakeHandler:
    """Captures emitted MessageEvents and returns a configurable result."""

    def __init__(self, result: RouterAdmitResult = RouterAdmitResult.ACCEPTED) -> None:
        self._result = result
        self.events: list[MessageEvent] = []
        self._call_count = 0

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        self._call_count += 1
        return self._result


class FakePool:
    """Minimal EnginePool stand-in for A′ gate tests."""

    def __init__(self, *, engine_has_been_ready_once: bool = False) -> None:
        self.engine_has_been_ready_once = engine_has_been_ready_once


def _make_channel_cfg(name: str = "slack-test") -> Any:
    """Build a minimal ChannelConfig for the Slack adapter."""
    from ach_agent.config.schema import ChannelConfig

    return ChannelConfig.model_validate({"name": name, "type": "slack"})


def _make_hermes_event(
    channel_id: str = "C123",
    thread_ts: str | None = None,
    ts: str = "1717000000.000001",
    text: str = "hello",
) -> Any:
    """Build a minimal Hermes-style MessageEvent using the real hermes_agent types."""
    from gateway.platforms.base import (  # type: ignore[import-untyped]
        MessageEvent as HermesEvent,
        Platform,
        SessionSource,
    )

    source = SessionSource(
        platform=Platform.SLACK,
        chat_id=channel_id,
        thread_id=thread_ts,
    )
    return HermesEvent(text=text, source=source, message_id=ts)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_shim_translates_hermes_event_to_message_event() -> None:
    """CHN-03/D-03: Slack shim builds MessageEvent with correct fields from Hermes event."""
    from ach_agent.channels.slack import _make_slack_shim

    handler = FakeHandler()
    channel_cfg = _make_channel_cfg("slack-ch")
    pool = FakePool(engine_has_been_ready_once=True)
    shim = _make_slack_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(channel_id="C999", ts="1717000001.000001", text="hi")
    await shim(hermes_event)

    assert len(handler.events) == 1
    evt = handler.events[0]
    assert evt.channel_name == "slack-ch"
    assert evt.source_trait == "async_no_retry"
    assert evt.payload["text"] == "hi"
    assert evt.payload["chat_id"] == "C999"
    assert evt.idempotency_key == "1717000001.000001"


@pytest.mark.asyncio
async def test_slack_session_key_uses_channel_and_thread_ts() -> None:
    """D-03: session_key = channel_id + thread_ts when thread is present."""
    from ach_agent.channels.slack import _make_slack_shim

    handler = FakeHandler()
    channel_cfg = _make_channel_cfg()
    pool = FakePool(engine_has_been_ready_once=True)
    shim = _make_slack_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(channel_id="C111", thread_ts="1717000000.000001")
    await shim(hermes_event)

    assert len(handler.events) == 1
    assert handler.events[0].session_key == "C111:1717000000.000001"


@pytest.mark.asyncio
async def test_slack_session_key_fallback_to_channel_when_no_thread() -> None:
    """D-03: session_key = channel_id when thread_ts is absent (fallback to ts root)."""
    from ach_agent.channels.slack import _make_slack_shim

    handler = FakeHandler()
    channel_cfg = _make_channel_cfg()
    pool = FakePool(engine_has_been_ready_once=True)
    shim = _make_slack_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(channel_id="C222", thread_ts=None)
    await shim(hermes_event)

    assert len(handler.events) == 1
    assert handler.events[0].session_key == "C222"


@pytest.mark.asyncio
async def test_slack_a_prime_gate_drops_event_before_engine_ready() -> None:
    """D-06: A′ gate — event dropped and handler NOT called when engine_has_been_ready_once=False."""
    from ach_agent.channels.slack import _make_slack_shim

    handler = FakeHandler()
    channel_cfg = _make_channel_cfg("slack-gate")
    pool = FakePool(engine_has_been_ready_once=False)
    shim = _make_slack_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event()
    result = await shim(hermes_event)

    # Handler must NOT have been called — event was dropped at A′ gate
    assert handler._call_count == 0
    assert result is None


@pytest.mark.asyncio
async def test_slack_full_queue_drops_and_logs(capsys: pytest.CaptureFixture[str]) -> None:
    """D-05/RTR-05: FULL_QUEUE result → drop+log (async_no_retry), never silent.

    structlog emits to stdout (not stdlib logging), so we capture stdout.
    """
    from ach_agent.channels.slack import _make_slack_shim

    handler = FakeHandler(result=RouterAdmitResult.FULL_QUEUE)
    channel_cfg = _make_channel_cfg("slack-fq")
    pool = FakePool(engine_has_been_ready_once=True)
    shim = _make_slack_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event()
    await shim(hermes_event)

    # Handler was called (event was dispatched, then dropped due to full queue)
    assert handler._call_count == 1
    # A warning must have been emitted (never silent — RTR-05)
    captured = capsys.readouterr()
    assert "queue full" in captured.out.lower(), (
        f"Expected 'queue full' warning in logs, got stdout: {captured.out!r}"
    )

"""Telegram channel adapter unit tests (CHN-04, D-03).

Covers:
  - Hermes PTB event → canonical MessageEvent shim translation
  - lane/session_key derivation: chat_id + message_thread_id (fallback to chat_id)
  - A′ cold-start gate drops event before engine ready
  - FULL_QUEUE async_no_retry drop path

Mirrors tests/channels/test_slack.py structure (Plan 04-02).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.telegram import _make_telegram_shim
from ach_agent.config.schema import ChannelConfig
from ach_agent.router.router import RouterAdmitResult

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_channel_cfg(name: str = "telegram-test") -> ChannelConfig:
    return ChannelConfig.model_validate({"name": name, "type": "telegram"})


def _make_hermes_event(
    text: str = "hello",
    chat_id: str = "111",
    thread_id: int | None = None,
    update_id: int = 42,
) -> Any:
    """Build a minimal mock Hermes MessageEvent matching PTB field mapping."""
    source = MagicMock()
    source.chat_id = chat_id
    source.thread_id = thread_id

    event = MagicMock()
    event.text = text
    event.source = source
    event.platform_update_id = update_id
    return event


class FakePool:
    """Minimal pool stand-in for A′ gate tests."""

    def __init__(self, *, ready: bool = True) -> None:
        self.engine_has_been_ready_once = ready


class FakeHandler:
    """Captures events and returns configurable RouterAdmitResult."""

    def __init__(self, result: RouterAdmitResult = RouterAdmitResult.ACCEPTED) -> None:
        self.events: list[MessageEvent] = []
        self._result = result

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        return self._result


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_shim_translates_hermes_event_to_message_event() -> None:
    """CHN-04/D-03: Telegram shim builds MessageEvent with correct fields from Hermes PTB event."""
    handler = FakeHandler()
    pool = FakePool(ready=True)
    channel_cfg = _make_channel_cfg("tg-main")
    shim = _make_telegram_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(
        text="Test message", chat_id="222", thread_id=None, update_id=99
    )
    await shim(hermes_event)

    assert len(handler.events) == 1
    event = handler.events[0]
    assert event.channel_name == "tg-main"
    assert event.payload["text"] == "Test message"
    assert event.payload["chat_id"] == "222"
    assert event.source_trait == "async_no_retry"
    # idempotency key must encode update_id
    assert "99" in event.idempotency_key


@pytest.mark.asyncio
async def test_telegram_session_key_uses_chat_id_and_message_thread_id() -> None:
    """D-03: session_key = chat_id + message_thread_id when forum topic is present."""
    handler = FakeHandler()
    pool = FakePool(ready=True)
    channel_cfg = _make_channel_cfg("tg-forum")
    shim = _make_telegram_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(
        chat_id="333", thread_id=7, update_id=101
    )
    await shim(hermes_event)

    assert len(handler.events) == 1
    assert handler.events[0].session_key == "333:7"


@pytest.mark.asyncio
async def test_telegram_session_key_fallback_to_chat_id_when_no_thread() -> None:
    """D-03: session_key = chat_id when message_thread_id is absent."""
    handler = FakeHandler()
    pool = FakePool(ready=True)
    channel_cfg = _make_channel_cfg("tg-plain")
    shim = _make_telegram_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(chat_id="444", thread_id=None, update_id=200)
    await shim(hermes_event)

    assert len(handler.events) == 1
    assert handler.events[0].session_key == "444"


@pytest.mark.asyncio
async def test_telegram_a_prime_gate_drops_event_before_engine_ready() -> None:
    """D-06: A′ gate — event dropped and handler NOT called when engine_has_been_ready_once=False."""
    handler = FakeHandler()
    pool = FakePool(ready=False)
    channel_cfg = _make_channel_cfg("tg-cold")
    shim = _make_telegram_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(chat_id="555", update_id=300)
    await shim(hermes_event)

    assert len(handler.events) == 0, "Handler must NOT be called during A′ gate"


@pytest.mark.asyncio
async def test_telegram_full_queue_drops_and_logs() -> None:
    """D-05/RTR-05: FULL_QUEUE result → drop+log (async_no_retry), never silent."""
    handler = FakeHandler(result=RouterAdmitResult.FULL_QUEUE)
    pool = FakePool(ready=True)
    channel_cfg = _make_channel_cfg("tg-full")
    shim = _make_telegram_shim(handler, pool, channel_cfg)

    hermes_event = _make_hermes_event(chat_id="666", update_id=400)
    # Must not raise — FULL_QUEUE is silently dropped + logged
    await shim(hermes_event)

    # Handler was called (event was admitted to handler, returned FULL_QUEUE)
    assert len(handler.events) == 1

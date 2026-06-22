"""Shared e2e mock fixtures for Phase 4 channel adapter tests.

Provides:
  - MockSlackAdapter: captures send() calls, fires inbound events via inject_inbound()
  - MockTelegramAdapter: captures send() calls, fires inbound events via inject_update()
  - MockEventQueue: captures EventQueue.enqueue_event() calls for A2A e2e tests

Pattern: mirrors Phase 2 mock-GitLab harness (tests/e2e/test_gitlab_e2e.py).
All mocks implement the same interface as the real Hermes adapters + a2a-sdk EventQueue,
so e2e tests can monkeypatch the real adapters with these and run hermetically.

RESEARCH.md Mock Adapter Shapes section is the authoritative source for this interface.
"""

from __future__ import annotations

from typing import Any

import pytest


class MockSlackAdapter:
    """Replaces SlackAdapter for e2e: captures send() calls, fires inbound events.

    Interface matches Hermes BasePlatformAdapter:
      - set_message_handler(handler): register the handler callback
      - connect() -> bool: always returns True (no real socket)
      - disconnect(): no-op
      - send(chat_id, content, metadata=None, **kwargs): captures message

    Test helpers:
      - inject_inbound(text, channel_id, thread_ts, ts): simulate a Slack message arriving
        by calling the registered handler with a Hermes-style MessageEvent.
    """

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self._handler: Any = None

    def set_message_handler(self, handler: Any) -> None:
        self._handler = handler

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        content: str,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.sent_messages.append(
            {"chat_id": chat_id, "text": content, "meta": metadata}
        )

    async def inject_inbound(
        self,
        text: str,
        channel_id: str,
        thread_ts: str | None = None,
        ts: str = "1234567890.123",
    ) -> None:
        """Simulate a Slack message arriving via Socket Mode."""
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
        event = HermesEvent(text=text, source=source, message_id=ts)
        if self._handler is not None:
            await self._handler(event)


class MockTelegramAdapter:
    """Replaces TelegramAdapter for e2e: captures send() calls, fires inbound updates.

    Interface matches Hermes BasePlatformAdapter (same as MockSlackAdapter).

    Test helpers:
      - inject_update(text, chat_id, thread_id, update_id): simulate a Telegram update.
    """

    def __init__(self) -> None:
        self.sent_messages: list[dict[str, Any]] = []
        self._handler: Any = None

    def set_message_handler(self, handler: Any) -> None:
        self._handler = handler

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        pass

    async def send(
        self,
        chat_id: str,
        content: str,
        **kwargs: Any,
    ) -> None:
        self.sent_messages.append({"chat_id": chat_id, "text": content})

    async def inject_update(
        self,
        text: str,
        chat_id: str,
        thread_id: int | None = None,
        update_id: int = 42,
    ) -> None:
        """Simulate a Telegram message update arriving via PTB polling."""
        from gateway.platforms.base import (  # type: ignore[import-untyped]
            MessageEvent as HermesEvent,
            Platform,
            SessionSource,
        )

        source = SessionSource(
            platform=Platform.TELEGRAM,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        event = HermesEvent(
            text=text, source=source, platform_update_id=update_id
        )
        if self._handler is not None:
            await self._handler(event)


class MockEventQueue:
    """Replaces a2a-sdk EventQueue for A2A e2e tests.

    Captures events enqueued via enqueue_event() so tests can assert:
      - TaskStatusUpdateEvent(completed, final=True) is enqueued after engine delivery
      - TaskStatusUpdateEvent(failed, final=True) is enqueued on A′ gate or FULL_QUEUE
    """

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)

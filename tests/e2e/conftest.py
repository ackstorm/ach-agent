"""Shared e2e mock fixtures for channel adapter tests.

Provides:
  - MockEventQueue: captures EventQueue.enqueue_event() calls for A2A e2e tests

All mocks implement the same interface as the real a2a-sdk EventQueue,
so e2e tests can run hermetically (no live A2A peer).
"""

from __future__ import annotations

from typing import Any


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

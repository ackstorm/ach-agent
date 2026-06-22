# SPDX-License-Identifier: Apache-2.0
"""Admission helpers — AtomicCounter for queued_total tracking.

Single-threaded asyncio: no locks needed for integer counter operations.
All mutations happen on the event loop thread.

CONTRACT §6.3: maxQueuedTotal is always enforced. queued_total is the
authoritative count of events waiting in lane queues OR being processed.

Constraint: NEVER import from hermes_agent.* or engine.* here (RTR-06, D-08).
"""

from __future__ import annotations


class AtomicCounter:
    """Simple integer counter — safe in single-threaded asyncio (no locks needed).

    inc() / dec() are called from the event loop thread only (router.handle and
    lane consumer tasks all run on the single asyncio event loop).
    """

    def __init__(self, initial: int = 0) -> None:
        self._value: int = initial

    def get(self) -> int:
        return self._value

    def inc(self) -> None:
        self._value += 1

    def dec(self) -> None:
        self._value -= 1

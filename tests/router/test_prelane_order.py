"""RTR-01 conformance: dedup MUST precede backpressure (CONTRACT §6.2).

D-05 invariant 1: A duplicate event is discarded by dedup BEFORE it consumes
a backpressure slot. Filling the queue with duplicates of the same key must
leave full capacity available for distinct events.

ORDER IS NORMATIVE — see router.py and CONTRACT §6.2.
"""
from __future__ import annotations

import pytest

from tests.router.conftest import make_event


@pytest.mark.asyncio
async def test_dedup_before_backpressure(router) -> None:
    """RTR-01: maxQueuedTotal copies of same event → all deduped → new distinct event accepted.

    If dedup did NOT precede backpressure, each duplicate would consume a queue slot.
    After max_queued_total duplicates the queue would appear full and reject the distinct event.
    The router must NOT behave that way — duplicates are free (no slot consumed).
    """
    from ach_agent.router.router import RouterAdmitResult

    # conftest router fixture: max_queued_total=5, max_concurrent_invocations=2
    event = make_event(idempotency_key="evt-001", session_key="s1")
    duplicate = make_event(idempotency_key="evt-001", session_key="s1")  # same key

    # First event is accepted (establishes the key in dedup store)
    first = await router.handle(event)
    assert first == RouterAdmitResult.ACCEPTED

    # All subsequent duplicates must be DUPLICATE — not FULL_QUEUE
    # queued_total must not change: duplicates must NOT consume queue slots
    for _ in range(router._max_queued_total - 1):
        result = await router.handle(duplicate)
        assert result == RouterAdmitResult.DUPLICATE, (
            "Duplicate events MUST be discarded by dedup BEFORE consuming a queue slot "
            "(ORDER IS NORMATIVE, CONTRACT §6.2)"
        )

    # Queue still has capacity (only 1 slot used by the first ACCEPTED event)
    # A distinct new event must be accepted, not rejected with FULL_QUEUE
    new_event = make_event(idempotency_key="evt-002", session_key="s2")
    result = await router.handle(new_event)
    assert result == RouterAdmitResult.ACCEPTED, (
        "Duplicates MUST NOT consume queue slots — "
        "queue should still have capacity for distinct events"
    )

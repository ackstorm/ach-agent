"""CONTRACT §6.2: Pre-lane order invariant (authoritative conformance test).

Invariant: dedup → backpressure (maxQueuedTotal) → lane. Duplicates are
discarded before they consume a queue slot.
"""
from __future__ import annotations

import pytest

from tests.router.conftest import FakeEngine, make_event


@pytest.mark.asyncio
async def test_inv02_prelane_order(fake_engine: FakeEngine) -> None:
    """§6.2: dedup → backpressure → lane ordering — authoritative conformance.

    CONTRACT perspective: at queue capacity, a duplicate event is discarded by
    dedup BEFORE backpressure counts it. Submitting maxQueuedTotal duplicates
    of the same key must leave full capacity available for a distinct event.
    If the order were reversed (backpressure first), each duplicate would consume
    a queue slot and the distinct event would be rejected with FULL_QUEUE.
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router, RouterAdmitResult

    max_queued = 4
    router = Router(
        max_concurrent_invocations=4,
        max_queued_total=max_queued,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine.run,
        delivery_adapter=None,
    )

    # Establish the idempotency key in dedup store via the first event.
    first = await router.handle(make_event(idempotency_key="dup-key", session_key="s1"))
    assert first == RouterAdmitResult.ACCEPTED

    # Submit maxQueuedTotal - 1 more duplicates; dedup must discard them without
    # consuming queue slots (order: dedup BEFORE backpressure).
    for _ in range(max_queued - 1):
        result = await router.handle(make_event(idempotency_key="dup-key", session_key="s1"))
        assert result == RouterAdmitResult.DUPLICATE, (
            "§6.2: duplicate events must be DUPLICATE, not FULL_QUEUE — "
            "dedup MUST precede backpressure (CONTRACT §6.2)"
        )

    # Only 1 slot was consumed (the first ACCEPTED event). The queue must still
    # have capacity for a distinct new event.
    distinct = await router.handle(make_event(idempotency_key="distinct-key", session_key="s2"))
    assert distinct == RouterAdmitResult.ACCEPTED, (
        "§6.2: duplicate events MUST NOT consume queue slots — "
        "distinct event should be accepted because dedup precedes backpressure"
    )

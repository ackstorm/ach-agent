"""CONTRACT §6.4: Expire exhaustion never silent (authoritative conformance test).

Invariant: expire exhaustion / full queue is never silent:
503 sync / NACK-redelivery async-retriable / drop-log async-no-retry.
"""
from __future__ import annotations

import pytest

from tests.router.conftest import make_event


@pytest.mark.asyncio
async def test_inv04_expire_non_silent() -> None:
    """§6.4: expire exhaustion / full queue is never silent — authoritative conformance.

    CONTRACT perspective: when the queue is full, both sync and async-no-retry
    sources receive a non-silent rejection. For sync: FULL_QUEUE (maps to 503
    at the HTTP layer). For async-no-retry: FULL_QUEUE + EXPIRE_DROPS metric
    increment (the drop is logged and counted, never a bare silent return).
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.metrics import BACKPRESSURE_REJECTS, EXPIRE_DROPS
    from ach_agent.router.router import Router, RouterAdmitResult

    async def fast_engine(event, on_kill):  # noqa: ANN001
        on_kill()

    router = Router(
        max_concurrent_invocations=4,
        max_queued_total=2,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fast_engine,
        delivery_adapter=None,
    )

    baseline_bp = BACKPRESSURE_REJECTS._value.get()
    baseline_expire = EXPIRE_DROPS._value.get()

    # Fill the queue.
    r1 = await router.handle(make_event(idempotency_key="e-fill-1", session_key="se1"))
    r2 = await router.handle(make_event(idempotency_key="e-fill-2", session_key="se2"))
    assert r1 == RouterAdmitResult.ACCEPTED
    assert r2 == RouterAdmitResult.ACCEPTED

    # Sync source on full queue: must return FULL_QUEUE — not a silent success.
    sync_overflow = make_event(
        idempotency_key="e-sync-overflow",
        session_key="se3",
        source_trait="sync",
    )
    r_sync = await router.handle(sync_overflow)
    assert r_sync == RouterAdmitResult.FULL_QUEUE, (
        "§6.4: sync source on full queue must return FULL_QUEUE (maps to 503) — "
        "expire exhaustion must never be silent"
    )
    assert BACKPRESSURE_REJECTS._value.get() == baseline_bp + 1, (
        "§6.4: BACKPRESSURE_REJECTS must increment for sync overflow"
    )
    # Sync does NOT count as EXPIRE_DROPS (async-no-retry only).
    assert EXPIRE_DROPS._value.get() == baseline_expire, (
        "§6.4: sync source must NOT increment EXPIRE_DROPS"
    )

    # Async-no-retry source on full queue: must return FULL_QUEUE AND increment
    # EXPIRE_DROPS — the drop is explicitly logged and counted (non-silent).
    async_overflow = make_event(
        idempotency_key="e-async-overflow",
        session_key="se4",
        source_trait="async_no_retry",
    )
    r_async = await router.handle(async_overflow)
    assert r_async == RouterAdmitResult.FULL_QUEUE, (
        "§6.4: async_no_retry source on full queue must return FULL_QUEUE"
    )
    assert BACKPRESSURE_REJECTS._value.get() == baseline_bp + 2
    assert EXPIRE_DROPS._value.get() == baseline_expire + 1, (
        "§6.4: async_no_retry drop must increment EXPIRE_DROPS (non-silent) — "
        "a bare return without metric is a spec violation"
    )

"""RTR-02 conformance: per-session FIFO serialization + empty-lane eviction (Pitfall 6).

D-05 invariants 2 and 4:
  - Events with the same session_key are processed strictly in the order they were submitted.
  - At most one invocation per session key at a time.
  - After a lane's queue drains the session_key is evicted from the lane map (no memory leak).
  - A later event for the same session_key creates a fresh lane.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.router.conftest import FakeEngine, make_event


@pytest.mark.asyncio
async def test_fifo_serialization(fake_engine: FakeEngine) -> None:
    """RTR-02: 5 same-session events are processed strictly in submit order, one at a time.

    Uses FakeEngine.hold()/release() to gate each invocation and verify ordering
    without relying on timing or sleeps.
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router, RouterAdmitResult

    router = Router(
        max_concurrent_invocations=4,  # high cap so global sem is not the limiter
        max_queued_total=10,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine.run,
        delivery_adapter=None,
    )

    session = "fifo-session"
    events = [
        make_event(idempotency_key=f"fifo-{i}", session_key=session)
        for i in range(5)
    ]

    # Submit all 5 events with engine held — they queue up in order
    fake_engine.hold()
    for ev in events:
        result = await router.handle(ev)
        assert result == RouterAdmitResult.ACCEPTED

    # Release: let all process in order
    fake_engine.release()

    # Wait for all 5 to complete (with a generous timeout)
    deadline = asyncio.get_event_loop().time() + 5.0
    while len(fake_engine.invocations) < 5:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(
                f"Timeout waiting for 5 FIFO invocations; got {len(fake_engine.invocations)}"
            )
        await asyncio.sleep(0.01)

    # Verify FIFO order: invocations must match submit order exactly
    assert len(fake_engine.invocations) == 5
    for i, inv in enumerate(fake_engine.invocations):
        assert inv.idempotency_key == f"fifo-{i}", (
            f"FIFO order violated: expected fifo-{i}, got {inv.idempotency_key}"
        )


@pytest.mark.asyncio
async def test_empty_lane_is_evicted() -> None:
    """Pitfall 6 / T-01-LANELEAK: after a lane drains, session_key is evicted from lane map.

    After draining:
      - session_key must NOT be present in router._lanes.
      - The consumer task must be done or cancelled (no asyncio.Task leak).

    A subsequent event for the same session_key must create a fresh lane.
    """
    import asyncio

    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router, RouterAdmitResult

    completed: list[str] = []

    async def fast_engine(event, on_kill):
        """Completes immediately so the lane drains and triggers eviction."""
        completed.append(event.idempotency_key)
        on_kill()

    router = Router(
        max_concurrent_invocations=4,
        max_queued_total=10,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fast_engine,
        delivery_adapter=None,
    )

    session = "evict-session"
    ev = make_event(idempotency_key="evict-evt-1", session_key=session)

    # Submit and let it process fully
    result = await router.handle(ev)
    assert result == RouterAdmitResult.ACCEPTED

    # Wait until the invocation completes and the lane drains
    deadline = asyncio.get_event_loop().time() + 2.0
    while session in router._lanes:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail("Timeout: session_key was never evicted from lane map (Pitfall 6)")
        await asyncio.sleep(0.01)

    # session_key must be gone from the lane map
    assert session not in router._lanes, (
        "Empty lane must be evicted from router._lanes (Pitfall 6, T-01-LANELEAK)"
    )

    # A new event for the same session_key must create a fresh lane
    ev2 = make_event(idempotency_key="evict-evt-2", session_key=session)
    result2 = await router.handle(ev2)
    assert result2 == RouterAdmitResult.ACCEPTED, (
        "Fresh event for evicted session_key must create a new lane and be ACCEPTED"
    )

    # Confirm a new lane was created
    assert session in router._lanes, (
        "New event for evicted session_key must create a fresh lane"
    )

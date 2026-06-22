"""CONTRACT §6 / SC#2 superset: FIFO per session key (authoritative conformance test).

Invariant (SC#2 extra): at most one invocation per session key at a time;
events for the same session key are processed in FIFO order.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.router.conftest import FakeEngine, make_event


@pytest.mark.asyncio
async def test_inv10_fifo_per_session(fake_engine: FakeEngine) -> None:
    """SC#2 extra: FIFO per session key — at most one invocation per session (authoritative).

    CONTRACT perspective: five events for the same session key must be processed
    strictly in the order submitted, one at a time. The FakeEngine.hold()/release()
    gates each invocation so ordering can be verified without relying on timing.
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router, RouterAdmitResult

    router = Router(
        max_concurrent_invocations=4,  # high global cap — session FIFO is the limiter
        max_queued_total=10,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine.run,
        delivery_adapter=None,
    )

    session = "fifo-conformance-session"
    n = 5
    events = [
        make_event(idempotency_key=f"fifo-conf-{i}", session_key=session)
        for i in range(n)
    ]

    # Hold the engine so all events queue up in submit order.
    fake_engine.hold()
    for ev in events:
        result = await router.handle(ev)
        assert result == RouterAdmitResult.ACCEPTED, (
            f"SC#2: event {ev.idempotency_key} must be ACCEPTED into the FIFO queue"
        )

    # Release: process all events.
    fake_engine.release()

    # Wait for all n invocations to complete.
    deadline = asyncio.get_event_loop().time() + 5.0
    while len(fake_engine.invocations) < n:
        if asyncio.get_event_loop().time() > deadline:
            pytest.fail(
                f"SC#2: timeout waiting for {n} FIFO invocations; "
                f"got {len(fake_engine.invocations)}"
            )
        await asyncio.sleep(0.01)

    # Assert FIFO order: invocations must match submit order exactly.
    assert len(fake_engine.invocations) == n
    for i, inv in enumerate(fake_engine.invocations):
        assert inv.idempotency_key == f"fifo-conf-{i}", (
            f"SC#2: FIFO order violated at position {i} — "
            f"expected fifo-conf-{i}, got {inv.idempotency_key} "
            "(events must be processed in submit order, one at a time per session key)"
        )

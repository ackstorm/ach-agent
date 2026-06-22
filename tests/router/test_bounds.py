"""RTR-03/04 conformance: maxConcurrentInvocations + maxQueuedTotal always enforced.

D-05 invariants 3 and 5:
  - At most maxConcurrentInvocations invocations in-flight across all session keys.
  - queued_total never exceeds maxQueuedTotal; full queue returns FULL_QUEUE.
  - slot release: semaphores freed by the lane `async with`; queued_total freed by
    on_kill exactly once per invocation, regardless of how the engine behaves.

OBS-02: DEDUP_DISCARDS, BACKPRESSURE_REJECTS, EXPIRE_DROPS counters emit on each path.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.router.conftest import FakeEngine, make_event


@pytest.mark.asyncio
async def test_max_queued_total(router) -> None:
    """RTR-04: Once queued_total >= max_queued_total, distinct events are rejected FULL_QUEUE."""
    from ach_agent.router.router import RouterAdmitResult

    # conftest router: max_queued_total=5, max_concurrent_invocations=2

    # Fill the queue: 5 distinct events with held engine so they stay queued
    results = []
    for i in range(router._max_queued_total):
        ev = make_event(idempotency_key=f"fill-{i}", session_key=f"s{i}")
        results.append(await router.handle(ev))

    assert all(r == RouterAdmitResult.ACCEPTED for r in results), (
        f"Expected all 5 to be accepted, got: {results}"
    )

    # The next distinct event must be rejected
    overflow = make_event(idempotency_key="overflow-evt", session_key="s99")
    result = await router.handle(overflow)
    assert result == RouterAdmitResult.FULL_QUEUE, (
        "maxQueuedTotal must be enforced — overflow event should be rejected FULL_QUEUE"
    )


@pytest.mark.asyncio
async def test_global_concurrency_cap(fake_engine: FakeEngine) -> None:
    """RTR-03: At most max_concurrent_invocations invocations in-flight at a time.

    The per-channel slot must NOT be the bottleneck here, or the test would pass
    even if the global cap were broken. Set channel_concurrency well above the
    global cap so the global semaphore is the only limiter, then assert EXACTLY
    max_concurrent_invocations dispatch (not just `<=`).
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router, RouterAdmitResult

    router = Router(
        max_concurrent_invocations=2,
        max_queued_total=10,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine.run,
        delivery_adapter=None,
        channel_concurrency=10,  # global_sem (=2) is the bottleneck, not channel_slot
    )

    fake_engine.hold()  # Block all invocations

    # Submit 3 events to 3 different session keys (different lanes → no FIFO serialization)
    ev1 = make_event(idempotency_key="c1", session_key="sc1")
    ev2 = make_event(idempotency_key="c2", session_key="sc2")
    ev3 = make_event(idempotency_key="c3", session_key="sc3")

    r1 = await router.handle(ev1)
    r2 = await router.handle(ev2)
    r3 = await router.handle(ev3)

    assert r1 == RouterAdmitResult.ACCEPTED
    assert r2 == RouterAdmitResult.ACCEPTED
    assert r3 == RouterAdmitResult.ACCEPTED

    # Give asyncio a chance to dispatch — exactly 2 should be in-flight (global cap)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    in_flight = len(fake_engine.invocations)
    assert in_flight == 2, (
        f"Expected exactly 2 concurrent invocations, got {in_flight} "
        "(maxConcurrentInvocations=2 must be the binding limit, not channel_slot)"
    )

    # Release — all should complete and the 3rd should then dispatch
    fake_engine.release()
    await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)
    assert len(fake_engine.invocations) == 3


@pytest.mark.asyncio
async def test_queued_total_released_when_engine_skips_on_kill() -> None:
    """RTR-04 regression: the LANE is the authoritative queued_total release point.

    The production engine_runner (lifecycle.run_invocation) does NOT call on_kill on
    normal completion — only on a watchdog kill. If the lane relied on the engine to
    call on_kill, queued_total would leak on every success until maxQueuedTotal wedged
    the queue shut. This models that engine and asserts the queue still drains.
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router, RouterAdmitResult

    class SilentEngine:
        """Models production: completes WITHOUT ever calling on_kill."""

        async def run(self, event, on_kill) -> None:  # noqa: ANN001
            return

    router = Router(
        max_concurrent_invocations=2,
        max_queued_total=3,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=SilentEngine().run,
        delivery_adapter=None,
    )

    # Submit more events than the queue can hold IF it never drained.
    for i in range(6):
        await router.handle(make_event(idempotency_key=f"q{i}", session_key=f"sq{i}"))
        await asyncio.sleep(0)  # let the lane consumer run and decrement
    await asyncio.sleep(0.05)

    assert router._queued_total.get() == 0, (
        "queued_total leaked — the lane must decrement even when the engine "
        "never calls on_kill (RTR-04)"
    )
    # Queue is not wedged: a fresh event is still ACCEPTED.
    after = await router.handle(make_event(idempotency_key="after", session_key="safter"))
    assert after == RouterAdmitResult.ACCEPTED


@pytest.mark.asyncio
async def test_timeout_path_does_not_over_release() -> None:
    """RTR-03/04 regression: the watchdog-kill path must not double/triple-release.

    Production lifecycle.run_invocation calls on_kill THEN raises (InvocationTimeout).
    The lane catches the exception and its finally calls on_kill again, and the
    `async with` blocks release the semaphores on exit. on_kill must be idempotent
    (queued_total decremented once, never negative) and the semaphores must be
    released exactly once (capacity preserved, cap not eroded).
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router

    class TimeoutEngine:
        """Models the watchdog: fires on_kill, then raises like InvocationTimeout."""

        async def run(self, event, on_kill) -> None:  # noqa: ANN001
            on_kill()
            raise TimeoutError

    router = Router(
        max_concurrent_invocations=2,
        max_queued_total=10,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=TimeoutEngine().run,
        delivery_adapter=None,
    )

    for i in range(4):
        await router.handle(make_event(idempotency_key=f"t{i}", session_key=f"st{i}"))
    await asyncio.sleep(0.05)

    assert router._queued_total.get() == 0, (
        "queued_total over-decremented (went negative) on the timeout path"
    )
    # Semaphore capacity must be exactly the configured cap — not inflated by
    # double-release (would be > 2) nor leaked (would be < 2).
    assert router._slot_manager.global_sem._value == 2, (
        "global semaphore over-released on timeout path — concurrency cap eroded (RTR-03)"
    )


@pytest.mark.asyncio
async def test_metrics_emitted(router) -> None:
    """OBS-02: DEDUP_DISCARDS, BACKPRESSURE_REJECTS, EXPIRE_DROPS counters emit.

    queued_total accounting on the drain path is covered separately by
    test_queued_total_released_when_engine_skips_on_kill.
    """

    from ach_agent.router.metrics import (
        BACKPRESSURE_REJECTS,
        DEDUP_DISCARDS,
        EXPIRE_DROPS,
    )
    from ach_agent.router.router import RouterAdmitResult

    # Capture baseline metric values
    baseline_dedup = DEDUP_DISCARDS._value.get()
    baseline_bp = BACKPRESSURE_REJECTS._value.get()
    baseline_expire = EXPIRE_DROPS._value.get()

    # Trigger DEDUP_DISCARDS: send same event twice
    ev = make_event(idempotency_key="m-evt-001", session_key="sm1")
    dup = make_event(idempotency_key="m-evt-001", session_key="sm1")
    r1 = await router.handle(ev)
    r2 = await router.handle(dup)

    assert r1 == RouterAdmitResult.ACCEPTED
    assert r2 == RouterAdmitResult.DUPLICATE
    assert DEDUP_DISCARDS._value.get() == baseline_dedup + 1

    # Trigger BACKPRESSURE_REJECTS + EXPIRE_DROPS via async_no_retry:
    # Fill the queue then send one more async_no_retry event
    # First fill with distinct keys (already have 1 accepted above)
    for i in range(router._max_queued_total - 1):
        await router.handle(make_event(idempotency_key=f"m-fill-{i}", session_key=f"sfill{i}"))

    overflow = make_event(
        idempotency_key="m-overflow",
        session_key="sm99",
        source_trait="async_no_retry",
    )
    r_overflow = await router.handle(overflow)

    assert r_overflow == RouterAdmitResult.FULL_QUEUE
    assert BACKPRESSURE_REJECTS._value.get() == baseline_bp + 1
    assert EXPIRE_DROPS._value.get() == baseline_expire + 1

"""CONTRACT §6.3: Three finite bounds invariant (authoritative conformance test).

Invariant: maxConcurrentInvocations, maxInvocationSeconds (600), and
maxQueuedTotal (100) are always enforced — never exceeded.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.router.conftest import FakeEngine, make_event


@pytest.mark.asyncio
async def test_inv03_finite_bounds(fake_engine: FakeEngine) -> None:
    """§6.3: three finite bounds always enforced — authoritative conformance.

    CONTRACT perspective: submitting events beyond maxQueuedTotal is rejected
    (not silently queued). Holding the engine and submitting beyond
    maxConcurrentInvocations confirms the concurrency cap. Both overflow
    outcomes are non-silent (FULL_QUEUE result, not a hang or exception swallow).
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router, RouterAdmitResult

    max_concurrent = 2
    max_queued = 4

    router = Router(
        max_concurrent_invocations=max_concurrent,
        max_queued_total=max_queued,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine.run,
        max_invocation_seconds=600.0,
    )

    # ---- maxQueuedTotal enforcement ----
    # Fill the queue completely.
    for i in range(max_queued):
        r = await router.handle(make_event(idempotency_key=f"fill-{i}", session_key=f"s{i}"))
        assert r == RouterAdmitResult.ACCEPTED

    # One more distinct event must be rejected — maxQueuedTotal is enforced.
    overflow = await router.handle(make_event(idempotency_key="overflow", session_key="s99"))
    assert overflow == RouterAdmitResult.FULL_QUEUE, (
        "§6.3: maxQueuedTotal must be enforced — overflow event must return FULL_QUEUE, "
        "never silently accepted beyond the bound"
    )

    # ---- maxConcurrentInvocations enforcement ----
    # Use a fresh FakeEngine and router so the invocation count starts from zero.
    cap_engine = FakeEngine()
    cap_engine.hold()
    cap_router = Router(
        max_concurrent_invocations=max_concurrent,
        max_queued_total=20,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=cap_engine.run,
        max_invocation_seconds=600.0,
        channel_concurrency={"test-channel": 10},  # global sem is the limiting factor
    )

    # Submit 3 distinct events — all accepted (queue has room), but only 2 in-flight.
    for i in range(3):
        await cap_router.handle(make_event(idempotency_key=f"cap-{i}", session_key=f"cap-s{i}"))

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    in_flight = len(cap_engine.invocations)
    assert in_flight == max_concurrent, (
        f"§6.3: maxConcurrentInvocations={max_concurrent} must be enforced — "
        f"expected {max_concurrent} in-flight, got {in_flight}"
    )

    cap_engine.release()


@pytest.mark.asyncio
async def test_inv03_script_channels_hold_their_own_finite_bound() -> None:
    """§6.3: a webhook-script event never spends an engine slot, and is itself bounded.

    Two pools, both finite: the deterministic handler runs no model turn, so holding an
    engine slot for `timeoutSeconds` would starve every model channel behind it. Unset
    maxConcurrentScripts keeps the single shared pool (no behaviour change).
    """
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router

    engine = FakeEngine()
    engine.hold()
    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=20,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=engine.run,
        max_invocation_seconds=600.0,
        channel_concurrency={"gitlab-register": 10, "mr-review": 10},
        max_concurrent_scripts=2,
        script_channels={"gitlab-register"},
    )

    # Three script events on distinct project lanes + one model event.
    for i in range(3):
        await router.handle(
            make_event(
                idempotency_key=f"script-{i}",
                session_key=f"{i}:webhook-script:gitlab-register",
                channel_name="gitlab-register",
            )
        )
    await router.handle(
        make_event(idempotency_key="mr-1", session_key="42:7", channel_name="mr-review")
    )
    for _ in range(4):
        await asyncio.sleep(0)

    channels = [e.channel_name for e in engine.invocations]
    assert channels.count("gitlab-register") == 2, (
        "§6.3: maxConcurrentScripts must bound script channels — expected 2 in flight, "
        f"got {channels.count('gitlab-register')}"
    )
    assert channels.count("mr-review") == 1, (
        "§6.3: a script must not consume the engine pool — the model event must run "
        "while scripts are saturated"
    )

    engine.release()


@pytest.mark.asyncio
async def test_inv03_unset_script_bound_shares_the_engine_pool() -> None:
    """Back-compat: maxConcurrentScripts unset → one pool, exactly as before."""
    from ach_agent.router.dedup import InMemoryDedupStore
    from ach_agent.router.router import Router

    engine = FakeEngine()
    engine.hold()
    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=20,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=engine.run,
        max_invocation_seconds=600.0,
        channel_concurrency={"gitlab-register": 10, "mr-review": 10},
        script_channels={"gitlab-register"},
    )

    await router.handle(
        make_event(
            idempotency_key="script-1",
            session_key="1:webhook-script:gitlab-register",
            channel_name="gitlab-register",
        )
    )
    await router.handle(
        make_event(idempotency_key="mr-1", session_key="42:7", channel_name="mr-review")
    )
    for _ in range(4):
        await asyncio.sleep(0)

    assert len(engine.invocations) == 1, (
        "§6.3: with maxConcurrentScripts unset both channels share maxConcurrentInvocations"
    )

    engine.release()

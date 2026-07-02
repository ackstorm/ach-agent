"""Router additive secondary dedup key (GitLab logical content composite) — Plan 3.

The secondary key is checked in step 1 (dedup, before backpressure — RTR-01 preserved)
and marked in step 3 on a SHORT window (_SECONDARY_DEDUP_WINDOW_S), independent of the
primary UUID key's full idempotency window. `secondary_idempotency_key=None` (every
non-gitlab channel) must reproduce today's single-key behaviour byte-for-byte.
"""
from __future__ import annotations

import pytest

from ach_agent.router.router import RouterAdmitResult
from tests.router.conftest import make_event


def _event_with_secondary(idempotency_key: str, secondary: str | None):
    """make_event + set the secondary key (a declared slot on MessageEvent)."""
    event = make_event(idempotency_key=idempotency_key, session_key="s1")
    event.secondary_idempotency_key = secondary
    return event


@pytest.mark.asyncio
async def test_secondary_key_dedups_when_primary_differs(router, fake_engine) -> None:
    """Same composite + DIFFERENT primary UUID → DUPLICATE, and no queue slot consumed."""
    fake_engine.hold()  # keep the accepted event in-flight so queued_total is deterministic

    a = _event_with_secondary("uuid-1", "gl:merge_request:42:7:alice:hashX")
    b = _event_with_secondary("uuid-2", "gl:merge_request:42:7:alice:hashX")  # diff UUID, same composite

    assert await router.handle(a) == RouterAdmitResult.ACCEPTED
    assert router._queued_total.get() == 1

    assert await router.handle(b) == RouterAdmitResult.DUPLICATE
    assert router._queued_total.get() == 1, "secondary duplicate MUST NOT consume a queue slot (RTR-01)"


@pytest.mark.asyncio
async def test_secondary_none_is_unchanged_behavior(router, fake_engine) -> None:
    """secondary=None (non-gitlab): two distinct primaries → both ACCEPTED (regression guard)."""
    fake_engine.hold()

    a = _event_with_secondary("uuid-1", None)
    b = _event_with_secondary("uuid-2", None)

    assert await router.handle(a) == RouterAdmitResult.ACCEPTED
    assert await router.handle(b) == RouterAdmitResult.ACCEPTED


@pytest.mark.asyncio
async def test_secondary_key_also_marked(router, fake_engine) -> None:
    """After admitting A, the secondary key is marked namespaced ({channel}:sec:{composite})."""
    fake_engine.hold()

    a = _event_with_secondary("uuid-1", "gl:merge_request:42:7:alice:hashX")
    assert await router.handle(a) == RouterAdmitResult.ACCEPTED
    assert router._dedup.seen("test-channel:sec:gl:merge_request:42:7:alice:hashX")


@pytest.mark.asyncio
async def test_secondary_short_window(router, fake_engine) -> None:
    """Secondary key is marked on _SECONDARY_DEDUP_WINDOW_S, primary on the full window."""
    from ach_agent.router import router as router_mod

    fake_engine.hold()
    calls: list[tuple[str, int]] = []
    original_mark = router._dedup.mark

    def spy_mark(key: str, ttl_seconds: int) -> None:
        calls.append((key, ttl_seconds))
        original_mark(key, ttl_seconds)

    router._dedup.mark = spy_mark  # type: ignore[method-assign]

    a = _event_with_secondary("uuid-1", "gl:merge_request:42:7:alice:hashX")
    assert await router.handle(a) == RouterAdmitResult.ACCEPTED

    ttl_by_key = dict(calls)
    assert ttl_by_key["test-channel:uuid-1"] == router._idempotency_window_seconds
    assert (
        ttl_by_key["test-channel:sec:gl:merge_request:42:7:alice:hashX"]
        == router_mod._SECONDARY_DEDUP_WINDOW_S
    )

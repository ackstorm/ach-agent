from __future__ import annotations

import asyncio

import pytest

from ach_agent.boot.completions import Admission, CompletionRegistry
from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.dedup import InMemoryDedupStore
from ach_agent.router.router import Router, RouterAdmitResult


def event(key: str = "raw-1", channel: str = "webhook") -> MessageEvent:
    return MessageEvent(idempotency_key=key, session_key="s", channel_name=channel)


@pytest.mark.asyncio
async def test_submission_coalesces_and_disconnect_does_not_cancel_admitted_work() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(received: MessageEvent) -> RouterAdmitResult:
        started.set()
        await release.wait()
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(handle)
    first = asyncio.create_task(registry.submit(event()))
    await started.wait()
    second = asyncio.create_task(registry.submit(event()))
    await asyncio.sleep(0)
    assert not second.done()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    result = await second

    assert result.admission is Admission.DUPLICATE
    assert result.completion is not None
    assert result.completion.state == "queued"
    assert registry.lookup(result.completion.ref).state == "queued"


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_cancel_work_and_finish_wakes_waiter() -> None:
    async def handle(received: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(handle)
    submission = await registry.submit(event())
    assert submission.completion is not None
    ref = submission.completion.ref
    waiter = asyncio.create_task(registry.wait(ref))
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await registry.finish(ref, {"text": "done"}, None)
    assert registry.lookup(ref).state == "completed"


@pytest.mark.asyncio
async def test_duplicate_without_retained_result_is_explicitly_unavailable() -> None:
    calls = 0

    async def handle(received: MessageEvent) -> RouterAdmitResult:
        nonlocal calls
        calls += 1
        return RouterAdmitResult.DUPLICATE

    registry = CompletionRegistry(handle)
    result = await registry.submit(event())

    assert result.admission is Admission.DUPLICATE
    assert result.completion is not None
    assert result.completion.state == "outcome_unavailable"
    assert calls == 1


@pytest.mark.asyncio
async def test_full_queue_does_not_leave_a_reservation() -> None:
    async def handle(received: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.FULL_QUEUE

    registry = CompletionRegistry(handle)
    result = await registry.submit(event())
    assert result.admission is Admission.FULL_QUEUE
    assert registry.active_count == 0


@pytest.mark.asyncio
async def test_registry_preserves_router_full_queue_admission() -> None:
    async def run_engine(received: MessageEvent, on_kill: object) -> None:
        return None

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=0,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=run_engine,
        max_invocation_seconds=1,
    )
    registry = CompletionRegistry(router.handle)

    result = await registry.submit(event())

    assert result.admission is Admission.FULL_QUEUE
    assert result.completion is None

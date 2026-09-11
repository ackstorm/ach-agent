from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from ach_agent.boot.completions import Admission, Completion, CompletionRegistry, RegistryBusy
from ach_agent.channels.envelopes import EventRef
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


@pytest.mark.asyncio
async def test_real_router_dedup_precedes_backpressure_at_queue_cap() -> None:
    async def run_engine(received: MessageEvent, on_kill: object) -> None:
        return None

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=1,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=run_engine,
        max_invocation_seconds=1,
    )
    first = await router.handle(event("same"))
    duplicate = await router.handle(event("same"))

    assert first is RouterAdmitResult.ACCEPTED
    assert duplicate is RouterAdmitResult.DUPLICATE


@pytest.mark.asyncio
async def test_registry_capacity_raises_without_bypassing_router() -> None:
    calls = 0

    async def handle(received: MessageEvent) -> RouterAdmitResult:
        nonlocal calls
        calls += 1
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(handle, max_active_entries=1)
    first = await registry.submit(event("first"))
    assert first.completion is not None

    with pytest.raises(RegistryBusy):
        await registry.submit(event("second"))
    assert calls == 1


@pytest.mark.asyncio
async def test_admission_failure_wakes_completion_waiter() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def handle(received: MessageEvent) -> RouterAdmitResult:
        started.set()
        await release.wait()
        raise RuntimeError("router unavailable")

    registry = CompletionRegistry(handle)
    submitting = asyncio.create_task(registry.submit(event()))
    await started.wait()
    ref = registry.lookup(
        EventRef(agent="default", channel_name="webhook", idempotency_key="raw-1")
    ).ref
    waiting = asyncio.create_task(registry.wait(ref))
    release.set()
    with pytest.raises(RuntimeError):
        await submitting
    result = await asyncio.wait_for(waiting, 1)
    assert result.state == "outcome_unavailable"


@pytest.mark.asyncio
async def test_retention_and_entry_bounds_use_injected_clock() -> None:
    now = [0.0]

    async def handle(received: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(
        handle, retention_seconds=10, max_completed_entries=1, clock=lambda: now[0]
    )
    first = await registry.submit(event("first"))
    second = await registry.submit(event("second"))
    assert first.completion is not None and second.completion is not None
    await registry.finish(first.completion.ref, {"n": 1})
    await registry.finish(second.completion.ref, {"n": 2})
    assert registry.lookup(first.completion.ref).state == "outcome_unavailable"
    assert registry.lookup(second.completion.ref).state == "completed"
    now[0] = 11
    assert registry.lookup(second.completion.ref).state == "outcome_unavailable"


@pytest.mark.asyncio
async def test_aggregate_result_bound_evicts_oldest_completion() -> None:
    async def handle(received: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(handle, max_completed_bytes=260)
    first = await registry.submit(event("first"))
    second = await registry.submit(event("second"))
    assert first.completion is not None and second.completion is not None
    await registry.finish(first.completion.ref, {"text": "a" * 40})
    await registry.finish(second.completion.ref, {"text": "b" * 40})
    assert registry.lookup(first.completion.ref).state == "outcome_unavailable"
    assert registry.lookup(second.completion.ref).state == "completed"


@pytest.mark.asyncio
async def test_waiter_limit_releases_after_cancellation() -> None:
    async def handle(received: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(handle, max_waiters=1)
    submission = await registry.submit(event())
    assert submission.completion is not None
    ref = submission.completion.ref
    first = asyncio.create_task(registry.wait(ref))
    await asyncio.sleep(0)
    second = await registry.wait(ref)
    assert second.state == "outcome_unavailable"
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    third = asyncio.create_task(registry.wait(ref))
    await registry.finish(ref, {"ok": True})
    assert (await third).state == "completed"


@pytest.mark.asyncio
async def test_mark_running_and_first_terminal_result_win() -> None:
    async def handle(received: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(handle)
    submission = await registry.submit(event())
    assert submission.completion is not None
    ref = submission.completion.ref
    await registry.mark_running(ref)
    assert registry.lookup(ref).state == "running"
    await registry.finish(ref, {"winner": 1})
    await registry.finish(ref, {"winner": 2}, "late")
    first_snapshot = registry.lookup(ref)
    assert first_snapshot.result == {"winner": 1}
    assert isinstance(first_snapshot.result, dict)
    first_snapshot.result["winner"] = 99
    assert registry.lookup(ref).result == {"winner": 1}


@pytest.mark.asyncio
async def test_finish_defensively_copies_caller_result() -> None:
    async def handle(received: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(handle)
    submission = await registry.submit(event())
    assert submission.completion is not None
    original = {"items": ["before"]}
    await registry.finish(submission.completion.ref, original)
    original["items"].append("after")
    assert registry.lookup(submission.completion.ref).result == {"items": ["before"]}


@pytest.mark.asyncio
async def test_channel_namespace_and_secondary_router_dedup() -> None:
    async def run_engine(received: MessageEvent, on_kill: object) -> None:
        return None

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=5,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=run_engine,
        max_invocation_seconds=1,
    )
    registry = CompletionRegistry(router.handle)
    left = await registry.submit(event("same", "webhook"))
    right = await registry.submit(event("same", "a2a"))
    assert left.admission is Admission.ACCEPTED
    assert right.admission is Admission.ACCEPTED

    duplicate_event = event("other", "webhook")
    duplicate_event.secondary_idempotency_key = "secondary"
    first_secondary = await registry.submit(duplicate_event)
    duplicate_event2 = event("other-2", "webhook")
    duplicate_event2.secondary_idempotency_key = "secondary"
    second_secondary = await registry.submit(duplicate_event2)
    assert first_secondary.admission is Admission.ACCEPTED
    assert second_secondary.admission is Admission.DUPLICATE
    assert second_secondary.completion is not None
    assert second_secondary.completion.state == "outcome_unavailable"


@pytest.mark.asyncio
async def test_cancelled_sole_submit_still_retrieves_router_exception() -> None:
    release = asyncio.Event()

    async def handle(received: MessageEvent) -> RouterAdmitResult:
        await release.wait()
        raise RuntimeError("boom")

    registry = CompletionRegistry(handle)
    submitting = asyncio.create_task(registry.submit(event()))
    await asyncio.sleep(0)
    submitting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitting
    release.set()
    await asyncio.sleep(0.05)


def test_completion_rejects_nonfinite_result() -> None:
    with pytest.raises(ValidationError):
        Completion(
            ref=EventRef(agent="a", channel_name="c", idempotency_key="k"),
            invocation_id="i",
            state="completed",
            result={"value": float("inf")},
        )

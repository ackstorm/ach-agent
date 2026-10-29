# SPDX-License-Identifier: MIT
"""Admission — dedup -> capacity -> FIFO-per-(Workspace,session) (contract §3).

Round-1 regression tests added per `task-3-admission-review.md` findings 1-9 (sections
marked below). No `pytest-asyncio` dependency (pyproject.toml is frozen to the Task 4
worker this round) — every async scenario is a plain `def test_*()` driving an inner
coroutine via `asyncio.run(...)`. Timing is controlled via an injected fake clock plus
`asyncio.Event` gates on the fake runner; `admission._permit_poll_seconds` is set small in
every test so the bounded internal permit-poll loop settles fast. The only `sleep` used
for a "nothing happened" assertion is a small multiple of that same configured poll
interval (not an unrelated arbitrary constant) — per the minor finding, replacing the
previous fixed 50ms sleeps.
"""

from __future__ import annotations

import asyncio
import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.harness.admission import (
    Admission,
    AdmissionClosedError,
    AdmissionQueueFullError,
    AdmissionTicket,
    _Lane,
)
from ach_runtime.harness.refs import invocation_digest, session_ref as compute_session_ref
from ach_runtime.harness.refs import workspace_ref as compute_workspace_ref

NAMESPACE = "ach"
AGENT_UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
POLL = 0.005


def _routed(event_id: str, *, channel: str = "ch", workspace_key: str = "ws1", session_key: str = "s1") -> RoutedEvent:
    return RoutedEvent(
        event=Event(event_id=event_id, channel=channel, type="t", source="webhook"),
        workspace_key=workspace_key,
        session_key=session_key,
    )


def _script_event(event_id: str, *, channel: str = "script", project_id: str = "42") -> Event:
    return Event(
        event_id=event_id, channel=channel, type="merge_request", source="gitlab",
        context={"gitlab": {"projectId": project_id, "mergeRequestIid": "7"}},
    )


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class GatedRunner:
    """Records calls; each call awaits a per-event gate (opened via `release`) before
    returning, and calls `ticket.mark_started()` immediately (the default)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._gates: dict[str, asyncio.Event] = {}

    def gate(self, event_id: str) -> asyncio.Event:
        return self._gates.setdefault(event_id, asyncio.Event())

    def release(self, event_id: str) -> None:
        self.gate(event_id).set()

    async def __call__(self, routed_event: RoutedEvent, ticket: AdmissionTicket) -> None:
        self.calls.append(routed_event.event_id)
        ticket.mark_started()
        await self.gate(routed_event.event_id).wait()


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while not predicate():
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.005)


def _admission(
    runner,
    *,
    clock: FakeClock,
    namespace: str = NAMESPACE,
    agent_uid: str = AGENT_UID,
    max_concurrent_invocations: int = 10,
    max_queued_total: int = 10,
    idempotency_window_seconds: int = 60,
    max_invocation_seconds: int = 600,
    workspace_max_concurrent_sessions: int = 10,
    channel_concurrency: dict[str, int] | None = None,
    max_concurrent_scripts: int | None = None,
    alarm=None,
) -> Admission:
    options = {}
    if max_concurrent_scripts is not None:
        options["max_concurrent_scripts"] = max_concurrent_scripts
    return Admission(
        namespace=namespace,
        agent_uid=agent_uid,
        max_concurrent_invocations=max_concurrent_invocations,
        max_queued_total=max_queued_total,
        idempotency_window_seconds=idempotency_window_seconds,
        max_invocation_seconds=max_invocation_seconds,
        workspace_max_concurrent_sessions=workspace_max_concurrent_sessions,
        channel_concurrency=channel_concurrency,
        clock=clock,
        runner=runner,
        alarm=alarm,
        permit_poll_seconds=POLL,
        **options,
    )


# ---------------------------------------------------------------------------
# dedup before capacity; duplicate returns the ORIGINAL ticket
# ---------------------------------------------------------------------------


def test_duplicate_returns_original_ticket_even_with_full_queue() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, max_queued_total=1)

        first = admission.admit(_routed("evt-1", session_key="s1"))
        await _wait_until(lambda: "evt-1" in runner.calls)

        dup = admission.admit(_routed("evt-1", session_key="s1"))
        assert dup is first, "a duplicate must return the ORIGINAL ticket, not raise"

        runner.release("evt-1")
        await first.completed

    asyncio.run(scenario())


def test_completed_future_forwards_opaque_runner_result_unchanged() -> None:
    async def scenario() -> None:
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        raw_result = {"text": '{"action":"a2a_reply","text":"hello"}'}

        async def runner(_routed_event, ticket):
            ticket.mark_started()
            return raw_result

        admission = _admission(runner, clock=clock)
        ticket = admission.admit(_routed("opaque-result"))
        await ticket.started
        assert await ticket.completed is raw_result
        await admission.close()

    asyncio.run(scenario())


def test_new_event_rejected_when_queue_full_and_not_dedup_marked() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        alarms = []
        admission = _admission(runner, clock=clock, max_queued_total=1,
                               alarm=lambda reason, ticket: alarms.append((reason, ticket)))

        first = admission.admit(_routed("evt-1", session_key="s1"))
        await _wait_until(lambda: "evt-1" in runner.calls)

        with pytest.raises(AdmissionQueueFullError):
            admission.admit(_routed("evt-2", session_key="s2"))
        assert alarms == [("admission queue capacity rejected", None)]

        runner.release("evt-1")
        await first.completed

        second = admission.admit(_routed("evt-2", session_key="s2"))
        await _wait_until(lambda: "evt-2" in runner.calls)
        runner.release("evt-2")
        await second.completed

    asyncio.run(scenario())


def test_script_capacity_rejection_is_counted_after_dedup() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        alarms = []
        admission = _admission(runner, clock=clock, max_queued_total=1,
                               alarm=lambda reason, ticket: alarms.append((reason, ticket)))
        first = admission.admit(_routed("occupied"))
        await _wait_until(lambda: "occupied" in runner.calls)
        script = _script_event("script-overflow")
        with pytest.raises(AdmissionQueueFullError):
            admission.admit_script(script)
        assert alarms == [("admission queue capacity rejected", None)]
        runner.release("occupied")
        await first.completed

    asyncio.run(scenario())


def test_script_started_at_spawn_duplicate_retains_ticket_after_caller_cancellation() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        allow_start = asyncio.Event()
        finish = asyncio.Event()
        calls: list[str] = []

        async def runner(event, ticket) -> None:
            calls.append(event.event_id)
            entered.set()
            await allow_start.wait()
            ticket.mark_started()
            await finish.wait()

        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)
        first = admission.admit_script(_script_event("script-1"))
        async def wait_started() -> None:
            await asyncio.shield(first.started)

        caller = asyncio.create_task(wait_started())
        await entered.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert not first.started.cancelled()
        assert type(first).__name__ == "ScriptAdmissionTicket"
        assert not hasattr(first, "workspace_ref")
        assert not hasattr(first, "session_ref")
        assert not hasattr(first, "routed_event")
        assert not admission.has_work("w" * 64)
        original_deadline = first.deadline_at
        duplicate = admission.admit_script(_script_event("script-1"))
        assert duplicate is first
        assert duplicate.deadline_at is original_deadline
        assert calls.count("script-1") == 1
        allow_start.set()
        await first.started
        finish.set()
        await first.completed
        await admission.close()

    asyncio.run(scenario())


def test_script_lane_is_fifo_per_project_and_queue_is_shared() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, max_queued_total=2)
        first = admission.admit_script(_script_event("script-1", project_id="42"))
        await _wait_until(lambda: "script-1" in runner.calls)
        second = admission.admit_script(_script_event("script-2", project_id="42"))
        with pytest.raises(AdmissionQueueFullError):
            admission.admit_script(_script_event("script-3", project_id="77"))
        assert admission.queued_total == 2
        await asyncio.sleep(POLL * 3)
        assert runner.calls == ["script-1"]
        runner.release("script-1")
        await first.completed
        await _wait_until(lambda: "script-2" in runner.calls)
        runner.release("script-2")
        await second.completed

        agent = admission.admit(_routed("agent-queue"))
        await _wait_until(lambda: "agent-queue" in runner.calls)
        mixed_script = admission.admit_script(_script_event("script-with-agent"))
        with pytest.raises(AdmissionQueueFullError):
            admission.admit_script(_script_event("script-over-total-capacity"))
        assert admission.queued_total == 2
        runner.release("agent-queue")
        runner.release("script-with-agent")
        await mixed_script.completed
        await agent.completed
        await admission.close()

    asyncio.run(scenario())


def test_script_explicit_pool_progresses_while_agent_uses_global_pool() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=1, max_concurrent_scripts=1,
        )
        agent = admission.admit(_routed("agent-1"))
        await _wait_until(lambda: "agent-1" in runner.calls)
        script = admission.admit_script(_script_event("script-1"))
        await _wait_until(lambda: "script-1" in runner.calls)
        assert script.started.done()
        runner.release("script-1")
        runner.release("agent-1")
        await asyncio.gather(agent.completed, script.completed)
        await admission.close()

    asyncio.run(scenario())


def test_explicit_script_pool_ignores_recovered_engine_global_occupancy() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=1, max_concurrent_scripts=1,
        )
        workspace = compute_workspace_ref(NAMESPACE, AGENT_UID, "recovered-ws")
        session = compute_session_ref(workspace, "recovered-session")
        recovered_gate = asyncio.Event()
        admission.recover(
            invocation_id="recovered-run", channel="engine", workspace_ref=workspace,
            session_ref=session, config_version="v1", deadline_at=clock.now + timedelta(minutes=5),
            instance_id="pod-1", run_id="run-1", observer=recovered_gate.wait,
        )
        script = admission.admit_script(_script_event("script-independent"))
        await _wait_until(lambda: "script-independent" in runner.calls)
        runner.release("script-independent")
        await script.completed
        recovered_gate.set()
        await admission.close()

    asyncio.run(scenario())


def test_script_default_pool_shares_global_capacity() -> None:
    """M2: an isolated oracle for the default (unnamed) script pool sharing the SAME
    global concurrency slot as an ordinary agent invocation -- capacity=1 overall means
    a script cannot start while the agent occupies the only slot, independent of any
    per-channel cap."""
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, max_concurrent_invocations=1)
        agent = admission.admit(_routed("agent-1"))
        await _wait_until(lambda: "agent-1" in runner.calls)
        script = admission.admit_script(_script_event("script-1"))
        await asyncio.sleep(POLL * 3)
        assert "script-1" not in runner.calls
        runner.release("agent-1")
        await agent.completed
        await _wait_until(lambda: "script-1" in runner.calls)
        runner.release("script-1")
        await script.completed
        await admission.close()

    asyncio.run(scenario())


def test_script_channel_limit_with_spare_global_and_script_capacity() -> None:
    """M2: an isolated oracle for the per-channel cap alone -- global (3) and the script
    pool (3) both have spare capacity throughout, across two distinct project lanes, so
    the second script can only be blocked by `channel_concurrency["script"]=1`, never by
    exhausting either pool. Releasing the first admits the second."""
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=3, max_concurrent_scripts=3,
            channel_concurrency={"script": 1},
        )
        first = admission.admit_script(_script_event("script-2", project_id="42"))
        await _wait_until(lambda: "script-2" in runner.calls)
        second = admission.admit_script(_script_event("script-3", project_id="77"))
        await asyncio.sleep(POLL * 3)
        assert "script-3" not in runner.calls
        runner.release("script-2")
        await first.completed
        await _wait_until(lambda: "script-3" in runner.calls)
        runner.release("script-3")
        await second.completed
        await admission.close()

    asyncio.run(scenario())


def test_admission_close_cancels_and_retires_active_script_ticket() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)
        ticket = admission.admit_script(_script_event("script-close"))
        await _wait_until(lambda: "script-close" in runner.calls)
        await admission.close()
        with pytest.raises(asyncio.CancelledError):
            await ticket.completed
        assert admission.queued_total == 0
        assert not admission._lanes
        assert not admission._monitors

    asyncio.run(scenario())


def test_script_admission_expiration_fails_started_before_runner() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, max_invocation_seconds=1)
        ticket = admission.admit_script(_script_event("expired-script"))
        deadline = ticket.deadline_at
        duplicate = admission.admit_script(_script_event("expired-script"))
        assert duplicate is ticket
        assert duplicate.deadline_at is deadline
        clock.advance(2)
        await _wait_until(lambda: ticket.started.done())
        assert isinstance(ticket.started.exception(), TimeoutError)
        assert runner.calls == []
        with pytest.raises(TimeoutError):
            await ticket.completed
        await admission.close()

    asyncio.run(scenario())


def test_script_post_spawn_failure_keeps_started_and_emits_alarm() -> None:
    async def scenario() -> None:
        alarmed: list[tuple[str, object]] = []

        async def runner(event, ticket) -> None:
            ticket.mark_started()
            raise RuntimeError("script exit failure")

        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, alarm=lambda reason, ticket: alarmed.append((reason, ticket)),
        )
        ticket = admission.admit_script(_script_event("failed-script"))
        await ticket.started
        with pytest.raises(RuntimeError, match="script exit failure"):
            await ticket.completed
        assert ticket.started.exception() is None
        assert alarmed == [("webhook script runner failed", ticket)]
        assert admission.admit_script(_script_event("failed-script")) is ticket
        await admission.close()

    asyncio.run(scenario())


def test_in_flight_duplicate_retained_past_idempotency_window() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=1)

        first = admission.admit(_routed("evt-1"))
        await _wait_until(lambda: "evt-1" in runner.calls)

        clock.advance(10_000)
        dup = admission.admit(_routed("evt-1"))
        assert dup is first, "in-flight dedup must be retained independently of expiry"

        runner.release("evt-1")
        await first.completed

    asyncio.run(scenario())


def test_completed_ticket_reused_within_idempotency_window() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=60)

        first = admission.admit(_routed("evt-1"))
        await _wait_until(lambda: "evt-1" in runner.calls)
        runner.release("evt-1")
        await first.completed

        clock.advance(30)
        dup = admission.admit(_routed("evt-1"))
        assert dup is first
        assert runner.calls.count("evt-1") == 1

    asyncio.run(scenario())


def test_completed_ticket_expires_after_idempotency_window() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=1)

        first = admission.admit(_routed("evt-1"))
        await _wait_until(lambda: "evt-1" in runner.calls)
        runner.release("evt-1")
        await first.completed

        clock.advance(10)
        second = admission.admit(_routed("evt-1"))
        assert second is not first
        await _wait_until(lambda: runner.calls.count("evt-1") == 2)
        runner.release("evt-1")
        await second.completed

    asyncio.run(scenario())


def test_recovered_admission_capacity_fifo_and_shrunk_limits() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=1,
            max_queued_total=1, workspace_max_concurrent_sessions=1,
            channel_concurrency={"ch": 1},
        )
        workspace = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        session = compute_session_ref(workspace, "s1")
        gate = asyncio.Event()
        observer = lambda: gate.wait()
        first = admission.recover(
            invocation_id="recovered-1", channel="ch", workspace_ref=workspace,
            session_ref=session, config_version="old-config", deadline_at=clock.now + timedelta(minutes=5),
            instance_id="pod-1", run_id="run-1", observer=observer,
        )
        second = admission.recover(
            invocation_id="recovered-2", channel="ch", workspace_ref=workspace,
            session_ref=session, config_version="old-config", deadline_at=clock.now + timedelta(minutes=5),
            instance_id="pod-1", run_id="run-2", observer=observer,
        )
        assert admission.queued_total == 2
        assert admission.has_work(workspace, session)
        assert first.started.done() and isinstance(first.started.exception(), RuntimeError)
        assert second.started.done() and isinstance(second.started.exception(), RuntimeError)
        with pytest.raises(AdmissionQueueFullError):
            admission.admit(_routed("new-work"))
        gate.set()
        await admission.close()

        # Recovering more owners than the current limit is synchronous. They hold
        # global/channel/workspace capacity and their session until terminal observation.
        runner = GatedRunner()
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=1, max_queued_total=8,
            workspace_max_concurrent_sessions=1, channel_concurrency={"ch": 1, "other": 1},
        )
        recovered_workspace = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        recovered_session = compute_session_ref(recovered_workspace, "s1")
        owners = [asyncio.Event(), asyncio.Event()]
        recovered = [
            admission.recover(
                invocation_id=f"owner-{index}", channel="ch", workspace_ref=recovered_workspace,
                session_ref=recovered_session, config_version="v1", deadline_at=clock.now + timedelta(minutes=5),
                instance_id=f"pod-{index}", run_id=f"run-{index}", observer=event.wait,
            )
            for index, event in enumerate(owners)
        ]
        successor1 = admission.admit(_routed("successor-1", workspace_key="ws1", session_key="s1"))
        successor2 = admission.admit(_routed("successor-2", workspace_key="ws1", session_key="s1"))
        other = admission.admit(_routed("other", channel="other", workspace_key="ws2", session_key="s2"))
        await asyncio.sleep(POLL * 4)
        assert not runner.calls
        assert admission.queued_total == 5
        for event in owners:
            event.set()
        await _wait_until(lambda: len(runner.calls) == 1)
        assert runner.calls == ["successor-1"]
        runner.release("successor-1")
        await _wait_until(lambda: len(runner.calls) == 2)
        assert runner.calls == ["successor-1", "successor-2"]
        runner.release("successor-2")
        await _wait_until(lambda: len(runner.calls) == 3)
        runner.release("other")
        await asyncio.gather(successor1.completed, successor2.completed, other.completed)
        await admission.close()

    asyncio.run(scenario())


def test_recovered_admission_leaves_global_headroom_for_unrelated_work() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=2,
            max_queued_total=4, workspace_max_concurrent_sessions=2,
            channel_concurrency={"recovered-channel": 1, "ordinary-channel": 1},
        )
        recovered_workspace = compute_workspace_ref(NAMESPACE, AGENT_UID, "recovered-ws")
        recovered_session = compute_session_ref(recovered_workspace, "recovered-session")
        observer_gate = asyncio.Event()
        recovered = admission.recover(
            invocation_id="active-recovered-owner", channel="recovered-channel",
            workspace_ref=recovered_workspace, session_ref=recovered_session,
            config_version="old-config", deadline_at=clock.now + timedelta(minutes=5),
            instance_id="pod-recovered", run_id="run-recovered", observer=observer_gate.wait,
        )

        ordinary = admission.admit(_routed(
            "ordinary-event", channel="ordinary-channel", workspace_key="ordinary-ws",
            session_key="ordinary-session",
        ))
        await _wait_until(lambda: "ordinary-event" in runner.calls)
        assert runner.calls == ["ordinary-event"]
        assert admission.queued_total == 2
        assert admission.has_work(recovered_workspace, recovered_session)
        assert admission.recovered_run("active-recovered-owner") is recovered

        runner.release("ordinary-event")
        await ordinary.completed
        assert admission.queued_total == 1
        assert admission.has_work(recovered_workspace, recovered_session)
        assert runner.calls == ["ordinary-event"]

        observer_gate.set()
        await _wait_until(lambda: admission.queued_total == 0)
        assert not admission.has_work(recovered_workspace, recovered_session)
        await admission.close()

    asyncio.run(scenario())


def test_recovered_admission_dedup_retention_and_future_cancellation() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=1)
        observer_gate = asyncio.Event()
        calls = 0

        async def observer() -> None:
            nonlocal calls
            calls += 1
            await observer_gate.wait()

        invocation_id = invocation_digest(AGENT_UID, "ch", "evt-recovered")
        workspace = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        session = compute_session_ref(workspace, "s1")
        kwargs = dict(
            invocation_id=invocation_id, channel="ch", workspace_ref=workspace,
            session_ref=session, config_version="old", deadline_at=clock.now + timedelta(minutes=5),
            instance_id="pod", run_id="run", observer=observer,
        )
        first = admission.recover(**kwargs)
        duplicate = admission.recover(**kwargs)
        await asyncio.sleep(0)
        assert duplicate is first
        assert calls == 1
        clock.advance(100)
        assert admission.recover(**kwargs) is first
        first.started.cancel()
        first.completed.cancel()
        assert admission.queued_total == 1
        assert admission.has_work(workspace, session)

        observer_gate.set()
        await _wait_until(lambda: admission.queued_total == 0)
        assert not admission.has_work(workspace, session)
        assert admission.recover(**kwargs) is first
        assert calls == 1
        clock.advance(2)
        fresh = admission.admit(_routed("evt-recovered"))
        assert fresh is not first
        await _wait_until(lambda: "evt-recovered" in runner.calls)
        runner.release("evt-recovered")
        await fresh.completed
        await admission.close()

    asyncio.run(scenario())


def test_recovered_admission_close_and_identity_conflict() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)
        workspace = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        session = compute_session_ref(workspace, "s1")
        cancelled = asyncio.Event()

        async def observer() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        kwargs = dict(
            invocation_id="owner", channel="ch", workspace_ref=workspace,
            session_ref=session, config_version="v1", deadline_at=clock.now + timedelta(minutes=5),
            instance_id="pod", run_id="run", observer=observer,
        )
        owner = admission.recover(**kwargs)
        for key, value in (
            ("channel", "other"), ("workspace_ref", "other-workspace"),
            ("session_ref", "other-session"), ("config_version", "v2"),
            ("deadline_at", clock.now + timedelta(minutes=6)), ("instance_id", "other-pod"),
            ("run_id", "other-run"),
        ):
            with pytest.raises(ValueError, match="conflicting recovered run ownership"):
                admission.recover(**{**kwargs, key: value})
        assert admission.queued_total == 1
        assert admission.recover(**kwargs) is owner
        await asyncio.sleep(0)
        await admission.close()
        assert cancelled.is_set()
        assert admission.queued_total == 0
        with pytest.raises(AdmissionClosedError):
            admission.recover(**kwargs)
        with pytest.raises(AdmissionClosedError):
            admission.admit(_routed("evt-after-close"))

        admission = _admission(runner, clock=clock)
        ticket = admission.admit(_routed("evt-same"))
        with pytest.raises(ValueError, match="conflicting recovered run ownership"):
            admission.recover(
                invocation_id=ticket.invocation_id, channel="ch", workspace_ref=ticket.workspace_ref,
                session_ref=ticket.session_ref, config_version="v1", deadline_at=ticket.deadline_at,
                instance_id="pod", run_id="run", observer=observer,
            )
        assert admission.queued_total == 1
        await admission.close()

    asyncio.run(scenario())


def test_owner_loss_ignores_cancelled_futures() -> None:
    async def scenario() -> None:
        import ach_runtime.harness.admission as admission_module

        lost_type = getattr(admission_module, "WorkspaceOwnerLost", RuntimeError)
        runner_entered = asyncio.Event()
        release_runner = asyncio.Event()
        runner_calls = []

        async def runner(routed_event, ticket):
            runner_calls.append(ticket.invocation_id)
            runner_entered.set()
            await release_runner.wait()
            raise lost_type("Workspace owner lost; run outcome is unknown")

        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=60)
        routed = _routed("owner-loss-cancelled")
        ticket = admission.admit(routed)
        await asyncio.wait_for(runner_entered.wait(), timeout=2)
        assert ticket.started.cancel()
        assert ticket.completed.cancel()
        release_runner.set()
        await _wait_until(lambda: admission.queued_total == 0)
        assert ticket.invocation_id in admission._released
        assert runner_calls == [ticket.invocation_id]
        assert admission.admit(routed) is ticket
        assert admission.queued_total == 0
        await admission.close()

        observer_entered = asyncio.Event()
        release_observer = asyncio.Event()
        recovered_calls = []

        async def observer():
            recovered_calls.append("observed")
            observer_entered.set()
            await release_observer.wait()
            raise lost_type("Workspace owner lost; run outcome is unknown")

        recovered_admission = _admission(GatedRunner(), clock=clock, idempotency_window_seconds=60)
        workspace = compute_workspace_ref(NAMESPACE, AGENT_UID, "recovered-owner-loss")
        session = compute_session_ref(workspace, "main")
        kwargs = dict(
            invocation_id="owner-loss-recovered-cancelled", channel="ch",
            workspace_ref=workspace, session_ref=session, config_version="f" * 64,
            deadline_at=clock.now + timedelta(minutes=5), instance_id="pod-old", run_id="run-old",
            observer=observer,
        )
        recovered = recovered_admission.recover(**kwargs)
        await asyncio.wait_for(observer_entered.wait(), timeout=2)
        assert recovered.completed.cancel()
        assert recovered_admission.queued_total == 1
        release_observer.set()
        await _wait_until(lambda: recovered_admission.queued_total == 0)
        assert recovered.invocation_id in recovered_admission._released
        assert recovered_admission.recovered_run(recovered.invocation_id) is None
        assert recovered_admission.recover(**kwargs) is recovered
        assert recovered_calls == ["observed"]
        assert recovered_admission.queued_total == 0
        await recovered_admission.close()

    asyncio.run(scenario())


def test_dedup_boundary_channel_and_event_id_unambiguous() -> None:
    """Finding 5: `f"{channel}:{event_id}"` let ('a','b:c') collide with ('a:b','c')."""

    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)

        t1 = admission.admit(_routed("b:c", channel="a", session_key="s1"))
        t2 = admission.admit(_routed("c", channel="a:b", session_key="s2"))
        assert t1 is not t2, "distinct (channel, eventId) pairs must never share a ticket"
        assert t1.invocation_id != t2.invocation_id

        await _wait_until(lambda: len(runner.calls) == 2)
        runner.release("b:c")
        runner.release("c")
        await t1.completed
        await t2.completed

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# FIFO per (Workspace, session); independent concurrency; global/channel/workspace bounds
# ---------------------------------------------------------------------------


def test_fifo_same_session() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)

        admission.admit(_routed("evt-1", session_key="s1"))
        admission.admit(_routed("evt-2", session_key="s1"))

        await _wait_until(lambda: "evt-1" in runner.calls)
        assert "evt-2" not in runner.calls, "same-session FIFO: evt-2 must wait for evt-1"

        runner.release("evt-1")
        await _wait_until(lambda: "evt-2" in runner.calls)
        runner.release("evt-2")

    asyncio.run(scenario())


def test_workspace_scoped_fifo_same_session_key_across_workspaces_is_independent() -> None:
    """Finding 1: lanes must be scoped by Workspace, not the raw session_key alone —
    two different Workspaces both using the default session_key "main" must run
    concurrently, not share one FIFO worker."""

    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, workspace_max_concurrent_sessions=5)

        admission.admit(_routed("evt-1", workspace_key="ws-A", session_key="main"))
        admission.admit(_routed("evt-2", workspace_key="ws-B", session_key="main"))

        await _wait_until(lambda: len(runner.calls) == 2)
        runner.release("evt-1")
        runner.release("evt-2")

    asyncio.run(scenario())


def test_independent_sessions_run_concurrently() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, workspace_max_concurrent_sessions=5)

        admission.admit(_routed("evt-1", workspace_key="ws1", session_key="s1"))
        admission.admit(_routed("evt-2", workspace_key="ws1", session_key="s2"))

        await _wait_until(lambda: len(runner.calls) == 2)
        runner.release("evt-1")
        runner.release("evt-2")

    asyncio.run(scenario())


def test_global_concurrency_cap() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=2, workspace_max_concurrent_sessions=10
        )

        for i in range(4):
            admission.admit(_routed(f"evt-{i}", workspace_key="ws1", session_key=f"s{i}"))

        await _wait_until(lambda: len(runner.calls) == 2)
        await asyncio.sleep(POLL * 3)
        assert len(runner.calls) == 2, "global cap must bind at exactly 2, not drift higher"

        for i in range(4):
            runner.release(f"evt-{i}")
        await _wait_until(lambda: len(runner.calls) == 4)

    asyncio.run(scenario())


def test_channel_concurrency_cap() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_concurrent_invocations=10,
            workspace_max_concurrent_sessions=10,
            channel_concurrency={"limited": 1},
        )

        admission.admit(_routed("evt-1", channel="limited", workspace_key="ws1", session_key="s1"))
        admission.admit(_routed("evt-2", channel="limited", workspace_key="ws1", session_key="s2"))

        await _wait_until(lambda: len(runner.calls) == 1)
        await asyncio.sleep(POLL * 3)
        assert len(runner.calls) == 1, "channel cap=1 must block the second channel event"

        runner.release("evt-1")
        await _wait_until(lambda: len(runner.calls) == 2)
        runner.release("evt-2")

    asyncio.run(scenario())


def test_workspace_concurrency_cap() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=10, workspace_max_concurrent_sessions=1
        )

        admission.admit(_routed("evt-1", workspace_key="ws1", session_key="s1"))
        admission.admit(_routed("evt-2", workspace_key="ws1", session_key="s2"))

        await _wait_until(lambda: len(runner.calls) == 1)
        await asyncio.sleep(POLL * 3)
        assert len(runner.calls) == 1, "workspace.maxConcurrentSessions=1 must bind across sessions"

        runner.release("evt-1")
        await _wait_until(lambda: len(runner.calls) == 2)
        runner.release("evt-2")

    asyncio.run(scenario())


def test_narrow_permit_contention_does_not_starve_unrelated_workspace() -> None:
    """Finding 6: a ticket blocked on a narrow (Workspace) permit must not keep holding
    the broad (global) permit — an unrelated Workspace must still make progress."""

    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_concurrent_invocations=2,  # just enough for A1 + B1, NOT for A1+A2+B1
            workspace_max_concurrent_sessions=1,
        )

        admission.admit(_routed("a1", workspace_key="A", session_key="sA1"))
        await _wait_until(lambda: "a1" in runner.calls)

        # a2 is blocked on A's workspace permit (cap=1, held by a1) — it must NOT also
        # hold a global permit while it waits.
        admission.admit(_routed("a2", workspace_key="A", session_key="sA2"))
        admission.admit(_routed("b1", workspace_key="B", session_key="sB1"))

        await _wait_until(lambda: "b1" in runner.calls)
        assert "a2" not in runner.calls, "a2 is correctly still blocked on A's workspace permit"

        runner.release("a1")
        runner.release("b1")
        await _wait_until(lambda: "a2" in runner.calls)
        runner.release("a2")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# deadline_at stamped once by Admission; permit wait is bounded by it; exact Started boundary
# ---------------------------------------------------------------------------


def test_queue_wait_consumes_original_deadline() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner, clock=clock, max_concurrent_invocations=1, max_invocation_seconds=10
        )

        blocker = admission.admit(_routed("evt-blocker", session_key="s1"))
        await _wait_until(lambda: "evt-blocker" in runner.calls)

        waiting = admission.admit(_routed("evt-waiting", session_key="s2"))
        assert waiting.deadline_at == clock.now + timedelta(seconds=10)

        clock.advance(15)  # exceeds waiting's deadline while it is still queued
        runner.release("evt-blocker")
        await blocker.completed

        with pytest.raises(TimeoutError):
            await waiting.started
        with pytest.raises(TimeoutError):
            await waiting.completed
        assert "evt-waiting" not in runner.calls, "deadline already passed — runner must not start"

    asyncio.run(scenario())


def test_permit_wait_bounded_by_deadline_even_behind_a_stuck_holder() -> None:
    """Finding 2: a ticket queued behind a holder that never releases must still fail at
    its OWN deadline — it must not wait indefinitely for that holder to finish."""

    async def scenario() -> None:
        alarms: list[tuple[str, AdmissionTicket]] = []
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_concurrent_invocations=1,
            max_invocation_seconds=5,
            alarm=lambda reason, ticket: alarms.append((reason, ticket)),
        )

        holder = admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: "evt-holder" in runner.calls)  # never released in this test

        waiting = admission.admit(_routed("evt-waiting", session_key="s2"))
        clock.advance(10)  # past evt-waiting's deadline, while the global permit is still held

        with pytest.raises(TimeoutError):
            await waiting.started
        with pytest.raises(TimeoutError):
            await waiting.completed
        assert "evt-waiting" not in runner.calls
        assert alarms, "an explicit alarm must fire on deadline expiry"

        runner.release("evt-holder")
        await holder.completed

    asyncio.run(scenario())


def test_exact_started_boundary() -> None:
    async def scenario() -> None:
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        prep_gate = asyncio.Event()
        finish_gate = asyncio.Event()
        started_was_done_during_prep = None

        async def runner(routed_event: RoutedEvent, ticket: AdmissionTicket) -> None:
            nonlocal started_was_done_during_prep
            await prep_gate.wait()
            started_was_done_during_prep = ticket.started.done()
            ticket.mark_started()
            await finish_gate.wait()

        admission = _admission(runner, clock=clock)
        ticket = admission.admit(_routed("evt-1"))

        await asyncio.sleep(POLL * 2)
        assert not ticket.started.done(), "being dequeued/running must NOT itself resolve Started"

        prep_gate.set()
        await _wait_until(lambda: ticket.started.done())
        assert started_was_done_during_prep is False

        finish_gate.set()
        await ticket.completed

    asyncio.run(scenario())


def test_pre_started_failure_propagates_to_started_future() -> None:
    async def scenario() -> None:
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

        async def failing_runner(routed_event: RoutedEvent, ticket: AdmissionTicket) -> None:
            raise RuntimeError("boom before Started")

        admission = _admission(failing_runner, clock=clock)
        ticket = admission.admit(_routed("evt-1"))

        with pytest.raises(RuntimeError, match="boom before Started"):
            await ticket.started
        with pytest.raises(RuntimeError, match="boom before Started"):
            await ticket.completed

    asyncio.run(scenario())


def test_post_started_failure_does_not_revoke_ack() -> None:
    async def scenario() -> None:
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

        async def failing_after_start(routed_event: RoutedEvent, ticket: AdmissionTicket) -> None:
            ticket.mark_started()
            raise RuntimeError("boom after Started")

        admission = _admission(failing_after_start, clock=clock)
        ticket = admission.admit(_routed("evt-1"))

        await ticket.started
        with pytest.raises(RuntimeError, match="boom after Started"):
            await ticket.completed

    asyncio.run(scenario())


def test_capacity_released_on_failure() -> None:
    async def scenario() -> None:
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

        async def failing_runner(routed_event: RoutedEvent, ticket: AdmissionTicket) -> None:
            ticket.mark_started()
            raise RuntimeError("boom")

        admission = _admission(failing_runner, clock=clock, max_concurrent_invocations=1)
        first = admission.admit(_routed("evt-1", session_key="s1"))
        with pytest.raises(RuntimeError):
            await first.completed

        async def ok_runner(routed_event: RoutedEvent, ticket: AdmissionTicket) -> None:
            ticket.mark_started()

        admission._runner = ok_runner
        second = admission.admit(_routed("evt-2", session_key="s2"))
        await second.started

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# cancellation / dead-lane cleanup (finding 3)
# ---------------------------------------------------------------------------


def test_cancellation_removes_lane_and_finalizes_dedup_for_queued_work() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)

        running = admission.admit(_routed("evt-running", session_key="s1"))
        await _wait_until(lambda: "evt-running" in runner.calls)
        queued = admission.admit(_routed("evt-queued", session_key="s1"))

        # session_ref is the real lane key — fetch it the same way admit() computed it.
        ws_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        sess_ref = compute_session_ref(ws_ref, "s1")
        lane = admission._lanes[sess_ref]

        lane.cancel()  # simulate an external cancellation of this lane's task
        await lane.wait_closed()

        assert sess_ref not in admission._lanes, "a cancelled lane must deregister itself"
        with pytest.raises(asyncio.CancelledError):
            await queued.started
        assert queued.completed.cancelled() or isinstance(queued.completed.exception(), asyncio.CancelledError)

        # A fresh admission to the same (workspace, session) must get a WORKING lane.
        fresh = admission.admit(_routed("evt-fresh", session_key="s1"))
        await _wait_until(lambda: "evt-fresh" in runner.calls)
        runner.release("evt-fresh")
        await fresh.completed

        with pytest.raises(asyncio.CancelledError):
            await running.completed  # the originally-running ticket was also cancelled

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# close() is terminal and accounts for capacity immediately (finding 4)
# ---------------------------------------------------------------------------


def test_close_immediately_after_admit_accounts_capacity() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)

        ticket = admission.admit(_routed("evt-1"))
        await admission.close()

        assert admission.queued_total == 0, "close() must account for a ticket whose lane never ran"
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await ticket.started
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await ticket.completed

    asyncio.run(scenario())


def test_admit_after_close_rejected() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)

        await admission.close()
        with pytest.raises(AdmissionClosedError):
            admission.admit(_routed("evt-1"))

    asyncio.run(scenario())


def test_close_resolves_queued_work_without_hanging() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, max_concurrent_invocations=1)

        running = admission.admit(_routed("evt-running", session_key="s1"))
        await _wait_until(lambda: "evt-running" in runner.calls)
        queued = admission.admit(_routed("evt-queued", session_key="s2"))

        await admission.close()

        assert running.completed.done()
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await running.completed
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await queued.started
        with pytest.raises((RuntimeError, asyncio.CancelledError)):
            await queued.completed

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# TTL/idle-map cleanup (finding 8)
# ---------------------------------------------------------------------------


def test_completed_records_are_swept_by_later_admissions() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=1)

        first = admission.admit(_routed("evt-old", session_key="s-old"))
        await _wait_until(lambda: "evt-old" in runner.calls)
        runner.release("evt-old")
        await first.completed

        assert len(admission._tickets) == 1

        clock.advance(10)  # evt-old's idempotency window has elapsed

        # Unique, unrelated traffic must sweep the now-expired record away — growth must
        # not be unbounded even though every delivery here is distinct.
        for i in range(5):
            t = admission.admit(_routed(f"evt-new-{i}", session_key=f"s-new-{i}"))
            await _wait_until(lambda t=t: t.routed_event.event_id in runner.calls)
            runner.release(t.routed_event.event_id)
            await t.completed

        assert len(admission._tickets) == 5, "evt-old must have been swept, not retained forever"

    asyncio.run(scenario())


def test_idle_workspace_semaphore_is_discarded() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=1)

        first = admission.admit(_routed("evt-1", workspace_key="ws-ephemeral", session_key="s1"))
        await _wait_until(lambda: "evt-1" in runner.calls)
        runner.release("evt-1")
        await first.completed
        assert len(admission._workspace_sems) == 1

        clock.advance(10)
        # A sweep is triggered by the NEXT admit() call (on an unrelated Workspace).
        second = admission.admit(_routed("evt-2", workspace_key="ws-other", session_key="s2"))
        await _wait_until(lambda: "evt-2" in runner.calls)  # lets ws-other's own sem get created

        ws_ephemeral_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws-ephemeral")
        ws_other_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws-other")
        assert ws_ephemeral_ref not in admission._workspace_sems, "the now-idle semaphore must be GC'd"
        assert ws_other_ref in admission._workspace_sems
        assert len(admission._workspace_sems) == 1

        runner.release("evt-2")
        await second.completed

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# immutable admission metadata / scoped refs (finding 9)
# ---------------------------------------------------------------------------


def test_ticket_exposes_immutable_namespace_agent_and_scoped_refs() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)

        routed = _routed("evt-1", channel="ch", workspace_key="ws1", session_key="s1")
        ticket = admission.admit(routed)

        expected_ws_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        expected_sess_ref = compute_session_ref(expected_ws_ref, "s1")

        assert ticket.namespace == NAMESPACE
        assert ticket.agent_uid == AGENT_UID
        assert ticket.workspace_ref == expected_ws_ref
        assert ticket.session_ref == expected_sess_ref
        assert len(ticket.workspace_ref) == 64
        assert len(ticket.session_ref) == 64

        with pytest.raises(dataclasses.FrozenInstanceError):
            ticket.deadline_at = clock.now  # type: ignore[misc]

        # Duplicate admission must retain the exact same scoped identity.
        dup = admission.admit(routed)
        assert dup.namespace == ticket.namespace
        assert dup.agent_uid == ticket.agent_uid
        assert dup.workspace_ref == ticket.workspace_ref
        assert dup.session_ref == ticket.session_ref
        assert dup.deadline_at == ticket.deadline_at

        await _wait_until(lambda: "evt-1" in runner.calls)
        runner.release("evt-1")
        await ticket.completed

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# round 2 — task-3-admission-fix-1-review.md: I2, N1, N2
# (I7 lives in tests/channels/test_routing.py, alongside round-1's finding-7 tests)
# ---------------------------------------------------------------------------


def test_queued_same_session_ticket_expires_behind_a_stuck_runner() -> None:
    """I2: a ticket queued behind an earlier same-session ticket that never finishes must
    still expire at its OWN deadline — the lane awaiting the stuck ticket must not be
    the only thing that can ever resolve it."""

    async def scenario() -> None:
        alarms: list[tuple[str, AdmissionTicket]] = []
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))

        async def stuck_runner(routed_event: RoutedEvent, ticket: AdmissionTicket) -> None:
            ticket.mark_started()
            await asyncio.Event().wait()  # a fresh, never-set Event — never returns

        admission = _admission(
            stuck_runner,
            clock=clock,
            max_invocation_seconds=5,
            alarm=lambda reason, ticket: alarms.append((reason, ticket)),
        )

        holder = admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: holder.started.done())

        waiting = admission.admit(_routed("evt-waiting", session_key="s1"))  # SAME session
        clock.advance(10)  # past evt-waiting's deadline while evt-holder is still stuck

        # Bounded wait first (so a not-yet-fixed implementation fails the test cleanly
        # instead of hanging this run forever), THEN assert the exception it resolved with.
        await _wait_until(lambda: waiting.started.done(), timeout=1.0)
        assert isinstance(waiting.started.exception(), TimeoutError)
        await _wait_until(lambda: waiting.completed.done(), timeout=1.0)
        assert isinstance(waiting.completed.exception(), TimeoutError)
        assert alarms, "an explicit alarm must fire for a queued ticket that expires"

        await admission.close()  # holder is genuinely stuck — only close() can clean it up

    asyncio.run(scenario())


def test_idle_lane_consumer_terminates_not_just_deregisters() -> None:
    """N1: a drained-idle lane must terminate its consumer task, not leave it looping on
    an abandoned queue forever."""

    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)

        ticket = admission.admit(_routed("evt-1", workspace_key="ws1", session_key="s1"))
        await _wait_until(lambda: "evt-1" in runner.calls)

        ws_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        sess_ref = compute_session_ref(ws_ref, "s1")
        lane = admission._lanes[sess_ref]

        runner.release("evt-1")
        await ticket.completed
        await _wait_until(lambda: lane._task.done())

        assert sess_ref not in admission._lanes
        assert lane._task.done(), "an idle consumer must terminate, not loop on an abandoned queue"

        await admission.close()  # must return promptly — no zombie consumer left for it to miss

    asyncio.run(scenario())


def test_external_cancel_of_completed_future_does_not_break_dedup() -> None:
    """N2: a caller cancelling the exposed `ticket.completed` Future mid-invocation must
    not make Admission's own dedup/GC forget that the invocation is still active."""

    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=60)

        first = admission.admit(_routed("evt-1"))
        await _wait_until(lambda: "evt-1" in runner.calls)

        first.completed.cancel()  # external caller, independent of the real invocation

        dup = admission.admit(_routed("evt-1"))
        assert dup is first, "an externally-cancelled Future must not forget an in-flight record"

        runner.release("evt-1")
        await _wait_until(lambda: admission.queued_total == 0)
        assert admission._dedup_key(AGENT_UID, dup.routed_event) in admission._released

    asyncio.run(scenario())


def test_external_cancel_of_completed_future_does_not_expire_capacity_early() -> None:
    """N2 companion: the externally-cancelled Future must not be mistaken for a
    COMPLETED-and-expired record by the admission-path sweep before the real invocation
    has actually finished and been accounted."""

    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, idempotency_window_seconds=1)

        first = admission.admit(_routed("evt-1", session_key="s1"))
        await _wait_until(lambda: "evt-1" in runner.calls)
        first.completed.cancel()

        clock.advance(10)  # would exceed the idempotency window IF this were truly completed
        admission.admit(_routed("evt-2", session_key="s2"))  # triggers the admission-path sweep

        dup = admission.admit(_routed("evt-1", session_key="s1"))
        assert dup is first, "still in-flight — a GC sweep must not have discarded it"

        runner.release("evt-1")
        runner.release("evt-2")
        await _wait_until(lambda: admission.queued_total == 0)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# round 3 — task-3-admission-fix-2-review.md: N3, N4
# ---------------------------------------------------------------------------


def test_expired_queued_ticket_survives_ttl_until_its_fifo_entry_is_retired() -> None:
    """N3: a ticket the deadline monitor expires while still queued behind a stuck
    same-session holder must NOT be forgotten by the idempotency-window sweep while its
    physical FIFO entry is still sitting undequeued — doing so let a second generation
    (fresh ticket + fresh monitor) spawn for the same digest, and when the stale first
    generation was eventually dequeued it stole/cancelled the second generation's monitor
    and re-finalized the shared key, leaving the second generation's futures pending
    forever. A same-key readmission must keep returning the SAME (already-failed) ticket
    for as long as that FIFO entry is outstanding, however long the holder stays stuck."""

    async def scenario() -> None:
        alarms: list[tuple[str, AdmissionTicket]] = []
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_invocation_seconds=5,
            idempotency_window_seconds=1,
            alarm=lambda reason, ticket: alarms.append((reason, ticket)),
        )

        holder = admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: "evt-holder" in runner.calls)

        repeat1 = admission.admit(_routed("evt-repeat", session_key="s1"))  # queued behind holder
        clock.advance(10)  # past repeat1's 5s deadline

        await _wait_until(lambda: repeat1.started.done(), timeout=1.0)
        assert isinstance(repeat1.started.exception(), TimeoutError)
        assert len(alarms) == 1

        clock.advance(10)  # also now well past the 1s idempotency window — but still queued

        repeat2 = admission.admit(_routed("evt-repeat", session_key="s1"))
        assert repeat2 is repeat1, (
            "must retain the original ticket until its FIFO entry is actually retired, "
            "not spawn a second generation while the first is still outstanding"
        )
        assert len(alarms) == 1, "no second alarm from the readmission itself"

        runner.release("evt-holder")
        await holder.completed
        # The lane now dequeues repeat1's stale entry, finds it monitor-expired, retires it.
        await _wait_until(lambda: admission.queued_total == 0)

        clock.advance(10)  # past the idempotency window measured from ACTUAL retirement

        repeat3 = admission.admit(_routed("evt-repeat", session_key="s1"))
        assert repeat3 is not repeat1, (
            "once genuinely retired and the window has elapsed since retirement, a fresh "
            "ticket is expected"
        )

        await _wait_until(lambda: "evt-repeat" in runner.calls)
        runner.release("evt-repeat")
        await repeat3.completed

    asyncio.run(scenario())


def test_external_cancel_of_queued_completion_future_does_not_disable_monitor() -> None:
    """N4: a caller cancelling the exposed `ticket.completed` Future BEFORE the ticket is
    ever dequeued must not silently disable its deadline monitor — Started must still
    fail, exactly one alarm must fire, capacity must still release exactly once, and a
    duplicate admission in the meantime must still return the same ticket."""

    async def scenario() -> None:
        alarms: list[tuple[str, AdmissionTicket]] = []
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_invocation_seconds=5,
            alarm=lambda reason, ticket: alarms.append((reason, ticket)),
        )

        holder = admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: "evt-holder" in runner.calls)

        waiting = admission.admit(_routed("evt-waiting", session_key="s1"))
        waiting.completed.cancel()  # external caller, before this ticket is ever dequeued

        dup = admission.admit(_routed("evt-waiting", session_key="s1"))
        assert dup is waiting, "duplicate identity must survive the external cancellation"

        clock.advance(10)  # past evt-waiting's deadline while it is still queued

        await _wait_until(lambda: waiting.started.done(), timeout=1.0)
        assert isinstance(waiting.started.exception(), TimeoutError)
        assert len(alarms) == 1, "exactly one expiry alarm for the queued ticket"

        runner.release("evt-holder")
        await _wait_until(lambda: admission.queued_total == 0)  # exactly one capacity release

        await admission.close()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# round 4 — task-3-admission-fix-3-review.md: N5
# ---------------------------------------------------------------------------


def test_cancellation_before_queued_expiry_retires_owned_monitor_and_allows_fresh_readmission() -> None:
    """N5 (before-expiry case): cancelling a lane while a queued ticket has NOT yet
    expired must retire that ticket's own deadline-monitor task immediately — a monitor
    left running after its ticket is already cancelled is both a task leak ("no owned
    watchdog survives retirement") and a source of a later false alarm. A fresh same-key
    admission after the idempotency window must still actually reach the runner."""

    async def scenario() -> None:
        alarms: list[tuple[str, AdmissionTicket]] = []
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_invocation_seconds=5,
            idempotency_window_seconds=1,
            alarm=lambda reason, ticket: alarms.append((reason, ticket)),
        )

        holder = admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: "evt-holder" in runner.calls)  # stuck — never released

        repeat = admission.admit(_routed("evt-repeat", session_key="s1"))  # queued, not yet expired
        dedup_key = admission._dedup_key(AGENT_UID, repeat.routed_event)
        monitor_task = admission._monitors[dedup_key]

        ws_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        sess_ref = compute_session_ref(ws_ref, "s1")
        lane = admission._lanes[sess_ref]

        lane.cancel()  # cancellation retirement BEFORE repeat's own deadline passes
        await lane.wait_closed()

        # both original futures settle
        with pytest.raises(asyncio.CancelledError):
            await repeat.started
        with pytest.raises(asyncio.CancelledError):
            await repeat.completed

        # no owned watchdog survives retirement — its task is cancelled (interrupting the
        # poll sleep) and the monitor's own `except CancelledError: pass` lets it return
        # normally rather than propagate, so `.done()` is the right predicate, not
        # `.cancelled()`.
        assert dedup_key not in admission._monitors
        await _wait_until(lambda: monitor_task.done())

        # no false later alarm, even once repeat's original deadline would have passed —
        # also carries clock past the idempotency window measured from retirement
        clock.advance(10)
        await asyncio.sleep(POLL * 3)
        assert not alarms, "a retired/cancelled ticket must never still fire its monitor's alarm"

        # fresh post-TTL same-key admission actually reaches the runner
        fresh = admission.admit(_routed("evt-repeat", session_key="s1"))
        assert fresh is not repeat

        await _wait_until(lambda: "evt-repeat" in runner.calls, timeout=1.0)
        runner.release("evt-repeat")
        await fresh.completed

        with pytest.raises(asyncio.CancelledError):
            await holder.completed

    asyncio.run(scenario())


def test_cancellation_after_queued_expiry_does_not_corrupt_fresh_readmission() -> None:
    """N5 (after-expiry case): if cancellation retires a ticket the deadline monitor had
    ALREADY expired while queued, the stale `_monitor_expired` marker for that digest
    must be cleared at retirement too — otherwise a fresh same-key ticket admitted later
    reuses the digest, its own `_run_ticket` sees the stale marker and returns as though
    the FRESH ticket had already expired: capacity is released but its Started/completed
    are left pending forever and the valid runner is never called (the exact sequence
    from the N5 review probe)."""

    async def scenario() -> None:
        alarms: list[tuple[str, AdmissionTicket]] = []
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_invocation_seconds=5,
            idempotency_window_seconds=1,
            alarm=lambda reason, ticket: alarms.append((reason, ticket)),
        )

        holder = admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: "evt-holder" in runner.calls)  # stuck — never released

        repeat = admission.admit(_routed("evt-repeat", session_key="s1"))  # queued
        dedup_key = admission._dedup_key(AGENT_UID, repeat.routed_event)

        clock.advance(10)  # past repeat's 5s deadline while still queued
        await _wait_until(lambda: repeat.started.done(), timeout=1.0)
        assert isinstance(repeat.started.exception(), TimeoutError)
        assert len(alarms) == 1, "the monitor itself fires exactly one alarm"
        assert dedup_key in admission._monitor_expired

        ws_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        sess_ref = compute_session_ref(ws_ref, "s1")
        lane = admission._lanes[sess_ref]

        lane.cancel()  # cancellation retirement of the still-undequeued, already-expired entry
        await lane.wait_closed()

        # `_fail` never overwrites an already-resolved future, so repeat keeps the
        # monitor's original TimeoutError, not a CancelledError from the drain.
        with pytest.raises(TimeoutError):
            await repeat.completed

        # the key regression: retirement must clear the stale marker, not just the futures
        assert dedup_key not in admission._monitor_expired
        assert dedup_key not in admission._monitors
        assert len(alarms) == 1, "cancellation retirement itself must not fire a second alarm"

        clock.advance(10)  # past the idempotency window measured from retirement
        fresh = admission.admit(_routed("evt-repeat", session_key="s1"))
        assert fresh is not repeat

        # Pre-fix, this hangs forever because _run_ticket sees the stale marker and
        # returns without ever resolving the fresh ticket's futures — bounded wait so a
        # not-yet-fixed implementation fails cleanly instead of hanging this run.
        await _wait_until(lambda: "evt-repeat" in runner.calls, timeout=1.0)
        runner.release("evt-repeat")
        await fresh.completed

        with pytest.raises(asyncio.CancelledError):
            await holder.completed

    asyncio.run(scenario())


def test_close_clears_monitor_expired_marker_for_still_queued_entry() -> None:
    """N5, close() path: close() retires its queued tickets through the same lane
    cancellation-drain path exercised above, so it must apply the same ownership/cleanup
    rule — a queued ticket already monitor-expired before close() must not leave its
    `_monitor_expired` marker (or its monitor task) behind once close() is done."""

    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock, max_invocation_seconds=5)

        admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: "evt-holder" in runner.calls)  # stuck — never released

        repeat = admission.admit(_routed("evt-repeat", session_key="s1"))  # queued
        dedup_key = admission._dedup_key(AGENT_UID, repeat.routed_event)

        clock.advance(10)  # past repeat's deadline while still queued
        await _wait_until(lambda: repeat.started.done(), timeout=1.0)
        assert dedup_key in admission._monitor_expired

        await admission.close()

        assert dedup_key not in admission._monitor_expired
        assert admission._monitors == {}

    asyncio.run(scenario())


def test_cancelled_monitor_cleanup_preserves_new_same_key_monitor() -> None:
    """N5 extension: the deadline monitor's own deferred cleanup (`_monitor_deadline`'s
    `finally`) popped `_monitors[dedup_key]` unconditionally. If a same-key ticket is
    cancelled and immediately readmitted (idempotency window elapsed) WHILE the old
    monitor's own cancellation is still in flight, normal asyncio Future-callback
    scheduling runs the readmission before the old monitor's deferred cleanup: `_fail`
    (on the drain path) schedules the done callback on `started` via `call_soon` first,
    then `_retire` -> `_cancel_monitor` -> `monitor.cancel()` schedules the old monitor's
    own cancelled resumption second. A readmission performed from that done callback
    therefore always lands before the old monitor's `finally` runs. Unconditionally
    popping `_monitors[dedup_key]` there then deletes the BRAND NEW generation's live
    monitor task from the registry — not cancelling it (a leaked, uncancelled task) and
    leaving `_run_ticket` for the fresh ticket unable to find/retire it."""

    async def scenario() -> None:
        alarms: list[tuple[str, AdmissionTicket]] = []
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(
            runner,
            clock=clock,
            max_invocation_seconds=5,
            idempotency_window_seconds=1,
            alarm=lambda reason, ticket: alarms.append((reason, ticket)),
        )
        loop = asyncio.get_running_loop()

        async def barrier() -> None:
            evt = asyncio.Event()
            loop.call_soon(evt.set)
            await evt.wait()

        holder = admission.admit(_routed("evt-holder", session_key="s1"))
        await _wait_until(lambda: "evt-holder" in runner.calls)

        old_repeat = admission.admit(_routed("evt-repeat", session_key="s1"))  # queued behind holder
        dedup_key = admission._dedup_key(AGENT_UID, old_repeat.routed_event)
        old_monitor = admission._monitors[dedup_key]
        await barrier()  # let the old monitor reach its poll-sleep before cancellation

        ws_ref = compute_workspace_ref(NAMESPACE, AGENT_UID, "ws1")
        sess_ref = compute_session_ref(ws_ref, "s1")
        old_lane = admission._lanes[sess_ref]

        callback_done = asyncio.Event()
        capacity_before_readmission: int | None = None
        replacement_holder: AdmissionTicket | None = None
        fresh: AdmissionTicket | None = None
        fresh_monitor: asyncio.Task[None] | None = None
        readmission_time: datetime | None = None
        new_lane: _Lane | None = None

        def on_old_started_failed(_: asyncio.Future[None]) -> None:
            # Runs via the `_fail`-scheduled done callback, before the old monitor's own
            # deferred `finally` cleanup (see docstring) — assertions live in the scenario.
            nonlocal capacity_before_readmission, replacement_holder, fresh
            nonlocal fresh_monitor, readmission_time, new_lane
            capacity_before_readmission = admission.queued_total
            clock.advance(2)  # > idempotency_window_seconds: old_repeat's slot is forgotten
            replacement_holder = admission.admit(_routed("evt-replacement-holder", session_key="s1"))
            fresh = admission.admit(_routed("evt-repeat", session_key="s1"))
            fresh_monitor = admission._monitors.get(dedup_key)
            readmission_time = clock.now
            new_lane = admission._lanes[sess_ref]
            callback_done.set()

        old_repeat.started.add_done_callback(on_old_started_failed)

        try:
            old_lane.cancel()
            await old_lane.wait_closed()
            await asyncio.wait_for(callback_done.wait(), timeout=2.0)
            await asyncio.wait_for(old_monitor, timeout=2.0)

            with pytest.raises(asyncio.CancelledError):
                await old_repeat.started
            with pytest.raises(asyncio.CancelledError):
                await old_repeat.completed
            with pytest.raises(asyncio.CancelledError):
                await holder.completed

            assert replacement_holder is not None and fresh is not None
            assert fresh_monitor is not None and new_lane is not None
            assert readmission_time is not None

            assert capacity_before_readmission == 0
            assert fresh is not old_repeat
            assert fresh.invocation_id == old_repeat.invocation_id
            assert fresh.deadline_at == readmission_time + timedelta(seconds=5)
            assert old_monitor.done()
            assert admission._monitors.get(dedup_key) is fresh_monitor
            assert not fresh_monitor.done()
            assert admission.queued_total == 2
            assert admission.admit(_routed("evt-repeat", session_key="s1")) is fresh
            assert fresh.deadline_at == readmission_time + timedelta(seconds=5)

            await _wait_until(lambda: "evt-replacement-holder" in runner.calls, timeout=2.0)
            runner.release("evt-replacement-holder")
            await asyncio.wait_for(replacement_holder.completed, timeout=2.0)
            await asyncio.wait_for(fresh.started, timeout=2.0)
            await asyncio.wait_for(fresh_monitor, timeout=2.0)

            assert admission.queued_total == 1
            assert dedup_key not in admission._monitors
            assert fresh_monitor.done()

            await _wait_until(lambda: "evt-repeat" in runner.calls, timeout=2.0)
            runner.release("evt-repeat")
            await asyncio.wait_for(fresh.completed, timeout=2.0)
            await new_lane.wait_closed()

            clock.advance(10)
            await barrier()

            assert runner.calls == ["evt-holder", "evt-replacement-holder", "evt-repeat"]
            assert admission.queued_total == 0
            assert dedup_key not in admission._monitor_expired
            assert alarms == []

            await admission.close()

            assert runner.calls == ["evt-holder", "evt-replacement-holder", "evt-repeat"]
            assert admission.queued_total == 0
            assert dedup_key not in admission._monitor_expired
            assert alarms == []
        finally:
            await admission.close()
            for task in (old_monitor, fresh_monitor):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(t for t in (old_monitor, fresh_monitor) if t is not None),
                return_exceptions=True,
            )
            futures: list[asyncio.Future[None]] = []
            for ticket in (holder, old_repeat, replacement_holder, fresh):
                if ticket is not None:
                    futures.append(ticket.started)
                    futures.append(ticket.completed)
            await asyncio.gather(*futures, return_exceptions=True)

    asyncio.run(scenario())


def test_has_work_uses_owned_unreleased_tickets() -> None:
    async def scenario() -> None:
        runner = GatedRunner()
        clock = FakeClock(datetime(2026, 10, 1, tzinfo=UTC))
        admission = _admission(runner, clock=clock)
        ticket = admission.admit(_routed("evt-work", workspace_key="owned", session_key="session"))
        assert admission.has_work(ticket.workspace_ref)
        assert admission.has_work(ticket.workspace_ref, ticket.session_ref)
        assert not admission.has_work(ticket.workspace_ref, "other-session")
        runner.release("evt-work")
        await ticket.completed
        assert not admission.has_work(ticket.workspace_ref)
        await admission.close()
    asyncio.run(scenario())

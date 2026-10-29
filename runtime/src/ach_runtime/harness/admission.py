# SPDX-License-Identifier: MIT
"""Admission — dedup -> capacity -> FIFO-per-(Workspace,session) (contract §3).

Scoped to a single ACHAgent (`namespace`/`agent_uid`). Owns no host/engine
dependency: the runner is injected (contract: "Runner injected; no host/engine
dependency here"). Structurally descended from old-ach-agent
`router/{dedup,lane,slots,router}.py` (dedup before backpressure is ORDER IS NORMATIVE in
both); see `task-3-runtime-report.md` for the full reuse map and the round-1/2/3 fix logs
for the deltas made in response to the independent admission reviews
(`task-3-admission-review.md`, `task-3-admission-fix-1-review.md`,
`task-3-admission-fix-2-review.md`).

Round-1 fixes (findings 1-9 of the first review): Workspace-scoped lane keys, bounded
all-or-nothing permit acquisition with deadline recheck, terminal `close()`, unambiguous
digest-based dedup key, admission-path GC sweep, frozen `AdmissionTicket` with scoped refs.

Round-2 fixes (I2/I7/N1/N2 of the fix-1 review):
  I2. A ticket queued behind a same-session ticket that never finishes previously had no
     expiry mechanism at all (the lane awaits one ticket fully before even looking at the
     next). Each admitted ticket now gets its own short-lived `_deadline_monitor` task
     that polls the injected clock independently of lane progress and fails/alarms the
     ticket if its deadline passes while still queued; `_run_ticket` cancels that monitor
     the moment it actually starts handling the ticket (point of no return — its own
     checks take over from there) and short-circuits if the monitor already resolved it.
  N1. A lane that drained to empty deregistered itself but kept looping on `queue.get()`
     forever (a task leak — every completed lane left a zombie consumer `close()` could no
     longer reach, since it only cancels what's still in `_lanes`). `_consume` now
     `return`s immediately after a normal idle deregistration instead of looping back.
  N2. GC/dedup decisions used the PUBLIC, externally-mutable `ticket.completed` Future's
     `.done()` state as "is this finished" — an external caller cancelling that exposed
     Future could make Admission forget an still-in-flight record and admit a fresh
     duplicate mid-invocation. Dedup/GC now key off `dedup_key in self._released`
     (Admission-owned, set only by `_release_capacity_once`), never the Future's own state.

Round-3 fixes (N3/N4 of the fix-2 review):
  N3. A ticket the monitor expired while still queued stayed in `_tickets` with its
     physical FIFO entry un-dequeued, yet round-2's `_release_capacity_once` ran
     immediately from the monitor — so once the idempotency window elapsed, `_forget`
     evicted the ledger entry while that stale physical entry was still sitting in the
     lane's queue, letting a SECOND generation (a fresh ticket + fresh monitor) be
     created for the same digest. When the stuck holder ahead of it finally released, the
     lane dequeued the stale FIRST generation, whose `_run_ticket` unconditionally popped
     and cancelled the SECOND generation's monitor and re-finalized the shared dedup key,
     leaving the second generation's futures pending forever with `queued_total` already
     back at zero. Fixed by moving capacity release (`_released`/`_completed_at`/
     `queued_total`) out of the monitor entirely — it now ONLY fails the futures and
     records `dedup_key` in `_monitor_expired`. `_release_capacity_once` fires exactly
     once, from `_run_ticket`, exactly when that ticket's physical FIFO entry is actually
     dequeued. `_existing_ticket` therefore never creates a second generation for a key
     whose first generation hasn't been dequeued yet, however long that takes or however
     many times the same delivery is retried in the meantime — bounding outstanding
     tombstones to exactly one per key (idempotencyWindow is a floor on retention, not a
     ceiling, per contract §3) instead of leaving a stuck lane accumulate them.
  N4. The deadline monitor's own loop condition still included `not ticket.completed.done()`
     — a caller cancelling that exposed Future before the ticket was ever dequeued made
     the monitor exit silently, with the ticket's `started` left pending past its deadline
     forever. The loop now has no Future-state dependency at all: it runs until it
     expires the ticket itself or its task is cancelled (by `_run_ticket` taking over, or
     by `close()`).

Round-4 fix (N5 of the fix-3 review):
  N5. Cancellation retirement (a lane's drain-on-cancel branch) only called
     `_release_capacity_once` — it never cancelled the retired ticket's own
     `_monitor_deadline` task nor cleared any `_monitor_expired` marker it had already
     set. A monitor that hadn't fired yet kept running as a leaked task after its ticket
     was already cancelled (able to emit a false later alarm); a monitor that HAD already
     fired left its `_monitor_expired[dedup_key]` marker behind permanently. Either way,
     once the idempotency window elapsed and a fresh same-digest ticket was admitted,
     `_run_ticket` for that FRESH ticket saw the stale marker and returned immediately —
     capacity released, but the fresh Started/completed left pending forever and the
     valid runner never called. Fixed with one retirement rule, `_retire`, used by
     ordinary dequeue (`_run_ticket`), cancellation drain (`_Lane._consume`), `close()`
     and GC (`_forget`) alike: it cancels+forgets the ticket's own monitor task and
     clears any `_monitor_expired` marker, gated by `_cancel_monitor`'s ownership check
     (`self._tickets[dedup_key] is ticket`) so it can never touch a newer generation's
     monitor/marker after a key has been reused.
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.harness.refs import invocation_digest
from ach_runtime.harness.refs import session_ref as compute_session_ref
from ach_runtime.harness.refs import workspace_ref as compute_workspace_ref

Clock = Callable[[], datetime]


@dataclass(frozen=True)
class AdmissionTicket:
    invocation_id: str
    namespace: str
    agent_uid: str
    workspace_ref: str
    session_ref: str
    routed_event: RoutedEvent
    deadline_at: datetime
    started: asyncio.Future[None]
    completed: asyncio.Future[Any]

    def mark_started(self) -> None:
        """The runner's one required signal — nothing else resolves `started` (contract §3)."""
        if not self.started.done():
            self.started.set_result(None)


@dataclass(frozen=True)
class ScriptAdmissionTicket:
    invocation_id: str
    namespace: str
    agent_uid: str
    event: Event
    deadline_at: datetime
    started: asyncio.Future[None]
    completed: asyncio.Future[Any]

    def mark_started(self) -> None:
        if not self.started.done():
            self.started.set_result(None)


Runner = Callable[[RoutedEvent | Event, AdmissionTicket | ScriptAdmissionTicket], Awaitable[Any]]


@dataclass(frozen=True)
class RecoveredRun:
    """Admission's handle for a run observed after a Harness restart.

    It intentionally contains only primitive execution identity and observation
    futures: a recovered run has no reconstructable RoutedEvent or Started receipt.
    """

    invocation_id: str
    channel: str
    workspace_ref: str
    session_ref: str
    config_version: str
    deadline_at: datetime
    instance_id: str
    run_id: str | None
    started: asyncio.Future[None]
    completed: asyncio.Future[Any]


Alarm = Callable[[str, AdmissionTicket | ScriptAdmissionTicket | RecoveredRun | None], None]


class AdmissionQueueFullError(RuntimeError):
    """`limits.maxQueuedTotal` reached for a genuinely new event — explicit, never silent."""


class AdmissionClosedError(RuntimeError):
    """`admit()` called after `close()` — close() is terminal, not just a drain."""


class WorkspaceOwnerLost(RuntimeError):
    """The pinned Workspace pod was replaced before this run was observed terminal."""


class Admission:
    def __init__(
        self,
        *,
        namespace: str,
        agent_uid: str,
        max_concurrent_invocations: int,
        max_queued_total: int,
        idempotency_window_seconds: int,
        max_invocation_seconds: int,
        workspace_max_concurrent_sessions: int,
        channel_concurrency: dict[str, int] | None,
        max_concurrent_scripts: int | None = None,
        clock: Clock,
        runner: Runner,
        alarm: Alarm | None = None,
        permit_poll_seconds: float = 0.01,
    ) -> None:
        self._namespace = namespace
        self._agent_uid = agent_uid
        self._max_concurrent_invocations = max_concurrent_invocations
        self._max_queued_total = max_queued_total
        self._idempotency_window_seconds = idempotency_window_seconds
        self._max_invocation_seconds = max_invocation_seconds
        self._workspace_max_concurrent_sessions = workspace_max_concurrent_sessions
        self._channel_concurrency = dict(channel_concurrency or {})
        self._clock = clock
        self._runner = runner
        self._alarm = alarm
        self._permit_poll_seconds = permit_poll_seconds

        self._global_sem = asyncio.Semaphore(max_concurrent_invocations)
        self._script_sem = (
            self._global_sem
            if max_concurrent_scripts is None
            else asyncio.Semaphore(max_concurrent_scripts)
        )
        self._channel_sems: dict[str, tuple[asyncio.Semaphore, int]] = {}
        self._workspace_sems: dict[str, tuple[asyncio.Semaphore, int]] = {}

        self._queued_total = 0
        self._tickets: dict[str, AdmissionTicket | ScriptAdmissionTicket] = {}
        self._recovered: dict[str, RecoveredRun] = {}
        self._recovery_tasks: dict[str, asyncio.Task[None]] = {}
        self._completed_at: dict[str, datetime] = {}
        self._released: set[str] = set()  # dedup keys whose FIFO entry has been dequeued
        self._monitor_expired: set[str] = set()  # dedup keys the deadline monitor itself failed
        self._monitors: dict[str, asyncio.Task[None]] = {}
        self._lanes: dict[str, _Lane] = {}
        self._closed = False

    @property
    def queued_total(self) -> int:
        return self._queued_total

    def has_work(self, workspace_ref: str, session_ref: str | None = None) -> bool:
        """Return whether an owned, unreleased ticket targets this workspace/session."""
        return any(
            key not in self._released
            and isinstance(ticket, AdmissionTicket)
            and ticket.workspace_ref == workspace_ref
            and (session_ref is None or ticket.session_ref == session_ref)
            for key, ticket in self._tickets.items()
        ) or any(
            key not in self._released
            and run.workspace_ref == workspace_ref
            and (session_ref is None or run.session_ref == session_ref)
            for key, run in self._recovered.items()
        )

    def recovered_run(self, invocation_id: str) -> RecoveredRun | None:
        """Return only a recovered run whose capacity is still internally owned."""
        if invocation_id in self._released:
            return None
        return self._recovered.get(invocation_id)

    # -- permit pools -------------------------------------------------------

    def _channel_sem(self, channel: str) -> asyncio.Semaphore:
        entry = self._channel_sems.get(channel)
        if entry is None:
            cap = self._channel_concurrency.get(channel, self._max_concurrent_invocations)
            entry = (asyncio.Semaphore(cap), cap)
            self._channel_sems[channel] = entry
        return entry[0]

    def _workspace_sem(self, workspace_ref: str) -> asyncio.Semaphore:
        entry = self._workspace_sems.get(workspace_ref)
        if entry is None:
            cap = self._workspace_max_concurrent_sessions
            entry = (asyncio.Semaphore(cap), cap)
            self._workspace_sems[workspace_ref] = entry
        return entry[0]

    async def _acquire_all(
        self,
        global_sem: asyncio.Semaphore,
        channel_sem: asyncio.Semaphore,
        workspace_sem: asyncio.Semaphore,
        ticket: AdmissionTicket,
    ) -> bool:
        """All-or-nothing acquire, bounded by `ticket.deadline_at`.

        Never holds a broader permit while blocked on a narrower one: a failed inner
        acquire rolls back everything taken so far before the next poll (round-1 finding 6).
        Each iteration rechecks the deadline via the injected clock (round-1 finding 2) —
        a ticket waiting behind a slow/stuck permit holder still expires on time.
        """
        while True:
            if self._clock() >= ticket.deadline_at:
                return False
            if self._recovered_count() >= global_sem._value:
                await asyncio.sleep(self._permit_poll_seconds)
                continue
            await global_sem.acquire()  # value was > 0 — does not suspend
            if self._recovered_count(channel=ticket.routed_event.channel) >= channel_sem._value:
                global_sem.release()
                await asyncio.sleep(self._permit_poll_seconds)
                continue
            await channel_sem.acquire()
            if (
                self._recovered_count(workspace_ref=ticket.workspace_ref) >= workspace_sem._value
                or self._recovered_owns_session(ticket.workspace_ref, ticket.session_ref)
            ):
                channel_sem.release()
                global_sem.release()
                await asyncio.sleep(self._permit_poll_seconds)
                continue
            await workspace_sem.acquire()
            return True

    async def _acquire_script(
        self,
        global_sem: asyncio.Semaphore,
        channel_sem: asyncio.Semaphore,
        script_sem: asyncio.Semaphore,
        ticket: ScriptAdmissionTicket,
    ) -> bool:
        """Acquire either the shared global or explicit script pool plus channel capacity."""
        while True:
            if self._clock() >= ticket.deadline_at:
                return False
            shared_pool = script_sem is global_sem
            if shared_pool:
                if self._recovered_count() >= global_sem._value:
                    await asyncio.sleep(self._permit_poll_seconds)
                    continue
            elif script_sem._value <= 0:
                await asyncio.sleep(self._permit_poll_seconds)
                continue
            if self._recovered_count(channel=ticket.event.channel) >= channel_sem._value:
                await asyncio.sleep(self._permit_poll_seconds)
                continue
            if shared_pool:
                await global_sem.acquire()
            await channel_sem.acquire()
            if not shared_pool:
                if script_sem._value <= 0:
                    channel_sem.release()
                    await asyncio.sleep(self._permit_poll_seconds)
                    continue
                await script_sem.acquire()
            return True

    # -- dedup / GC — keyed on OUR OWN `_released` bookkeeping, never on the
    # externally-exposed `ticket.completed` Future's `.done()` state (round-2 finding N2:
    # a caller cancelling that Future must never make Admission forget an active record).
    # ------------------------------------------------------------------------

    @staticmethod
    def _dedup_key(agent_uid: str, routed_event: RoutedEvent) -> str:
        return invocation_digest(agent_uid, routed_event.channel, routed_event.event_id)

    def _existing_ticket(self, dedup_key: str) -> AdmissionTicket | ScriptAdmissionTicket | RecoveredRun | None:
        record: AdmissionTicket | ScriptAdmissionTicket | RecoveredRun | None = self._tickets.get(dedup_key)
        if record is None:
            record = self._recovered.get(dedup_key)
        if record is None:
            return None
        if dedup_key not in self._released:
            # Still in-flight by OUR accounting, OR already failed by the deadline monitor
            # but its physical FIFO entry hasn't been dequeued/retired yet (round-3 finding
            # N3) — retained either way. This is also what bounds tombstones to exactly one
            # generation per key: no second ticket is ever created while this is true.
            return record
        completed_at = self._completed_at.get(dedup_key)
        if completed_at is not None and self._clock() - completed_at >= timedelta(
            seconds=self._idempotency_window_seconds
        ):
            self._forget(dedup_key)
            return None
        return record

    def _forget(self, dedup_key: str) -> None:
        self._tickets.pop(dedup_key, None)
        self._recovered.pop(dedup_key, None)
        self._completed_at.pop(dedup_key, None)
        self._released.discard(dedup_key)
        monitor = self._monitors.pop(dedup_key, None)
        if monitor is not None:
            monitor.cancel()
        self._monitor_expired.discard(dedup_key)

    def _sweep_expired(self) -> None:
        """Bounded, admission-path GC (round-1 finding 8) — never runs a background task."""
        now = self._clock()
        window = timedelta(seconds=self._idempotency_window_seconds)
        expired = [
            key for key in (self._tickets.keys() | self._recovered.keys())
            if key in self._released
            and (self._completed_at.get(key) is None or now - self._completed_at[key] >= window)
        ]
        for key in expired:
            self._forget(key)

        active_workspace_refs = {
            t.workspace_ref for t in self._tickets.values() if isinstance(t, AdmissionTicket)
        }
        active_workspace_refs.update(run.workspace_ref for run in self._recovered.values())
        for key, (sem, cap) in list(self._workspace_sems.items()):
            if key not in active_workspace_refs and sem._value == cap:
                del self._workspace_sems[key]

        active_channels = {
            t.routed_event.channel if isinstance(t, AdmissionTicket) else t.event.channel
            for t in self._tickets.values()
        }
        active_channels.update(run.channel for run in self._recovered.values())
        for key, (sem, cap) in list(self._channel_sems.items()):
            if key not in active_channels and sem._value == cap:
                del self._channel_sems[key]

    # -- admission --------------------------------------------------------

    def admit(self, routed_event: RoutedEvent) -> AdmissionTicket | RecoveredRun:
        """Dedup -> capacity -> FIFO-per-(Workspace,session) enqueue (contract §3)."""
        if self._closed:
            raise AdmissionClosedError("admission is closed")

        self._sweep_expired()

        dedup_key = self._dedup_key(self._agent_uid, routed_event)

        existing = self._existing_ticket(dedup_key)
        if existing is not None:
            if isinstance(existing, RecoveredRun):
                self._alarm_recovered("recovered Started outcome is unknown", existing)
            return existing

        if self._queued_total >= self._max_queued_total:
            if self._alarm is not None:
                self._alarm("admission queue capacity rejected", None)
            raise AdmissionQueueFullError(f"limits.maxQueuedTotal ({self._max_queued_total}) reached")

        workspace_ref = compute_workspace_ref(self._namespace, self._agent_uid, routed_event.workspace_key)
        session_ref = compute_session_ref(workspace_ref, routed_event.session_key)

        loop = asyncio.get_running_loop()
        ticket = AdmissionTicket(
            invocation_id=dedup_key,
            namespace=self._namespace,
            agent_uid=self._agent_uid,
            workspace_ref=workspace_ref,
            session_ref=session_ref,
            routed_event=routed_event,
            deadline_at=self._clock() + timedelta(seconds=self._max_invocation_seconds),
            started=loop.create_future(),
            completed=loop.create_future(),
        )

        # Mark-before-enqueue (preserved invariant): a duplicate arriving before the lane
        # even dequeues this ticket must still find it here.
        self._tickets[dedup_key] = ticket
        self._queued_total += 1
        self._monitors[dedup_key] = loop.create_task(self._monitor_deadline(dedup_key, ticket))

        lane = self._lanes.get(session_ref)
        if lane is None:
            lane = _Lane(admission=weakref.ref(self), lane_key=session_ref)
            self._lanes[session_ref] = lane
        lane.put(dedup_key, ticket)

        return ticket

    def admit_script(self, event: Event) -> ScriptAdmissionTicket:
        """Deduplicate and enqueue a deterministic script without deriving Workspace identity."""
        if self._closed:
            raise AdmissionClosedError("admission is closed")
        self._sweep_expired()
        dedup_key = invocation_digest(self._agent_uid, event.channel, event.event_id)
        existing = self._existing_ticket(dedup_key)
        if existing is not None:
            if isinstance(existing, RecoveredRun):
                self._alarm_recovered("recovered Started outcome is unknown", existing)
            if not isinstance(existing, ScriptAdmissionTicket):
                raise ValueError("conflicting agent and script delivery ownership")
            return existing
        if self._queued_total >= self._max_queued_total:
            if self._alarm is not None:
                self._alarm("admission queue capacity rejected", None)
            raise AdmissionQueueFullError(f"limits.maxQueuedTotal ({self._max_queued_total}) reached")

        loop = asyncio.get_running_loop()
        ticket = ScriptAdmissionTicket(
            invocation_id=dedup_key,
            namespace=self._namespace,
            agent_uid=self._agent_uid,
            event=event,
            deadline_at=self._clock() + timedelta(seconds=self._max_invocation_seconds),
            started=loop.create_future(),
            completed=loop.create_future(),
        )
        self._tickets[dedup_key] = ticket
        self._queued_total += 1
        self._monitors[dedup_key] = loop.create_task(self._monitor_deadline(dedup_key, ticket))

        context = event.context.get("gitlab") if isinstance(event.context, dict) else None
        project_id = context.get("projectId", "") if isinstance(context, dict) else ""
        lane_key = "script:" + invocation_digest(self._agent_uid, event.channel, str(project_id))
        lane = self._lanes.get(lane_key)
        if lane is None:
            lane = _Lane(admission=weakref.ref(self), lane_key=lane_key)
            self._lanes[lane_key] = lane
        lane.put(dedup_key, ticket)
        return ticket

    def recover(
        self,
        *,
        invocation_id: str,
        channel: str,
        workspace_ref: str,
        session_ref: str,
        config_version: str,
        deadline_at: datetime,
        instance_id: str,
        run_id: str | None,
        observer: Callable[[], Awaitable[Any]],
    ) -> RecoveredRun:
        """Synchronously claim recovered ownership before scheduling observation."""
        if self._closed:
            raise AdmissionClosedError("admission is closed")
        self._sweep_expired()
        ordinary = self._tickets.get(invocation_id)
        if ordinary is not None:
            raise ValueError("conflicting recovered run ownership")
        existing = self._recovered.get(invocation_id)
        if existing is not None:
            same = (
                existing.channel == channel
                and existing.workspace_ref == workspace_ref
                and existing.session_ref == session_ref
                and existing.config_version == config_version
                and existing.deadline_at == deadline_at
                and existing.instance_id == instance_id
                and not (existing.run_id is not None and run_id is not None and existing.run_id != run_id)
            )
            if not same:
                raise ValueError("conflicting recovered run ownership")
            self._alarm_recovered("recovered Started outcome is unknown", existing)
            return existing
        loop = asyncio.get_running_loop()
        started: asyncio.Future[None] = loop.create_future()
        started.set_exception(RuntimeError("recovered Started outcome is unknown"))
        started.exception()  # retrieve it: recovered Started is deliberately never successful
        recovered = RecoveredRun(
            invocation_id=invocation_id, channel=channel, workspace_ref=workspace_ref,
            session_ref=session_ref, config_version=config_version, deadline_at=deadline_at,
            instance_id=instance_id, run_id=run_id, started=started, completed=loop.create_future(),
        )
        self._recovered[invocation_id] = recovered
        self._queued_total += 1
        self._alarm_recovered("recovered Started outcome is unknown", recovered)
        self._recovery_tasks[invocation_id] = loop.create_task(
            self._observe_recovered(invocation_id, recovered, observer)
        )
        return recovered

    def _alarm_recovered(self, reason: str, recovered: RecoveredRun) -> None:
        if self._alarm is not None:
            self._alarm(reason, recovered)  # type: ignore[arg-type]

    def _recovered_count(self, *, channel: str | None = None, workspace_ref: str | None = None) -> int:
        return sum(
            key not in self._released
            and (channel is None or run.channel == channel)
            and (workspace_ref is None or run.workspace_ref == workspace_ref)
            for key, run in self._recovered.items()
        )

    def _recovered_owns_session(self, workspace_ref: str, session_ref: str) -> bool:
        return any(
            key not in self._released
            and run.workspace_ref == workspace_ref
            and run.session_ref == session_ref
            for key, run in self._recovered.items()
        )

    async def _observe_recovered(
        self,
        invocation_id: str,
        recovered: RecoveredRun,
        observer: Callable[[], Awaitable[Any]],
    ) -> None:
        current = asyncio.current_task()
        try:
            await observer()
        except asyncio.CancelledError:
            if self._closed and invocation_id not in self._released:
                if not recovered.completed.done():
                    recovered.completed.set_exception(RuntimeError("admission closed"))
                    recovered.completed.exception()
                self._release_capacity_once(invocation_id)
            raise
        except WorkspaceOwnerLost as exc:
            if not recovered.completed.done():
                recovered.completed.set_exception(exc)
                recovered.completed.exception()
            self._release_capacity_once(invocation_id)
        except Exception as exc:  # an unavailable observer never proves terminal state
            self._alarm_recovered(f"recovered run observation failed: {exc}", recovered)
        else:
            if invocation_id not in self._released:
                if not recovered.completed.done():
                    recovered.completed.set_result(None)
                self._release_capacity_once(invocation_id)
        finally:
            if self._recovery_tasks.get(invocation_id) is current:
                self._recovery_tasks.pop(invocation_id, None)

    def _fail(self, ticket: AdmissionTicket | ScriptAdmissionTicket, exc: BaseException) -> None:
        if not ticket.started.done():
            ticket.started.set_exception(exc)
        if not ticket.completed.done():
            ticket.completed.set_exception(exc)

    def _expire(self, ticket: AdmissionTicket | ScriptAdmissionTicket, reason: str) -> None:
        self._fail(ticket, TimeoutError(f"admission deadline_at exceeded: {reason}"))
        if self._alarm is not None:
            self._alarm(reason, ticket)

    def _release_capacity_once(self, dedup_key: str) -> None:
        """Idempotent — safe to call from the normal path, a cancellation drain, the
        deadline monitor, AND close()'s own sweep without double-decrementing
        `queued_total` (round-1 findings 3/4). This — not the exposed `completed` Future —
        is the ONLY signal `_existing_ticket`/`_sweep_expired` trust (round-2 finding N2)."""
        if dedup_key in self._released:
            return
        self._released.add(dedup_key)
        self._queued_total -= 1
        self._completed_at[dedup_key] = self._clock()

    def _cancel_monitor(self, dedup_key: str, ticket: AdmissionTicket | ScriptAdmissionTicket) -> bool:
        """Cancels and forgets `ticket`'s own deadline-monitor task, and clears any
        `_monitor_expired` marker it left — but ONLY while `ticket` is still the current
        occupant of `dedup_key` in `_tickets` (round-4 finding N5's ownership rule: never
        cancel/clear a NEWER same-digest generation's monitor/marker after a key has been
        reused). Returns whether the marker was present, i.e. the monitor had already
        expired this exact ticket before this call."""
        if self._tickets.get(dedup_key) is not ticket:
            return False
        monitor = self._monitors.pop(dedup_key, None)
        if monitor is not None:
            monitor.cancel()
        was_monitor_expired = dedup_key in self._monitor_expired
        self._monitor_expired.discard(dedup_key)
        return was_monitor_expired

    def _retire(self, dedup_key: str, ticket: AdmissionTicket | ScriptAdmissionTicket) -> None:
        """The one retirement rule for an actual FIFO entry (round-4 finding N5): ordinary
        dequeue (`_run_ticket`), cancellation drain (`_Lane._consume`), `close()` and GC
        (`_forget`) all funnel a ticket through this — its owned monitor task and any
        `_monitor_expired` marker never outlive it, regardless of which path retired it."""
        self._cancel_monitor(dedup_key, ticket)
        self._release_capacity_once(dedup_key)

    async def _monitor_deadline(
        self, dedup_key: str, ticket: AdmissionTicket | ScriptAdmissionTicket
    ) -> None:
        """Expires a ticket still sitting in its lane's queue once its deadline passes —
        `_run_ticket`'s own deadline checks only run once the lane actually dequeues the
        ticket, which never happens while an earlier same-session ticket never finishes
        (round-2 finding I2). `_run_ticket` cancels this task immediately once it starts
        handling the ticket, so the two expiry paths never race past that point.

        Loop condition is driven ONLY by internal task cancellation — never by the
        externally-exposed `ticket.completed` Future's state (round-3 finding N4: a caller
        cancelling that Future before this ticket is ever dequeued must not silently
        disable its watchdog). On expiry this records the failure in `_monitor_expired`
        (keyed by `dedup_key`, unambiguous because `_existing_ticket` never lets a second
        generation exist for the same key while this one is still unretired — round-3
        finding N3) and does NOT release capacity itself: that happens exactly once, when
        `_run_ticket` actually dequeues this same physical entry, whenever that occurs."""
        try:
            while True:
                if self._clock() >= ticket.deadline_at:
                    self._monitor_expired.add(dedup_key)
                    self._expire(ticket, "deadline exceeded while queued")
                    return
                await asyncio.sleep(self._permit_poll_seconds)
        except asyncio.CancelledError:
            pass
        finally:
            # Only remove our OWN registry entry — a same-key readmission may already have
            # installed a newer generation's monitor here while this one's cancellation was
            # still in flight (round-5 finding: unconditional pop clobbered that fresh task).
            if self._monitors.get(dedup_key) is asyncio.current_task():
                self._monitors.pop(dedup_key, None)

    async def _run_script_ticket(self, ticket: ScriptAdmissionTicket) -> None:
        if self._clock() >= ticket.deadline_at:
            self._expire(ticket, "deadline exceeded before permit wait")
            return
        global_sem = self._global_sem
        channel_sem = self._channel_sem(ticket.event.channel)
        script_sem = self._script_sem
        acquired = await self._acquire_script(global_sem, channel_sem, script_sem, ticket)
        if not acquired:
            self._expire(ticket, "deadline exceeded while waiting for script permits")
            return
        try:
            if self._clock() >= ticket.deadline_at:
                self._expire(ticket, "deadline exceeded immediately before script spawn")
                return
            try:
                await self._runner(ticket.event, ticket)
            except Exception as exc:  # noqa: BLE001 - resolve every future after Started too
                self._fail(ticket, exc)
                if self._alarm is not None:
                    self._alarm("webhook script runner failed", ticket)
                return
            if not ticket.started.done():
                ticket.started.set_exception(
                    RuntimeError("script runner completed without signalling Started")
                )
            if not ticket.completed.done():
                ticket.completed.set_result(None)
        finally:
            channel_sem.release()
            if script_sem is global_sem:
                global_sem.release()
            else:
                script_sem.release()

    async def _run_ticket(
        self, dedup_key: str, ticket: AdmissionTicket | ScriptAdmissionTicket
    ) -> None:
        """Processes exactly one physical FIFO entry. Because `_existing_ticket` never
        creates a second generation for `dedup_key` while the current one is unretired
        (round-3 finding N3), `ticket` is always the one `self._tickets[dedup_key]` still
        points to — no identity check against a possibly-superseded ticket is needed; the
        invariant makes that scenario structurally impossible rather than papering over it
        after the fact."""
        already_monitor_expired = self._cancel_monitor(dedup_key, ticket)
        try:
            if already_monitor_expired:
                # Already failed + alarmed by the deadline monitor while this was still
                # queued — nothing left to do but retire it (the `finally` below).
                return
            if isinstance(ticket, ScriptAdmissionTicket):
                await self._run_script_ticket(ticket)
                return
            if self._clock() >= ticket.deadline_at:
                self._expire(ticket, "deadline exceeded before permit wait")
                return
            global_sem = self._global_sem
            channel_sem = self._channel_sem(ticket.routed_event.channel)
            workspace_sem = self._workspace_sem(ticket.workspace_ref)

            acquired = await self._acquire_all(global_sem, channel_sem, workspace_sem, ticket)
            if not acquired:
                self._expire(ticket, "deadline exceeded while waiting for permits")
                return
            try:
                if self._clock() >= ticket.deadline_at:
                    self._expire(ticket, "deadline exceeded immediately before delivery")
                    return
                try:
                    result = await self._runner(ticket.routed_event, ticket)
                except Exception as exc:  # noqa: BLE001 - must resolve every future
                    self._fail(ticket, exc)
                    return
                if not ticket.started.done():
                    ticket.started.set_exception(
                        RuntimeError("runner completed without signalling Started")
                    )
                if not ticket.completed.done():
                    ticket.completed.set_result(result)
            finally:
                workspace_sem.release()
                channel_sem.release()
                global_sem.release()
        except asyncio.CancelledError:
            self._fail(ticket, asyncio.CancelledError("admission closed"))
            raise
        finally:
            self._retire(dedup_key, ticket)

    async def close(self) -> None:
        """Terminal: no `admit()` succeeds afterward. Drains tasks/permits/futures and
        accounts for every ticket regardless of whether its lane ever ran (round-1
        finding 4), and cancels every still-pending deadline monitor (round-2 finding N1
        class of leak, applied to monitors as well as lanes)."""
        self._closed = True
        recovery_tasks = list(self._recovery_tasks.values())
        for task in recovery_tasks:
            task.cancel()
        for task in recovery_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._recovery_tasks.clear()
        for lane in list(self._lanes.values()):
            lane.cancel()
        for lane in list(self._lanes.values()):
            await lane.wait_closed()
        self._lanes.clear()
        monitors = list(self._monitors.values())
        for monitor in monitors:
            monitor.cancel()
        for monitor in monitors:
            try:
                await monitor
            except asyncio.CancelledError:
                pass
        self._monitors.clear()
        for dedup_key, ticket in list(self._tickets.items()):
            self._fail(ticket, RuntimeError("admission closed"))
            self._retire(dedup_key, ticket)
        for invocation_id, recovered in list(self._recovered.items()):
            if invocation_id not in self._released:
                if not recovered.completed.done():
                    recovered.completed.set_exception(RuntimeError("admission closed"))
                    recovered.completed.exception()
                self._release_capacity_once(invocation_id)


class _Lane:
    """One asyncio.Queue + one consumer task per `session_ref` — strict FIFO (contract §3)."""

    def __init__(self, admission: weakref.ref[Admission], lane_key: str) -> None:
        self._admission_ref = admission
        self._lane_key = lane_key
        self._queue: asyncio.Queue[tuple[str, AdmissionTicket | ScriptAdmissionTicket]] = asyncio.Queue()
        self._task = asyncio.get_running_loop().create_task(self._consume())

    def put(self, dedup_key: str, ticket: AdmissionTicket | ScriptAdmissionTicket) -> None:
        self._queue.put_nowait((dedup_key, ticket))

    def is_empty(self) -> bool:
        return self._queue.empty()

    def cancel(self) -> None:
        self._task.cancel()

    async def wait_closed(self) -> None:
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def _deregister(self, admission: Admission) -> None:
        current = admission._lanes.get(self._lane_key)
        if current is self:
            del admission._lanes[self._lane_key]

    async def _consume(self) -> None:
        try:
            while True:
                dedup_key, ticket = await self._queue.get()
                admission = self._admission_ref()
                try:
                    if admission is not None:
                        await admission._run_ticket(dedup_key, ticket)
                finally:
                    self._queue.task_done()
                # Round-2 N1: a drained-idle consumer must TERMINATE here, not loop back
                # to `queue.get()` forever on a queue no future admit() can reach anymore
                # (it was just deregistered). `return` deliberately sits OUTSIDE the
                # `finally` above (a `return`/`break`/`continue` lexically inside a
                # `finally` block can silently swallow a pending exception — CPython warns
                # on it — even though this one is unreachable from that path).
                if admission is not None and self.is_empty():
                    self._deregister(admission)
                    return
        except asyncio.CancelledError:
            admission = self._admission_ref()
            while True:
                try:
                    dedup_key, ticket = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if admission is not None:
                    admission._fail(ticket, asyncio.CancelledError("admission closed"))
                    admission._retire(dedup_key, ticket)
                self._queue.task_done()
            if admission is not None:
                self._deregister(admission)
            raise

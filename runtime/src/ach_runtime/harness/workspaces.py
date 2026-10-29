# SPDX-License-Identifier: MIT
"""Workspace coordination over a `WorkspaceSource` (contract v2): the source answers "give me
a running instance of this Workspace, take it away, tell me what already runs"; the manager
keeps everything else.

Every mutating operation (`ensure`/`close`/`recover`) is owned per-ref: the owner is
retained across cancelled waiters (a cancelled caller detaches via `asyncio.shield`, it
never cancels the underlying work), same-kind concurrent calls coalesce onto one shared
task, and different-kind concurrent calls for the same ref serialize through a per-ref
lock so at most one of them is ever inside a callback/mutation at a time. `close()`/
`recover()` remove the ref from the delivered-endpoint map before any await, so a racing
`ensure()` can never return an endpoint mid-freeze.

Identity comes from the process: the manager learns an instance's ID by probing it, never
from the source. Every detected loss goes through `recover(ref, lost_instance_id)`, which
compares against the published endpoint, so a late report about an older instance never
touches a newer one. Retiring a unit has no deadline at the source; the manager bounds its
own wait, and the slot stays held until the source confirms the unit is gone.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid as _uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.harness.refs import workspace_name
from ach_runtime.observability import alarm
from ach_runtime.storage.api import PersistReceipt, SnapshotTransientFailure

_FULL_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_LOG = logging.getLogger(__name__)


def _require_canonical_uuid(value: str, field: str) -> str:
    try:
        parsed = _uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{field} must be a canonical UUID: {value!r}") from exc
    if str(parsed) != value:
        raise ValueError(f"{field} must be the lowercase canonical UUID form: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    namespace: str
    agent_uid: str
    workspace_ref: str

    def __post_init__(self) -> None:
        if not self.namespace:
            raise ValueError("namespace must not be empty")
        _require_canonical_uuid(self.agent_uid, "agent_uid")
        if not _FULL_HEX_RE.fullmatch(self.workspace_ref):
            raise ValueError(f"workspace_ref must be a 64-hex-char digest: {self.workspace_ref!r}")


@dataclass(frozen=True, slots=True)
class WorkspaceEndpoint:
    ref: WorkspaceRef
    name: str
    instance_id: str
    base_url: str


@dataclass(frozen=True, slots=True)
class Placement:
    """What a source hands back. The instance ID is deliberately absent: it comes from probing."""

    name: str
    base_url: str
    registration: int  # new for every PUT or adopted unit; the same URL registered twice differs


class WorkspaceAssemblyUnavailable(Exception):
    """A required callback seam is missing, a commit receipt does not match the
    workspace it claims to persist, or a launch spec cannot be built -- raised before any
    host-visible change is attempted or accepted."""


class WorkspaceConflict(Exception):
    """The source already hosts this Workspace, or its registration was replaced while the
    manager was still activating it."""


class WorkspaceUnhosted(Exception):
    """The host registered no execution unit before the deadline."""


class InstanceMismatch(RuntimeError):
    """A response carried an `X-ACH-Instance-Id` other than the endpoint's: the unit was
    restarted or replaced. Raised by the transfer and mini-request paths."""

    def __init__(self, message: str = "mini-harness instance ID did not match observation") -> None:
        super().__init__(message)


def instance_changed(response: Any, endpoint: "WorkspaceEndpoint") -> bool:
    """True only when the response names a DIFFERENT instance. A missing header (a 500 from
    middleware, a gateway error) is a plain error, never a loss."""
    got = response.headers.get("X-ACH-Instance-Id")
    return got is not None and got != endpoint.instance_id


class WorkspaceRetireTimeout(Exception):
    """The host has not confirmed the unit is gone; the slot stays held until it does."""


class WorkspaceCapacityExceeded(Exception):
    """`limits.maxActiveWorkspaces` slots are all held by starting/active/closing/
    recovering/persistence-failed/retiring workspaces."""


class WorkspaceSource(Protocol):
    async def place(self, ref: WorkspaceRef, name: str, *, deadline: float) -> Placement:
        """Return a started execution unit for this Workspace. Raises WorkspaceUnhosted at
        the absolute monotonic `deadline`."""

    async def retire(self, name: str) -> None:
        """Ask for the unit to go away; return once it is gone. No deadline: the manager
        bounds the wait."""

    async def adopt(self) -> list[tuple[WorkspaceRef, Placement]]:
        """Units already running at startup. Local returns []; hosted waits for the
        inventory, with no deadline."""

    def observed(self, placement: Placement, instance_id: str) -> bool:
        """Record the probed instance ID right after the first successful probe. False if
        that registration is no longer current."""

    def serving(self, placement: Placement, instance_id: str) -> bool:
        """Publish placement as serving. False if that registration is no longer current."""

    def on_lost(self, callback: Callable[[str, str | None], Awaitable[None]]) -> None:
        """Register the manager's handler for 'unit gone without being asked': (name, instance_id)."""

    def gateway_available(self) -> bool:
        """Whether the host reports its sandbox gateway as reachable. While it is not,
        failures are expected and neither recovery nor retirement acts on them."""

    async def wait_gateway(self, deadline: float) -> bool:
        """True once the gateway is available; False if still down at the absolute
        `time.monotonic()` `deadline`."""

    def close(self) -> None:
        """Called by `Control.close()`; release anything that blocks shutdown."""


RestoreCallback = Callable[[WorkspaceEndpoint], Awaitable[None]]
CommitCallback = Callable[[WorkspaceEndpoint], Awaitable[PersistReceipt | None]]
ReconnectCallback = Callable[[WorkspaceEndpoint], Awaitable[None]]
# (base_url, workspace name, absolute monotonic deadline) -> the instance ID the process reports
HealthCallback = Callable[[str, str, float], Awaitable[str]]


class _RefOwner:
    """Per-ref coordination: one lock serializes any two operations on the same ref,
    and `tasks` lets same-kind concurrent callers coalesce onto one shared task without
    letting a cancelled caller cancel (or lose track of) the underlying work."""

    __slots__ = ("lock", "tasks")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.tasks: dict[str, asyncio.Task[Any]] = {}


class WorkspaceManager:
    def __init__(
        self,
        config: RuntimeConfig,
        source: WorkspaceSource,
        *,
        restore_workspace: RestoreCallback | None,
        commit_workspace: CommitCallback | None,
        reconnect_workspace: ReconnectCallback | None,
        wait_mini_health: HealthCallback | None = None,
    ) -> None:
        if reconnect_workspace is None:
            raise WorkspaceAssemblyUnavailable("reconnect_workspace is required in every mode")
        if config.workspace.persistence.enabled and (
            restore_workspace is None or commit_workspace is None
        ):
            raise WorkspaceAssemblyUnavailable(
                "restore_workspace and commit_workspace are required when "
                "workspace.persistence.enabled is true"
            )
        self._config = config
        self._source = source
        self._restore = restore_workspace
        self._commit = commit_workspace
        self._reconnect = reconnect_workspace
        self._wait_mini_health = wait_mini_health
        self._capacity = config.limits.max_active_workspaces
        self._occupied: set[str] = set()
        self._slot_released = asyncio.Event()
        self._active: dict[str, WorkspaceEndpoint] = {}
        self._owners: dict[str, _RefOwner] = {}
        self._errors: dict[str, BaseException] = {}
        self._adoption: asyncio.Task[None] | None = None
        self._loss_tasks: set[asyncio.Task[Any]] = set()
        self._deferred_adoptions: set[asyncio.Task[Any]] = set()
        # ponytail: never pruned; one id per lost instance.
        self._lost_instances: set[str] = set()
        source.on_lost(self._source_lost)

    # -- seam used by Control and Lifecycle -----------------------------------------------
    def bind_callbacks(
        self,
        *,
        restore: RestoreCallback | None = None,
        commit: CommitCallback | None = None,
        reconnect: ReconnectCallback | None = None,
        wait_mini_health: HealthCallback | None = None,
    ) -> None:
        """Late wiring: Lifecycle and Control are built after the manager and own these."""
        self._restore = restore or self._restore
        self._commit = commit or self._commit
        self._reconnect = reconnect or self._reconnect
        self._wait_mini_health = wait_mini_health or self._wait_mini_health

    def active_endpoint(self, key: str) -> WorkspaceEndpoint | None:
        return self._active.get(key)

    def active_endpoints(self) -> dict[str, WorkspaceEndpoint]:
        return dict(self._active)

    def is_lost(self, instance_id: str) -> bool:
        return instance_id in self._lost_instances

    # -- identity --------------------------------------------------------------------------
    def _validate_ref(self, ref: WorkspaceRef) -> None:
        agent = self._config.agent
        if ref.namespace != agent.namespace or ref.agent_uid != agent.uid:
            raise ValueError(
                f"WorkspaceRef {ref.namespace!r}/{ref.agent_uid!r} does not match configured "
                f"agent {agent.namespace!r}/{agent.uid!r}"
            )

    # -- capacity ----------------------------------------------------------------------------
    def _has_slot(self, key: str) -> bool:
        return key in self._occupied or len(self._occupied) < self._capacity

    def _reserve(self, key: str) -> None:
        if key in self._occupied:
            return
        if not self._has_slot(key):
            raise WorkspaceCapacityExceeded(
                f"limits.maxActiveWorkspaces={self._capacity} already holds every slot"
            )
        self._occupied.add(key)

    def _wake_slot_waiters(self) -> None:
        self._slot_released.set()
        self._slot_released = asyncio.Event()

    def _release(self, key: str) -> None:
        self._occupied.discard(key)
        self._errors.pop(key, None)
        self._wake_slot_waiters()

    async def _wait_for_slot(self, deadline_at: datetime | None) -> None:
        # ponytail: waiters are woken together, not FIFO; add a queue if starvation shows up.
        remaining = (deadline_at - datetime.now(timezone.utc)).total_seconds() if deadline_at else 0
        if remaining <= 0:
            raise WorkspaceCapacityExceeded(
                f"limits.maxActiveWorkspaces={self._capacity} already holds every slot"
            )
        try:
            async with asyncio.timeout(remaining):
                await self._slot_released.wait()
        except TimeoutError:
            raise WorkspaceCapacityExceeded(
                f"limits.maxActiveWorkspaces={self._capacity} stayed full until the deadline"
            ) from None

    def _reserve_known(self, key: str) -> None:
        """Adoption reservation: units the host already runs bypass the capacity check --
        they hold a slot in the real world regardless of configured capacity."""
        self._occupied.add(key)

    def _startup_deadline(self, deadline_at: datetime | None) -> float:
        """One absolute monotonic startup deadline (I4): clipped by both
        `workspace.readyTimeoutSeconds` and the remaining time on the original UTC admission
        deadline, so elapsed placement/health time is naturally subtracted out of whatever is
        checked against this same deadline next -- never a fresh timer per step. No admission
        ticket (recovery/adoption) keeps the configured bound."""
        bound = float(self._config.workspace.ready_timeout_seconds)
        if deadline_at is not None:
            remaining = (deadline_at - datetime.now(timezone.utc)).total_seconds()
            bound = max(0.0, min(bound, remaining))
        return time.monotonic() + bound

    # -- per-ref coalescing/serialization ------------------------------------------------
    async def _run(self, key: str, kind: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        owner = self._owners.setdefault(key, _RefOwner())
        task = owner.tasks.get(kind)
        if task is None or task.done():

            async def runner() -> Any:
                async with owner.lock:
                    return await factory()

            task = asyncio.ensure_future(runner())
            owner.tasks[kind] = task

            def _cleanup(done: asyncio.Task[Any], _owner: _RefOwner = owner, _kind: str = kind) -> None:
                if _owner.tasks.get(_kind) is done:
                    del _owner.tasks[_kind]

            task.add_done_callback(_cleanup)
        return await asyncio.shield(task)

    # -- activation --------------------------------------------------------------------------
    async def ensure(
        self, ref: WorkspaceRef, *, deadline_at: datetime | None = None
    ) -> WorkspaceEndpoint:
        self._validate_ref(ref)
        key = ref.workspace_ref

        # Delivery loop: after any awaited lifecycle/adoption/activation task, re-check
        # pending close/recover, retained errors and the CURRENT `_active` publication
        # before returning -- an awaited task's result can go stale the instant a
        # lifecycle operation is queued behind it, and must never be delivered as-is.
        while True:
            owner = self._owners.get(key)
            if owner is not None:
                lifecycle = next(
                    (
                        task
                        for lifecycle_kind in ("close", "recover", "retire", "adopt")
                        if (task := owner.tasks.get(lifecycle_kind)) is not None and not task.done()
                    ),
                    None,
                )
                if lifecycle is not None:
                    await asyncio.shield(lifecycle)
                    continue

            error = self._errors.get(key)
            if error is not None:
                raise error

            # Nothing is allocated before the units that already run have been adopted.
            task = self._adoption
            if task is None or not task.done():
                await self.adopt()
                continue

            endpoint = self._active.get(key)
            if endpoint is not None:
                return endpoint
            if not self._has_slot(key):
                await self._wait_for_slot(deadline_at)
                continue
            self._reserve(key)
            await self._run(
                key, "activate", lambda: self._activate(ref, key, deadline_at=deadline_at)
            )
            # Do not return this raw result: loop back and re-check pending lifecycle/
            # errors/current publication before delivering anything to the caller.

    async def _activate(
        self, ref: WorkspaceRef, key: str, *, deadline_at: datetime | None = None
    ) -> WorkspaceEndpoint:
        error = self._errors.get(key)
        if error is not None:
            raise error
        endpoint = self._active.get(key)
        if endpoint is not None:
            return endpoint
        self._reserve(key)
        return await self._start(ref, key, self._startup_deadline(deadline_at))

    async def _start(self, ref: WorkspaceRef, key: str, deadline: float) -> WorkspaceEndpoint:
        """place -> probe -> observed -> restore -> reconnect -> serving -> publish. A fresh
        instance always restores: its directories are empty. Any failure retires the unit and
        releases the slot once the host confirms it is gone."""
        name = workspace_name(self._config.agent.name, key)
        phase = "place"
        try:
            placement = await self._source.place(ref, name, deadline=deadline)
        except (WorkspaceUnhosted, WorkspaceAssemblyUnavailable, asyncio.CancelledError):
            # The source provably holds nothing: it dropped its entry (unhosted, cancelled
            # before registration) or refused the launch spec before publishing anything.
            self._release(key)
            raise
        # Any other error, notably WorkspaceConflict (the source already hosts this name),
        # re-raises WITHOUT releasing: the slot stays held for whatever the source holds.
        try:
            endpoint = await self._probe(ref, placement, deadline)
            phase = "observed"
            if not self._source.observed(placement, endpoint.instance_id):
                raise WorkspaceConflict(f"{name}: registration was replaced during activation")
            if self._restore is not None:
                phase = "restore"
                await self._restore(endpoint)
            phase = "reconnect"
            await self._reconnect(endpoint)
            phase = "serving"
            if not self._source.serving(placement, endpoint.instance_id):
                raise WorkspaceConflict(f"{name}: registration was replaced during activation")
        except BaseException as exc:
            _LOG.error(
                "Workspace activation failed: workspace_ref=%s workspace_name=%s phase=%s error=%s",
                key, name, phase, type(exc).__name__,
            )
            await self._retire(key, name)
            raise
        self._active[key] = endpoint
        return endpoint

    async def _probe(self, ref: WorkspaceRef, placement: Placement, deadline: float) -> WorkspaceEndpoint:
        if self._wait_mini_health is None:
            raise WorkspaceAssemblyUnavailable("wait_mini_health is required to learn the instance ID")
        instance_id = await self._wait_mini_health(placement.base_url, placement.name, deadline)
        return WorkspaceEndpoint(ref, placement.name, instance_id, placement.base_url)

    # -- retiring ------------------------------------------------------------------------------
    async def _retire(self, key: str, name: str) -> bool:
        """Ask the source to retire the unit. True, with the slot released, once the host
        confirmed it. After `shutdownTimeoutSeconds` the wait ends with anomaly
        `workspace_retire_timeout` and the slot stays held: the retire keeps running and
        its completion releases the slot exactly once."""
        task = asyncio.ensure_future(self._source.retire(name))
        try:
            await asyncio.wait_for(
                asyncio.shield(task), self._config.workspace.shutdown_timeout_seconds
            )
        except TimeoutError:
            alarm("workspace_retire_timeout", workspace_ref=key)
            timeout_error = WorkspaceRetireTimeout(f"{name}: the host has not confirmed the stop")
            self._errors[key] = timeout_error
            task.add_done_callback(lambda done: self._retired_late(key, timeout_error, done))
            return False
        self._release(key)
        return True

    def _retired_late(
        self, key: str, timeout_error: WorkspaceRetireTimeout, task: asyncio.Task[None]
    ) -> None:
        if task.cancelled() or self._errors.get(key) is not timeout_error:
            return  # settled already: never free a slot a newer activation may hold
        self._release(key)

    # -- close -------------------------------------------------------------------------------
    async def close(self, ref: WorkspaceRef) -> None:
        self._validate_ref(ref)
        key = ref.workspace_ref
        if key not in self._occupied:
            return
        unpublished = self._active.pop(key, None)
        name = workspace_name(self._config.agent.name, key)
        await self._run(key, "close", lambda: self._do_close(ref, key, name, unpublished))

    async def _do_close(
        self, ref: WorkspaceRef, key: str, name: str, unpublished: WorkspaceEndpoint | None
    ) -> None:
        # An activation that finished while this close waited for the lock published its own.
        endpoint = self._active.pop(key, None) or unpublished
        if endpoint is None or key not in self._occupied:
            return
        try:
            try:
                receipt = await self._commit(endpoint) if self._commit is not None else None
            except SnapshotTransientFailure:
                # Still serving: Lifecycle retries the close after its bounded cooldown.
                self._active[key] = endpoint
                raise
            self._validate_receipt(receipt, ref)
        except BaseException as exc:
            if isinstance(exc, InstanceMismatch):
                # The unit restarted or was replaced while we were closing it: its files
                # cannot be trusted and nothing was committed. This is a LOSS, not
                # PersistenceFailed: the Workspace does not keep serving. `close()` already
                # popped `_active`, so `recover()` would find nothing -- handle it here.
                # The `recover` that `report_lost` scheduled no-ops for the same reason, so
                # `pod_unhealthy` is emitted exactly once.
                self._lost_instances.add(endpoint.instance_id)
                alarm("pod_unhealthy", workspace_ref=key)  # "execution unit lost"
                await self._retire(key, name)  # slot freed on DELETE, or the retire-timeout path
                return
            self._errors[key] = exc
            raise
        # The commit is durable; a retire timeout is an anomaly, not a failed close.
        await self._retire(key, name)

    def _validate_receipt(self, receipt: PersistReceipt | None, ref: WorkspaceRef) -> None:
        if receipt is None:
            if self._config.workspace.persistence.enabled:
                raise WorkspaceAssemblyUnavailable(
                    "commit_workspace returned no receipt but workspace persistence is enabled"
                )
            return
        agent = self._config.agent
        state_ref = receipt.ref
        if (
            state_ref.domain != "workspace"
            or state_ref.workspace_ref != ref.workspace_ref
            or state_ref.session_ref is not None
            or state_ref.agent.name != agent.name
            or state_ref.agent.namespace != agent.namespace
            or state_ref.agent.uid != agent.uid
        ):
            raise WorkspaceAssemblyUnavailable(
                f"commit receipt does not match workspace {ref.workspace_ref!r}"
            )

    # -- loss and recovery -------------------------------------------------------------------
    def report_lost(self, endpoint: WorkspaceEndpoint) -> None:
        """An instance-ID mismatch seen on a response from `endpoint`: recover in the background."""
        task = asyncio.ensure_future(self.recover(endpoint.ref, endpoint.instance_id))
        self._loss_tasks.add(task)

        def _done(done: asyncio.Task[Any]) -> None:
            self._loss_tasks.discard(done)
            if not done.cancelled() and done.exception() is not None:
                _LOG.warning("Workspace recovery after a lost instance failed: %s",
                             type(done.exception()).__name__)

        task.add_done_callback(_done)

    async def _source_lost(self, name: str, instance_id: str | None) -> None:
        """The host reported the unit gone. Before its first probe there is nothing to
        recover: the activation in flight fails on its own."""
        endpoint = next((e for e in self._active.values() if e.name == name), None)
        if endpoint is not None and endpoint.instance_id == instance_id:
            self.report_lost(endpoint)

    async def recover(self, ref: WorkspaceRef, lost_instance_id: str) -> WorkspaceEndpoint | None:
        """Replace the instance `lost_instance_id`. A no-op when it is no longer the
        published one. Ends without a new activation when the host does not confirm the
        old unit stopped: it may still be alive, and the late confirmation frees the slot."""
        self._validate_ref(ref)
        key = ref.workspace_ref
        return await self._run(key, "recover", lambda: self._do_recover(ref, key, lost_instance_id))

    async def _do_recover(
        self, ref: WorkspaceRef, key: str, lost_instance_id: str
    ) -> WorkspaceEndpoint | None:
        current = self._active.get(key)
        if current is None or current.instance_id != lost_instance_id:
            return None
        del self._active[key]
        self._lost_instances.add(lost_instance_id)
        alarm("pod_unhealthy", workspace_ref=key)  # "execution unit lost"
        name = workspace_name(self._config.agent.name, key)
        if not await self._retire(key, name):
            return None
        # WorkspaceCapacityExceeded here is intended: adopted units count against the limit
        # and the next demand waits in `_wait_for_slot`.
        self._reserve(key)
        return await self._start(ref, key, self._startup_deadline(None))

    # -- adoption ------------------------------------------------------------------------------
    async def adopt(self) -> None:
        """Take over the units the host already runs (once). A unit that cannot be adopted
        is retired in the background; readiness does not wait for that."""
        if self._adoption is None:
            self._adoption = asyncio.ensure_future(self._run_adoption())
        await asyncio.shield(self._adoption)

    def cancel_adoption(self) -> None:
        """Shutdown: an adoption still waiting for the host's inventory is cancelled."""
        if self._adoption is not None and not self._adoption.done():
            self._adoption.cancel()
        for task in list(self._deferred_adoptions):
            task.cancel()

    async def _run_adoption(self) -> None:
        units = await self._source.adopt()
        for ref, _ in units:
            self._reserve_known(ref.workspace_ref)
        await asyncio.gather(
            *(
                self._run(ref.workspace_ref, "adopt", lambda ref=ref, p=p: self._adopt_one(ref, p))
                for ref, p in units
            )
        )
        self._wake_slot_waiters()

    def gateway_available(self) -> bool:
        return self._source.gateway_available()

    async def wait_gateway(self, deadline: float) -> bool:
        """True once the gateway is available; False if still down at the absolute
        `time.monotonic()` `deadline`."""
        return await self._source.wait_gateway(deadline)

    async def _adopt_when_gateway_returns(self, ref: WorkspaceRef, placement: Placement) -> None:
        while not self._source.gateway_available():
            await self.wait_gateway(time.monotonic() + 30)
        await self._adopt_one(ref, placement)

    def _defer_adoption(self, ref: WorkspaceRef, placement: Placement) -> None:
        """Re-run the adoption once the gateway is back, as the ref's own "adopt" task:
        `ensure()` waits for it (no `place()` against a unit that is still hosted), it runs
        under the ref's lock after the current attempt returns, and `cancel_adoption()`
        cancels it. The task replaces the finishing attempt in `owner.tasks`."""
        key = ref.workspace_ref
        owner = self._owners.setdefault(key, _RefOwner())

        async def runner() -> None:
            async with owner.lock:
                await self._adopt_when_gateway_returns(ref, placement)

        task = asyncio.ensure_future(runner())
        owner.tasks["adopt"] = task
        self._deferred_adoptions.add(task)

        def _cleanup(done: asyncio.Task[Any]) -> None:
            self._deferred_adoptions.discard(done)
            if owner.tasks.get("adopt") is done:
                del owner.tasks["adopt"]
            if not done.cancelled():
                done.exception()

        task.add_done_callback(_cleanup)

    async def _adopt_one(self, ref: WorkspaceRef, placement: Placement) -> None:
        key = ref.workspace_ref
        try:
            endpoint = await self._probe(ref, placement, self._startup_deadline(None))
            if not self._source.observed(placement, endpoint.instance_id):
                raise WorkspaceConflict(f"{placement.name}: registration was replaced")
            await self._reconnect(endpoint)
            if not self._source.serving(placement, endpoint.instance_id):
                raise WorkspaceConflict(f"{placement.name}: registration was replaced")
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not self._source.gateway_available():
                _LOG.warning("Workspace adoption deferred: gateway unavailable: workspace_ref=%s", key)
                self._defer_adoption(ref, placement)
                return
            _LOG.error("Workspace adoption failed: workspace_ref=%s error=%s", key, type(exc).__name__)
            # Readiness does not wait for the host's DELETE, but `ensure()` must not place
            # again before it: run the retire through `_run` so it is the ref's pending
            # "retire" task (`ensure()` awaits it, and the per-ref lock serializes any
            # activation behind it). `sleep(0)` lets the runner register itself in
            # `owner.tasks` before adoption can finish.
            retiring = asyncio.ensure_future(
                self._run(key, "retire", lambda: self._retire(key, placement.name))
            )
            retiring.add_done_callback(lambda done: done.cancelled() or done.exception())
            await asyncio.sleep(0)
            return
        self._active[key] = endpoint

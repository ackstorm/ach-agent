"""Native execution service used by the mini-harness.

This module is deliberately a thin supervisor: native protocol parsing and session
maintenance remain in the selected driver, while invocation identity, deadlines and
serializable events live here.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from ach_agent.engine.base.driver import EngineConfig, EngineDriver, TurnResult
from ach_agent.engine.base.pool import EnginePool
from ach_agent.engine.lifecycle import NativeLaunchFailed, OwnedProcessCleanupError
from ach_agent.engine.mcp_passthrough import to_engine_entry
from ach_agent.engine.workspace import (
    WorkspaceHandoffFailed,
    WorkspaceHookFailed,
    build_public_env,
    handoff_bundle,
    prepare_workspace,
    run_public_hook,
    workspace_dir,
)
from ach_agent.execution.wire import (
    AcquireRequest,
    ExecutionEvent,
    ExecutionHandle,
    ReleaseRequest,
    SessionOperation,
    SessionReadyRequest,
    TurnRequest,
    WorkspaceCleanupAckRequest,
    WorkspaceHandoffRequest,
    WorkspacePrepareRequest,
    WorkspaceStoppedEvent,
)

log = structlog.get_logger(__name__)


@dataclass
class _Invocation:
    handle: ExecutionHandle
    session_key: str
    conversation_key: str
    reuse: bool
    server: Any
    deadline: float
    current_ref: str | None = None
    released: bool = False
    task: asyncio.Task[Any] | None = None
    turn_active: bool = False
    terminal: bool = False
    deadline_task: asyncio.Task[None] | None = None
    cached_ref_pending: bool = False
    wake_task: asyncio.Task[None] | None = None
    cleanup_error: str | None = None
    maintenance_active: bool = False
    maintenance_task: asyncio.Task[Any] | None = None
    cleanup_task: asyncio.Task[None] | None = None
    turn_ids: set[str] = dataclasses.field(default_factory=set)
    buffered_events: int = 0
    buffered_bytes: int = 0
    leased_bytes: int = 0
    queued_bytes: int = 0
    output_error: Exception | None = None
    event_queue: asyncio.Queue[Any] | None = None
    cleanup_deadline: float | None = None
    session_ready: tuple[str, str, asyncio.Event] | None = None
    workspace_cleanup_timeout_seconds: float = 0.0


@dataclass
class _WorkspaceReservation:
    request: WorkspacePrepareRequest
    workspace: Path
    deadline: float
    acquiring: bool = False
    cancel_requested: bool = False
    cleanup_task: asyncio.Task[None] | None = None
    cleanup_owner: asyncio.Task[Any] | None = None
    skip_operation: asyncio.Task[Any] | None = None


@dataclass
class _WorkspaceCleanupBarrier:
    event: WorkspaceStoppedEvent
    acknowledged: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)
    failed: bool = False


class OutputLimitExceeded(RuntimeError):
    """The client stream exceeded one of the bounded NDJSON output limits."""


MAX_NDJSON_RECORD_BYTES = 1 * 1024 * 1024
MAX_STREAM_EVENTS = 256
MAX_INVOCATION_STREAM_BYTES = 4 * 1024 * 1024
MAX_AGGREGATE_QUEUED_BYTES = 32 * 1024 * 1024
CLEANUP_DEADLINE_SECONDS = 10.0
CLEANUP_PROCESS_MARGIN_SECONDS = 5.0


def _engine_config(public: Any) -> EngineConfig:
    """Copy the approved wire fields into the native driver's config type."""
    values = public.model_dump(exclude={"mcp_templates"})
    values["extra_mcp_servers"] = {
        name: to_engine_entry(spec) for name, spec in public.mcp_templates.items()
    }
    return EngineConfig(**values)


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            key: _json_value(child)
            for key, child in dataclasses.asdict(value).items()  # type: ignore[arg-type]
        }
    if hasattr(value, "model_dump"):
        return _json_value(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(child) for child in value]
    return value


def _event_bytes(event: ExecutionEvent) -> bytes:
    return (
        json.dumps(
            event.model_dump(mode="json"), separators=(",", ":"), ensure_ascii=False
        ).encode()
        + b"\n"
    )


class ExecutionService:
    def __init__(
        self,
        driver: EngineDriver,
        sessions_map: MutableMapping[str, str] | None = None,
    ) -> None:
        self.driver = driver
        self.pool = EnginePool(driver=driver, sessions_map=sessions_map, strict_cleanup=True)
        self._invocations: dict[str, _Invocation] = {}
        self._acquiring: set[str] = set()
        self._acquiring_lanes: dict[str, str] = {}
        self._acquire_tasks: dict[str, asyncio.Task[Any]] = {}
        self.instance_id = str(uuid.uuid4())
        self._unhealthy = False
        self._controller_id: str | None = None
        self._admission_open = False
        # The legacy in-process runner uses the service directly.  The HTTP app
        # enables this gate before exposing any execution route.
        self.controller_required = False
        self._queued_stream_bytes = 0
        self.shutdown_requested = False
        self.controller_cleanup_error: str | None = None
        self._ttl_watchers: set[asyncio.Task[None]] = set()
        self._controller_cleanup_task: asyncio.Task[None] | None = None
        self._workspace_tasks: dict[str, asyncio.Task[Any]] = {}
        self._workspace_reservations: dict[str, _WorkspaceReservation] = {}
        self._workspace_cancelled: set[str] = set()
        self.workspace_cleanup_errors: list[str] = []
        self._controller_events: asyncio.Queue[WorkspaceStoppedEvent] | None = None
        self._workspace_barriers: dict[str, _WorkspaceCleanupBarrier] = {}

    @property
    def can_accept_controller(self) -> bool:
        return not self._unhealthy and self._controller_id is None

    @property
    def controller_id(self) -> str | None:
        return self._controller_id

    async def claim_controller(self, controller_id: str) -> None:
        if self._unhealthy:
            raise RuntimeError("native cleanup failed; execution service is unhealthy")
        if self._controller_id is not None:
            raise RuntimeError("execution service already has a controller")
        self._controller_id = controller_id
        self._admission_open = True
        self._controller_events = asyncio.Queue(maxsize=64)

    def _assert_controller(self, controller_id: str) -> None:
        if self._unhealthy:
            raise RuntimeError("native cleanup failed; execution service is unhealthy")
        if not self.controller_required and self._controller_id is None:
            return
        if not self._admission_open or self._controller_id != controller_id:
            raise ValueError("obsolete controller")

    def _mark_unhealthy(self) -> None:
        self._unhealthy = True
        self.shutdown_requested = True
        self._admission_open = False
        for barrier in self._workspace_barriers.values():
            barrier.failed = True
            barrier.acknowledged.set()

    @staticmethod
    def _observe_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    async def release_controller(self, controller_id: str) -> None:
        """Close admission and finish all owned cleanup before another controller."""
        if self._controller_id != controller_id:
            return
        self._admission_open = False
        for barrier in self._workspace_barriers.values():
            barrier.failed = True
            barrier.acknowledged.set()
        reserved_acquisitions = {
            invocation_id
            for invocation_id, reservation in self._workspace_reservations.items()
            if reservation.acquiring
        }
        acquisition_tasks = [
            task
            for invocation_id, task in self._acquire_tasks.items()
            if invocation_id not in reserved_acquisitions
        ]
        workspace_tasks = list(self._workspace_tasks.values())
        operations: list[asyncio.Future[Any] | asyncio.Task[Any]] = []
        reservation_operations = [
            asyncio.create_task(self._cancel_workspace_reservation(invocation_id, reservation))
            for invocation_id, reservation in tuple(self._workspace_reservations.items())
        ]
        for task in acquisition_tasks:
            if task is not asyncio.current_task():
                task.cancel()
        for inv in list(self._invocations.values()):
            operations.append(self._start_cleanup(inv, release=False))

        async def cleanup_all() -> None:
            if acquisition_tasks:
                await asyncio.gather(*acquisition_tasks, return_exceptions=True)
            if workspace_tasks:
                await asyncio.gather(*workspace_tasks, return_exceptions=True)
            if reservation_operations:
                await asyncio.gather(*reservation_operations, return_exceptions=False)
            if operations:
                await asyncio.gather(*operations, return_exceptions=False)
            await self.pool.stop_all()

        cleanup_task = asyncio.create_task(cleanup_all())
        self._controller_cleanup_task = cleanup_task
        cleanup_budget = max(
            [
                reservation.request.cleanup_budget_seconds
                for reservation in self._workspace_reservations.values()
            ]
            + [inv.workspace_cleanup_timeout_seconds for inv in self._invocations.values()]
            + [0.0]
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup_task),
                timeout=CLEANUP_DEADLINE_SECONDS
                + cleanup_budget
                + CLEANUP_PROCESS_MARGIN_SECONDS,
            )
        except BaseException as exc:
            self._mark_unhealthy()
            self.controller_cleanup_error = repr(exc)
            cleanup_task.add_done_callback(self._observe_task)
            raise
        finally:
            if cleanup_task.done() and self._controller_cleanup_task is cleanup_task:
                self._controller_cleanup_task = None
        self._controller_id = None
        self._workspace_tasks.clear()
        self._workspace_reservations.clear()
        self._workspace_cancelled.clear()
        self._controller_events = None
        self._workspace_barriers.clear()

    def controller_events(self) -> asyncio.Queue[WorkspaceStoppedEvent] | None:
        """Return the finite event queue held by the current controller stream."""
        return self._controller_events

    def _emit_controller_event(self, event: WorkspaceStoppedEvent) -> None:
        queue = self._controller_events
        if queue is None:
            return
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull as exc:
            self._mark_unhealthy()
            raise RuntimeError("controller event buffer is full") from exc

    async def ack_workspace_cleanup(self, request: WorkspaceCleanupAckRequest) -> None:
        """Release one callback only after its correlated private cleanup completed."""
        self._assert_controller(request.controller_id)
        barrier = self._workspace_barriers.get(request.invocation_id)
        if barrier is None:
            raise ValueError("unknown workspace cleanup event")
        event = barrier.event
        if (
            request.instance_id != event.instance_id
            or request.session_key != event.session_key
            or request.event_id != event.event_id
            or request.controller_id != event.controller_id
        ):
            raise ValueError("workspace cleanup acknowledgement mismatch")
        barrier.acknowledged.set()

    def _record_workspace_failure(
        self, *, phase: str, invocation_id: str, error: BaseException
    ) -> None:
        detail = f"{phase} {invocation_id}: {type(error).__name__}: {error}"
        if len(self.workspace_cleanup_errors) >= 64:
            del self.workspace_cleanup_errors[: len(self.workspace_cleanup_errors) - 63]
        self.workspace_cleanup_errors.append(detail)
        log.warning(
            "workspace hook failed", phase=phase, invocation_id=invocation_id, error=str(error)
        )

    def _track_workspace_task(self, invocation_id: str) -> asyncio.Task[Any]:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("workspace operation requires an asyncio task")
        if invocation_id in self._workspace_tasks:
            raise ValueError(f"workspace operation already active: {invocation_id}")
        self._workspace_tasks[invocation_id] = current
        return current

    def _workspace_reservation_for(self, invocation_id: str) -> _WorkspaceReservation:
        reservation = self._workspace_reservations.get(invocation_id)
        if reservation is None:
            raise ValueError("workspace reservation is required")
        return reservation

    async def _cancel_workspace_reservation(
        self, invocation_id: str, reservation: _WorkspaceReservation
    ) -> None:
        if reservation.cleanup_task is None:
            reservation.cancel_requested = True
            current = asyncio.current_task()
            reservation.cleanup_owner = current
            if reservation.acquiring and current is self._acquire_tasks.get(invocation_id):
                reservation.skip_operation = current
            reservation.cleanup_task = asyncio.create_task(
                self._finish_workspace_reservation(invocation_id, reservation)
            )
        task = reservation.cleanup_task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            task.add_done_callback(self._observe_task)
            raise

    async def _finish_workspace_reservation(
        self, invocation_id: str, reservation: _WorkspaceReservation
    ) -> None:
        native_deadline = asyncio.get_running_loop().time() + CLEANUP_DEADLINE_SECONDS
        deadline = native_deadline
        deadline += reservation.request.cleanup_budget_seconds + CLEANUP_PROCESS_MARGIN_SECONDS
        operation = self._workspace_tasks.get(invocation_id)
        if operation is None:
            operation = self._acquire_tasks.get(invocation_id)
        operation_error: BaseException | None = None
        if operation is not None and operation not in (
            asyncio.current_task(),
            reservation.skip_operation,
        ):
            operation.cancel()
            try:
                remaining = max(0.001, native_deadline - asyncio.get_running_loop().time())
                await asyncio.wait_for(asyncio.shield(operation), remaining)
            except asyncio.CancelledError:
                pass
            except TimeoutError as exc:
                operation_error = exc
                operation.add_done_callback(self._observe_task)
                self._mark_unhealthy()
            except BaseException as exc:
                operation_error = exc
                if isinstance(exc, OwnedProcessCleanupError):
                    self._mark_unhealthy()
        try:
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            discard_task = asyncio.create_task(
                self.pool.discard(
                    reservation.request.session_key,
                    native_timeout_seconds=max(
                        0.001, native_deadline - asyncio.get_running_loop().time()
                    ),
                )
            )
            try:
                await asyncio.wait_for(asyncio.shield(discard_task), remaining)
                if self._unhealthy and operation_error is None:
                    operation_error = RuntimeError("workspace cleanup failed; service is unhealthy")
            except TimeoutError as exc:
                self._mark_unhealthy()
                discard_task.add_done_callback(self._observe_task)
                if operation_error is None:
                    operation_error = exc
            except BaseException as exc:
                self._mark_unhealthy()
                if operation_error is None:
                    operation_error = exc
        finally:
            self._workspace_reservations.pop(invocation_id, None)
            self._workspace_cancelled.add(invocation_id)
            if len(self._workspace_cancelled) > 64:
                self._workspace_cancelled.pop()
        if operation_error is not None:
            raise operation_error

    async def prepare_workspace(self, request: WorkspacePrepareRequest) -> dict[str, str]:
        """Run the public hook and register its latest cleanup before native acquire."""
        self._assert_controller(request.controller_id)
        self._workspace_cancelled.discard(request.invocation_id)
        if request.invocation_id in self._workspace_reservations:
            raise ValueError("workspace reservation already exists")
        if request.invocation_id in self._invocations or request.invocation_id in self._acquiring:
            raise ValueError("invocation is already active")
        if (
            any(
                item.request.session_key == request.session_key
                for item in self._workspace_reservations.values()
            )
            or any(inv.session_key == request.session_key for inv in self._invocations.values())
            or (request.session_key in self._acquiring_lanes.values())
        ):
            raise ValueError("workspace lane already has a pending reservation")
        self._track_workspace_task(request.invocation_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.remaining_seconds
        workspace = workspace_dir(request.work_dir, request.session_key)
        reservation = _WorkspaceReservation(request, workspace, deadline)
        self._workspace_reservations[request.invocation_id] = reservation
        completed = False

        async def cleanup() -> None:
            if request.cleanup is not None:
                remaining = request.cleanup.timeout_seconds
                env = build_public_env(
                    request.cleanup,
                    session_key=request.session_key,
                    event_id=request.event_id,
                    channel_name=request.channel_name,
                    delivery_context=request.delivery_context,
                    workspace=workspace,
                )
                try:
                    await run_public_hook(
                        request.cleanup,
                        cwd=workspace.parent,
                        env=env,
                        remaining_seconds=remaining,
                    )
                except OwnedProcessCleanupError:
                    self._mark_unhealthy()
                    raise
                except WorkspaceHookFailed as exc:
                    self._record_workspace_failure(
                        phase="cleanup", invocation_id=request.invocation_id, error=exc
                    )
                except BaseException:
                    self._mark_unhealthy()
                    raise
            if request.notify_on_stop:
                event = WorkspaceStoppedEvent(
                    controller_id=request.controller_id,
                    instance_id=self.instance_id,
                    session_key=request.session_key,
                    event_id=request.event_id,
                    invocation_id=request.invocation_id,
                    workspace=str(workspace),
                )
                barrier = _WorkspaceCleanupBarrier(event)
                if request.cleanup_ack_required:
                    if not self._admission_open or self._controller_id != request.controller_id:
                        barrier.failed = True
                        raise RuntimeError("workspace cleanup controller is unavailable")
                    self._workspace_barriers[request.invocation_id] = barrier
                try:
                    self._emit_controller_event(event)
                    if request.cleanup_ack_required:
                        ack_wait = asyncio.create_task(barrier.acknowledged.wait())
                        try:
                            await asyncio.wait_for(
                                ack_wait,
                                request.cleanup_timeout_seconds + CLEANUP_PROCESS_MARGIN_SECONDS,
                            )
                        except TimeoutError as exc:
                            barrier.failed = True
                            raise RuntimeError(
                                "workspace cleanup acknowledgement timed out"
                            ) from exc
                        finally:
                            if not ack_wait.done():
                                ack_wait.cancel()
                            await asyncio.gather(ack_wait, return_exceptions=True)
                        if barrier.failed:
                            raise RuntimeError("workspace cleanup acknowledgement unavailable")
                finally:
                    if request.cleanup_ack_required:
                        self._workspace_barriers.pop(request.invocation_id, None)

        try:
            await self.pool.begin_session(
                request.session_key,
                cleanup if (request.cleanup or request.notify_on_stop) else None,
            )
            workspace = prepare_workspace(request.home, request.work_dir, request.session_key)
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("workspace preparation deadline expired")
            if request.prepare is not None:
                env = build_public_env(
                    request.prepare,
                    session_key=request.session_key,
                    event_id=request.event_id,
                    channel_name=request.channel_name,
                    delivery_context=request.delivery_context,
                    workspace=workspace,
                )
                await run_public_hook(
                    request.prepare,
                    cwd=workspace,
                    env=env,
                    remaining_seconds=remaining,
                )
            reservation.workspace = workspace
            completed = True
            return {"status": "ok", "workspace": str(workspace)}
        except asyncio.CancelledError:
            if reservation.cleanup_task is not None:
                raise
            reservation.cancel_requested = True
            reservation.skip_operation = asyncio.current_task()
            if reservation.cleanup_task is None:
                reservation.cleanup_task = asyncio.create_task(
                    self._finish_workspace_reservation(request.invocation_id, reservation)
                )
            try:
                await asyncio.shield(reservation.cleanup_task)
            except asyncio.CancelledError:
                reservation.cleanup_task.add_done_callback(self._observe_task)
            raise
        except OwnedProcessCleanupError:
            self._mark_unhealthy()
            if not reservation.cancel_requested:
                try:
                    await asyncio.shield(
                        self.pool.discard(
                            request.session_key,
                            native_timeout_seconds=CLEANUP_DEADLINE_SECONDS,
                        )
                    )
                except BaseException:
                    self._mark_unhealthy()
            raise
        except BaseException:
            if not reservation.cancel_requested:
                try:
                    await asyncio.shield(
                        self.pool.discard(
                            request.session_key,
                            native_timeout_seconds=CLEANUP_DEADLINE_SECONDS,
                        )
                    )
                except BaseException:
                    self._mark_unhealthy()
            raise
        finally:
            self._workspace_tasks.pop(request.invocation_id, None)
            if not completed and not reservation.cancel_requested:
                self._workspace_reservations.pop(request.invocation_id, None)

    async def handoff_workspace(self, request: WorkspaceHandoffRequest) -> dict[str, str]:
        """Import a credential-free bundle artifact into the public session workspace."""
        self._assert_controller(request.controller_id)
        reservation = self._workspace_reservation_for(request.invocation_id)
        if reservation.request.controller_id != request.controller_id:
            raise ValueError("workspace reservation controller mismatch")
        if reservation.request.session_key != request.session_key:
            raise ValueError("workspace reservation lane mismatch")
        if reservation.acquiring or request.invocation_id in self._acquire_tasks:
            raise ValueError("workspace reservation is acquiring")
        if (
            reservation.request.home != request.home
            or reservation.request.work_dir != request.work_dir
        ):
            raise ValueError("workspace reservation path mismatch")
        self._track_workspace_task(request.invocation_id)
        try:
            remaining = min(
                request.remaining_seconds,
                reservation.deadline - asyncio.get_running_loop().time(),
            )
            if remaining <= 0:
                raise TimeoutError("workspace reservation deadline expired")
            workspace = await handoff_bundle(
                home=request.home,
                work_dir=request.work_dir,
                session_key=request.session_key,
                bundle_path=request.bundle_path,
                head=request.head,
                origin=request.origin,
                remaining_seconds=remaining,
            )
            if workspace != reservation.workspace:
                raise WorkspaceHandoffFailed("workspace handoff changed the reserved workspace")
            return {"status": "ok", "workspace": str(workspace)}
        except OwnedProcessCleanupError:
            self._mark_unhealthy()
            raise
        finally:
            self._workspace_tasks.pop(request.invocation_id, None)

    async def acquire(self, request: AcquireRequest) -> ExecutionHandle:
        self._assert_controller(request.controller_id)
        if self._unhealthy:
            raise RuntimeError("native cleanup failed; execution service is unhealthy")
        if request.invocation_id in self._invocations or request.invocation_id in self._acquiring:
            raise ValueError(f"invocation already acquired: {request.invocation_id}")
        reservation = self._workspace_reservations.get(request.invocation_id)
        if reservation is not None:
            if reservation.request.session_key != request.lane_key:
                raise ValueError("workspace reservation lane mismatch")
            if request.invocation_id in self._workspace_tasks:
                raise ValueError("workspace operation is still active")
            if reservation.acquiring:
                raise ValueError("workspace reservation is already acquiring")
            reservation.acquiring = True
        elif any(
            item.request.session_key == request.lane_key
            for item in self._workspace_reservations.values()
        ):
            raise ValueError("workspace lane has a pending reservation")
        cfg = _engine_config(request.config)
        self._acquiring.add(request.invocation_id)
        self._acquiring_lanes[request.invocation_id] = request.lane_key
        current_task = asyncio.current_task()
        if current_task is not None:
            self._acquire_tasks[request.invocation_id] = current_task
        try:
            loop = asyncio.get_running_loop()
            remaining = request.remaining_seconds
            if reservation is not None:
                remaining = min(remaining, reservation.deadline - loop.time())
                if remaining <= 0:
                    raise TimeoutError("workspace reservation deadline expired")
            deadline = loop.time() + remaining
            remaining = max(0.001, deadline - loop.time())
            server = await asyncio.wait_for(self.pool.acquire(request.lane_key, cfg), remaining)
        except asyncio.CancelledError:
            if reservation is not None and reservation.cleanup_task is not None:
                raise
            if reservation is not None:
                await self._cancel_workspace_reservation(request.invocation_id, reservation)
            raise
        except NativeLaunchFailed:
            # Driver launch owns process cleanup. The typed error is retained so an HTTP
            # adapter can serialize LaunchFailed without treating it as controller death.
            if reservation is not None and reservation.cleanup_task is not None:
                raise
            if reservation is not None:
                await self._cancel_workspace_reservation(request.invocation_id, reservation)
            raise
        except Exception:
            if reservation is not None and reservation.cleanup_task is not None:
                raise
            if reservation is not None:
                await self._cancel_workspace_reservation(request.invocation_id, reservation)
            self._mark_unhealthy()
            raise
        except BaseException:
            if reservation is not None and reservation.cleanup_task is not None:
                raise
            if reservation is not None:
                await self._cancel_workspace_reservation(request.invocation_id, reservation)
            raise
        finally:
            self._acquiring.discard(request.invocation_id)
            self._acquiring_lanes.pop(request.invocation_id, None)
            self._acquire_tasks.pop(request.invocation_id, None)
            if reservation is not None:
                reservation.acquiring = False
        try:
            self._assert_controller(request.controller_id)
        except Exception:
            try:
                await self.pool.discard(
                    request.lane_key,
                    native_timeout_seconds=CLEANUP_DEADLINE_SECONDS,
                )
            except Exception:
                self._mark_unhealthy()
            if reservation is not None:
                self._workspace_reservations.pop(request.invocation_id, None)
            raise
        handle = ExecutionHandle(
            instance_id=self.instance_id,
            controller_id=request.controller_id,
            execution_id=str(uuid.uuid4()),
            invocation_id=request.invocation_id,
            proxy_route=getattr(server, "proxy_route", "") or getattr(server, "proxy_token", ""),
        )
        inv = _Invocation(
            handle=handle,
            session_key=request.lane_key,
            conversation_key=request.conversation_key,
            reuse=request.reuse,
            server=server,
            deadline=deadline,
            workspace_cleanup_timeout_seconds=(
                reservation.request.cleanup_budget_seconds if reservation is not None else 0.0
            ),
        )
        self._invocations[request.invocation_id] = inv
        if reservation is not None:
            self._workspace_reservations.pop(request.invocation_id, None)
        inv.deadline_task = asyncio.create_task(self._deadline_watch(inv))
        return handle

    @staticmethod
    async def _cancel_and_join(
        task: asyncio.Task[Any] | None, *, timeout_seconds: float = CLEANUP_DEADLINE_SECONDS
    ) -> bool:
        """Cancel an owned task and consume its result, including cancellation."""
        if task is None or task is asyncio.current_task():
            return True
        if not task.done():
            task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(task), max(0.001, timeout_seconds))
            return True
        except TimeoutError:
            task.add_done_callback(ExecutionService._observe_task)
            return False
        except asyncio.CancelledError:
            if task.done():
                return True
            task.add_done_callback(ExecutionService._observe_task)
            raise
        except BaseException:
            return True

    def _start_cleanup(
        self, inv: _Invocation, *, release: bool, idle_ttl_seconds: float = 0.0
    ) -> asyncio.Task[None]:
        """Create the sole cleanup operation and reserve the invocation immediately."""
        if inv.cleanup_task is not None:
            return inv.cleanup_task
        inv.terminal = True
        if inv.cleanup_deadline is None:
            inv.cleanup_deadline = (
                asyncio.get_running_loop().time()
                + CLEANUP_DEADLINE_SECONDS
                + inv.workspace_cleanup_timeout_seconds
                + CLEANUP_PROCESS_MARGIN_SECONDS
            )
        inv.cleanup_task = asyncio.create_task(
            self._cleanup_invocation(inv, release=release, idle_ttl_seconds=idle_ttl_seconds)
        )
        return inv.cleanup_task

    def _reserve_output(self, inv: _Invocation, event: ExecutionEvent) -> int:
        size = len(_event_bytes(event))
        if size > MAX_NDJSON_RECORD_BYTES:
            raise OutputLimitExceeded("NDJSON record exceeds 1 MiB")
        if inv.buffered_events >= MAX_STREAM_EVENTS:
            raise OutputLimitExceeded("invocation stream exceeds 256 buffered events")
        if inv.buffered_bytes + size > MAX_INVOCATION_STREAM_BYTES:
            raise OutputLimitExceeded("invocation stream exceeds 4 MiB buffered output")
        if self._queued_stream_bytes + size > MAX_AGGREGATE_QUEUED_BYTES:
            raise OutputLimitExceeded("aggregate output queue exceeds 32 MiB")
        inv.buffered_events += 1
        inv.buffered_bytes += size
        self._queued_stream_bytes += size
        return size

    def _release_output(self, inv: _Invocation, size: int) -> None:
        inv.buffered_events = max(0, inv.buffered_events - 1)
        inv.buffered_bytes = max(0, inv.buffered_bytes - size)
        self._queued_stream_bytes = max(0, self._queued_stream_bytes - size)

    async def _cleanup_invocation(
        self, inv: _Invocation, *, release: bool, idle_ttl_seconds: float
    ) -> None:
        """Join invocation work, then release or discard its pool reference once."""
        native_deadline = asyncio.get_running_loop().time() + CLEANUP_DEADLINE_SECONDS
        native_uncertain = False
        current = asyncio.current_task()
        deadline_task = inv.deadline_task
        if deadline_task is not current:
            joined = await self._cancel_and_join(deadline_task)
            native_uncertain = native_uncertain or not joined
        inv.deadline_task = None

        for attr in ("maintenance_task", "task", "wake_task"):
            task = getattr(inv, attr)
            if attr == "wake_task" and inv.turn_active and task is not None:
                # Let the wake task publish its sentinel after the run task has
                # been cancelled so a consumer waiting in __anext__ observes
                # the run's CancelledError.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        max(0.001, native_deadline - asyncio.get_running_loop().time()),
                    )
                except TimeoutError:
                    task.add_done_callback(self._observe_task)
                    native_uncertain = True
                except BaseException:
                    pass
            elif task is not current:
                joined = await self._cancel_and_join(
                    task,
                    timeout_seconds=max(0.001, native_deadline - asyncio.get_running_loop().time()),
                )
                native_uncertain = native_uncertain or not joined
            if attr == "maintenance_task" and getattr(inv, attr) is task:
                setattr(inv, attr, None)

        queue = inv.event_queue
        if queue is not None:
            while True:
                try:
                    queued = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if queued is not None:
                    _event, size = queued
                    inv.queued_bytes = max(0, inv.queued_bytes - size)
                    self._release_output(inv, size)
            inv.queued_bytes = 0
            # The consumer may already be blocked in ``queue.get`` after its
            # run task was cancelled; preserve the wake-up sentinel after the
            # discarded buffered records.
            queue.put_nowait(None)
            inv.event_queue = None
        if inv.leased_bytes:
            self._release_output(inv, inv.leased_bytes)
            inv.leased_bytes = 0

        if native_uncertain:
            self._mark_unhealthy()
            inv.cleanup_error = "native cleanup uncertain"
            release = False
            idle_ttl_seconds = 0.0
        native_remaining = max(0.001, native_deadline - asyncio.get_running_loop().time())

        try:
            if release:
                await self.pool.release(
                    inv.session_key,
                    idle_ttl_seconds,
                    native_timeout_seconds=native_remaining,
                )
                inv.released = True
                ttl_task = getattr(self.pool, "_ttl_tasks", {}).get(inv.session_key)
                if ttl_task is not None:
                    watcher = asyncio.create_task(self._watch_pool_cleanup(ttl_task))
                    self._ttl_watchers.add(watcher)
                    watcher.add_done_callback(self._ttl_watchers.discard)
            else:
                await self.pool.discard(
                    inv.session_key,
                    native_timeout_seconds=native_remaining,
                    run_cleanup=not native_uncertain,
                )
        except asyncio.CancelledError:
            self._mark_unhealthy()
            inv.cleanup_error = "native cleanup cancelled"
            raise
        except Exception as exc:
            self._mark_unhealthy()
            inv.cleanup_error = str(exc)
            raise
        else:
            if inv.cleanup_error is not None:
                raise RuntimeError(inv.cleanup_error)
            if native_uncertain:
                raise RuntimeError(inv.cleanup_error or "native cleanup uncertain")
            if self._invocations.get(inv.handle.invocation_id) is inv:
                self._invocations.pop(inv.handle.invocation_id, None)

    async def _watch_pool_cleanup(self, task: asyncio.Task[Any]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            return
        except BaseException:
            self._mark_unhealthy()

    async def _await_cleanup(self, task: asyncio.Task[None], inv: _Invocation) -> None:
        """Wait for the one cleanup deadline despite caller cancellation."""
        deadline = inv.cleanup_deadline
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + CLEANUP_DEADLINE_SECONDS
            deadline += inv.workspace_cleanup_timeout_seconds
            deadline += CLEANUP_PROCESS_MARGIN_SECONDS
            inv.cleanup_deadline = deadline

        async def wait_remaining() -> None:
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(asyncio.shield(task), timeout=remaining)

        try:
            await wait_remaining()
        except TimeoutError:
            self._mark_unhealthy()
            task.add_done_callback(self._observe_task)
            raise RuntimeError("native cleanup deadline expired")
        except asyncio.CancelledError:
            try:
                await wait_remaining()
            except TimeoutError:
                self._mark_unhealthy()
                task.add_done_callback(self._observe_task)
            except BaseException:
                pass
            raise

    async def _deadline_watch(self, inv: _Invocation) -> None:
        try:
            await asyncio.sleep(max(0.0, inv.deadline - asyncio.get_running_loop().time()))
        except asyncio.CancelledError:
            return
        if inv.terminal or inv.released:
            return
        # Once this watcher starts cleanup it owns the cleanup deadline wait.
        # Clear the invocation deadline handle first so cleanup does not cancel
        # the watcher that is enforcing its own ten second bound.
        inv.deadline_task = None
        cleanup = self._start_cleanup(inv, release=False)
        try:
            deadline = inv.cleanup_deadline or asyncio.get_running_loop().time()
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=remaining)
        except TimeoutError:
            self._mark_unhealthy()
            cleanup.add_done_callback(self._observe_task)
        except BaseException:
            # _cleanup_invocation records the failure and leaves the invocation tracked.
            pass

    def _get(self, controller_id: str, execution_id: str, invocation_id: str) -> _Invocation:
        inv = self._invocations.get(invocation_id)
        if (
            inv is None
            or inv.handle.controller_id != controller_id
            or inv.handle.execution_id != execution_id
        ):
            raise ValueError("unknown execution")
        return inv

    def validate_turn(self, request: TurnRequest) -> None:
        """Validate an HTTP turn before its streaming response is committed."""
        self._assert_controller(request.controller_id)
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.terminal or inv.released:
            raise ValueError("invocation is terminal")
        if request.turn_id in inv.turn_ids:
            raise ValueError("duplicate turn id")
        if inv.turn_active or inv.maintenance_active:
            message = "maintenance active" if inv.maintenance_active else "turn already running"
            raise ValueError(message)

    async def session_ready(self, request: SessionReadyRequest) -> None:
        """Release the pending native send after the harness records its correlation."""
        self._assert_controller(request.controller_id)
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        pending = inv.session_ready
        if pending is None or pending[0] != request.turn_id:
            raise ValueError("no pending session acknowledgement")
        pending[2].set()

    async def turn(self, request: TurnRequest) -> AsyncIterator[ExecutionEvent]:
        self._assert_controller(request.controller_id)
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.terminal or inv.released:
            raise ValueError("invocation is terminal")
        if inv.turn_active:
            raise ValueError("turn already running")
        try:
            async for event in self._turn_impl(request):
                yield event
        finally:
            wake = inv.wake_task
            if wake is not None:
                joined = await self._cancel_and_join(wake)
                if not joined:
                    self._mark_unhealthy()
                    inv.cleanup_error = "invocation wake task did not stop"
                if inv.wake_task is wake:
                    inv.wake_task = None
            if inv.turn_active and inv.cleanup_task is None:
                inv.turn_active = False
                cleanup = self._start_cleanup(inv, release=False)
                try:
                    await self._await_cleanup(cleanup, inv)
                except Exception:
                    pass

    async def _turn_impl(self, request: TurnRequest) -> AsyncIterator[ExecutionEvent]:
        self._assert_controller(request.controller_id)
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.terminal or inv.released:
            raise ValueError("invocation is terminal")
        if request.turn_id in inv.turn_ids:
            raise ValueError("duplicate turn id")
        if inv.turn_active or inv.maintenance_active:
            raise ValueError(
                "maintenance active" if inv.maintenance_active else "turn already running"
            )
        inv.turn_active = True
        inv.turn_ids.add(request.turn_id)
        if inv.current_ref is None:
            stats: dict[str, Any] = {}
            remaining = inv.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("invocation deadline expired")

            async def resolve() -> str:
                return await self.driver.resolve_session(
                    inv.server,
                    conv_key=inv.conversation_key,
                    reuse=inv.reuse,
                    sessions=self.pool.sessions,
                    stats=stats,
                )

            inv.task = asyncio.create_task(resolve())
            try:
                inv.current_ref = await asyncio.wait_for(asyncio.shield(inv.task), remaining)
            except BaseException:
                raise
            inv.cached_ref_pending = bool(stats.get("_cached_session", False))
            session_event = ExecutionEvent(
                kind="session_resolved",
                execution_id=inv.handle.execution_id,
                invocation_id=inv.handle.invocation_id,
                turn_id=request.turn_id,
                payload={"session_ref": inv.current_ref},
            )
            size = self._reserve_output(inv, session_event)
            inv.leased_bytes = size
            ready = asyncio.Event()
            if self.controller_required:
                current_ref = inv.current_ref
                if current_ref is None:
                    raise RuntimeError("session resolution returned no native reference")
                inv.session_ready = (request.turn_id, current_ref, ready)
            yield session_event
            if inv.leased_bytes:
                self._release_output(inv, inv.leased_bytes)
                inv.leased_bytes = 0
            if inv.terminal or inv.released:
                raise ValueError("invocation is terminal")
            if self.controller_required:
                try:
                    await ready.wait()
                finally:
                    if inv.session_ready is not None and inv.session_ready[2] is ready:
                        inv.session_ready = None

        queue: asyncio.Queue[tuple[ExecutionEvent, int] | None] = asyncio.Queue()
        inv.event_queue = queue

        def enqueue(event: ExecutionEvent) -> None:
            if inv.output_error is not None:
                return
            try:
                size = self._reserve_output(inv, event)
            except OutputLimitExceeded as exc:
                inv.output_error = exc
                if inv.task is not None and not inv.task.done():
                    inv.task.cancel()
                queue.put_nowait(None)
                return
            inv.queued_bytes += size
            queue.put_nowait((event, size))

        stats = {"_cached_session": inv.cached_ref_pending}

        async def on_session_resolved(session_ref: str) -> None:
            inv.current_ref = session_ref
            ready = asyncio.Event()
            if self.controller_required:
                inv.session_ready = (request.turn_id, session_ref, ready)
            enqueue(
                ExecutionEvent(
                    kind="session_resolved",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload={"session_ref": session_ref},
                )
            )
            if self.controller_required:
                try:
                    await ready.wait()
                finally:
                    if inv.session_ready is not None and inv.session_ready[2] is ready:
                        inv.session_ready = None

        def on_text(text: str) -> None:
            enqueue(
                ExecutionEvent(
                    kind="text",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload=text,
                )
            )

        def on_tool(tool: Any) -> None:
            enqueue(
                ExecutionEvent(
                    kind="tool",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload=_json_value(tool),
                )
            )

        async def run() -> TurnResult:
            remaining = inv.deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("invocation deadline expired")
            async with asyncio.timeout(remaining):
                return await self.driver.run_turn(
                    inv.server,
                    conv_key=inv.conversation_key,
                    prompt=request.prompt,
                    reuse=inv.reuse,
                    sessions=self.pool.sessions,
                    session_ref=inv.current_ref,
                    on_text=on_text,
                    on_tool=on_tool,
                    max_tool_calls=request.max_tool_calls,
                    stats=stats,
                    on_session_resolved=on_session_resolved,
                )

        inv.task = asyncio.create_task(run())

        async def wake() -> None:
            try:
                task = inv.task
                assert task is not None
                await task
            except BaseException:
                pass
            finally:
                if inv.output_error is None:
                    queue.put_nowait(None)

        inv.wake_task = asyncio.create_task(wake())
        while True:
            if inv.leased_bytes:
                self._release_output(inv, inv.leased_bytes)
                inv.leased_bytes = 0
            event = await queue.get()
            if event is None:
                if inv.output_error is not None:
                    raise inv.output_error
                break
            output, size = event
            inv.queued_bytes = max(0, inv.queued_bytes - size)
            inv.leased_bytes = size
            yield output
            if inv.leased_bytes:
                self._release_output(inv, inv.leased_bytes)
                inv.leased_bytes = 0
        if inv.wake_task is not None:
            await inv.wake_task
        try:
            result = inv.task.result()
            inv.current_ref = result.session_ref
            inv.cached_ref_pending = False
            stats.pop("on_session_resolved", None)
            if stats.get("usage") is not None:
                usage_event = ExecutionEvent(
                    kind="usage",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload=_json_value(stats["usage"]),
                )
                size = self._reserve_output(inv, usage_event)
                inv.leased_bytes = size
                yield usage_event
                if inv.leased_bytes:
                    self._release_output(inv, inv.leased_bytes)
                    inv.leased_bytes = 0
            done_event = ExecutionEvent(
                kind="turn_done",
                execution_id=inv.handle.execution_id,
                invocation_id=inv.handle.invocation_id,
                turn_id=request.turn_id,
                payload={
                    "session_ref": result.session_ref,
                    "text": result.text,
                    "aborted": result.aborted,
                    "stats": _json_value(stats),
                },
            )
            size = self._reserve_output(inv, done_event)
            inv.leased_bytes = size
            yield done_event
            if inv.leased_bytes:
                self._release_output(inv, inv.leased_bytes)
                inv.leased_bytes = 0
        except OutputLimitExceeded as exc:
            inv.output_error = exc
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed turn is a serializable engine result
            error_event = ExecutionEvent(
                kind="error",
                execution_id=inv.handle.execution_id,
                invocation_id=inv.handle.invocation_id,
                turn_id=request.turn_id,
                payload={"type": type(exc).__name__, "message": str(exc)},
            )
            size = self._reserve_output(inv, error_event)
            inv.leased_bytes = size
            yield error_event
            if inv.leased_bytes:
                self._release_output(inv, inv.leased_bytes)
                inv.leased_bytes = 0
        inv.turn_active = False

    async def session_op(self, request: SessionOperation) -> None:
        self._assert_controller(request.controller_id)
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.terminal or inv.released or inv.turn_active or inv.maintenance_active:
            raise ValueError("invocation is not available for maintenance")
        inv.maintenance_active = True

        async def operate() -> None:
            if inv.current_ref is None:
                return
            if asyncio.get_running_loop().time() >= inv.deadline:
                raise TimeoutError("invocation deadline expired")
            async with asyncio.timeout(
                max(0.001, inv.deadline - asyncio.get_running_loop().time())
            ):
                if request.operation == "discard":
                    await self.driver.discard_session(inv.server, inv.current_ref)
                elif request.operation == "compact":
                    await self.driver.compact_session(inv.server, inv.current_ref)
                else:
                    self.pool.sessions.pop(inv.conversation_key, None)
                    inv.current_ref = None

        inv.maintenance_task = asyncio.create_task(operate())
        try:
            await asyncio.shield(inv.maintenance_task)
        finally:
            maintenance_task = inv.maintenance_task
            if maintenance_task is not None and maintenance_task is not asyncio.current_task():
                joined = await self._cancel_and_join(maintenance_task)
                if not joined:
                    self._mark_unhealthy()
                    inv.cleanup_error = "maintenance task did not stop"
                    raise RuntimeError(inv.cleanup_error)
            inv.maintenance_active = False
            if inv.maintenance_task is maintenance_task:
                inv.maintenance_task = None

    async def release(self, request: ReleaseRequest) -> None:
        self._assert_controller(request.controller_id)
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.released:
            return
        if inv.terminal and inv.cleanup_task is not None:
            await self._await_cleanup(inv.cleanup_task, inv)
            if inv.cleanup_error is not None:
                raise RuntimeError(inv.cleanup_error)
            return
        if (
            inv.terminal
            or inv.turn_active
            or inv.maintenance_active
            or (inv.task is not None and not inv.task.done())
        ):
            raise ValueError("cannot release an active invocation")
        cleanup = self._start_cleanup(inv, release=True, idle_ttl_seconds=request.idle_ttl_seconds)
        await self._await_cleanup(cleanup, inv)
        if inv.cleanup_error is not None:
            raise RuntimeError(inv.cleanup_error)

    async def cancel(self, controller_id: str, invocation_id: str) -> None:
        self._assert_controller(controller_id)
        reservation = self._workspace_reservations.get(invocation_id)
        if reservation is not None and invocation_id not in self._invocations:
            await self._cancel_workspace_reservation(invocation_id, reservation)
            return
        if invocation_id in self._workspace_cancelled:
            return
        inv = self._invocations.get(invocation_id)
        if inv is None or inv.handle.controller_id != controller_id:
            raise ValueError("unknown invocation")
        if inv.terminal:
            if inv.cleanup_task is not None:
                await self._await_cleanup(inv.cleanup_task, inv)
                if inv.cleanup_error is not None:
                    raise RuntimeError(inv.cleanup_error)
                return
            raise ValueError("invocation is terminal")
        cleanup = self._start_cleanup(inv, release=False)
        await self._await_cleanup(cleanup, inv)
        if inv.cleanup_error is not None:
            raise RuntimeError(inv.cleanup_error)

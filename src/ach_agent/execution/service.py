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
from typing import Any

from ach_agent.engine.base.driver import EngineConfig, EngineDriver, TurnResult
from ach_agent.engine.base.pool import EnginePool
from ach_agent.engine.lifecycle import NativeLaunchFailed
from ach_agent.engine.mcp_passthrough import to_engine_entry
from ach_agent.execution.wire import (
    AcquireRequest,
    ExecutionEvent,
    ExecutionHandle,
    ReleaseRequest,
    SessionOperation,
    TurnRequest,
)


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
    stream_events: int = 0
    stream_bytes: int = 0
    queued_bytes: int = 0
    output_error: Exception | None = None
    event_queue: asyncio.Queue[Any] | None = None


class OutputLimitExceeded(RuntimeError):
    """The client stream exceeded one of the bounded NDJSON output limits."""


MAX_NDJSON_RECORD_BYTES = 1 * 1024 * 1024
MAX_STREAM_EVENTS = 256
MAX_INVOCATION_STREAM_BYTES = 4 * 1024 * 1024
MAX_AGGREGATE_QUEUED_BYTES = 32 * 1024 * 1024


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

    def _assert_controller(self, controller_id: str) -> None:
        if not self.controller_required and self._controller_id is None:
            return
        if not self._admission_open or self._controller_id != controller_id:
            raise ValueError("obsolete controller")

    async def release_controller(self, controller_id: str) -> None:
        """Close admission and finish all owned cleanup before another controller."""
        if self._controller_id != controller_id:
            return
        self._admission_open = False
        acquisition_tasks = list(self._acquire_tasks.values())
        operations: list[asyncio.Future[Any] | asyncio.Task[Any]] = []
        for task in acquisition_tasks:
            if task is not asyncio.current_task():
                task.cancel()
        for inv in list(self._invocations.values()):
            operations.append(self._start_cleanup(inv, release=False))

        async def cleanup_all() -> None:
            if acquisition_tasks:
                await asyncio.gather(*acquisition_tasks, return_exceptions=True)
            if operations:
                await asyncio.gather(*operations, return_exceptions=False)
            await self.pool.stop_all()

        try:
            await asyncio.wait_for(asyncio.shield(cleanup_all()), timeout=10.0)
        except BaseException as exc:
            self._unhealthy = True
            self.shutdown_requested = True
            self.controller_cleanup_error = repr(exc)
            raise
        self._controller_id = None

    async def acquire(self, request: AcquireRequest) -> ExecutionHandle:
        self._assert_controller(request.controller_id)
        if self._unhealthy:
            raise RuntimeError("native cleanup failed; execution service is unhealthy")
        if request.invocation_id in self._invocations or request.invocation_id in self._acquiring:
            raise ValueError(f"invocation already acquired: {request.invocation_id}")
        self._acquiring.add(request.invocation_id)
        current_task = asyncio.current_task()
        if current_task is not None:
            self._acquire_tasks[request.invocation_id] = current_task
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + request.remaining_seconds
            cfg = _engine_config(request.config)
            remaining = max(0.001, deadline - loop.time())
            server = await asyncio.wait_for(self.pool.acquire(request.lane_key, cfg), remaining)
        except NativeLaunchFailed:
            # Driver launch owns process cleanup. The typed error is retained so an HTTP
            # adapter can serialize LaunchFailed without treating it as controller death.
            raise
        except Exception:
            self._unhealthy = True
            raise
        finally:
            self._acquiring.discard(request.invocation_id)
            self._acquire_tasks.pop(request.invocation_id, None)
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
        )
        self._invocations[request.invocation_id] = inv
        inv.deadline_task = asyncio.create_task(self._deadline_watch(inv))
        return handle

    @staticmethod
    async def _cancel_and_join(task: asyncio.Task[Any] | None) -> None:
        """Cancel an owned task and consume its result, including cancellation."""
        if task is None or task is asyncio.current_task():
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except BaseException:
            pass

    def _start_cleanup(
        self, inv: _Invocation, *, release: bool, idle_ttl_seconds: float = 0.0
    ) -> asyncio.Task[None]:
        """Create the sole cleanup operation and reserve the invocation immediately."""
        if inv.cleanup_task is not None:
            return inv.cleanup_task
        inv.terminal = True
        inv.cleanup_task = asyncio.create_task(
            self._cleanup_invocation(
                inv, release=release, idle_ttl_seconds=idle_ttl_seconds
            )
        )
        return inv.cleanup_task

    async def _cleanup_invocation(
        self, inv: _Invocation, *, release: bool, idle_ttl_seconds: float
    ) -> None:
        """Join invocation work, then release or discard its pool reference once."""
        current = asyncio.current_task()
        deadline_task = inv.deadline_task
        if deadline_task is not current:
            await self._cancel_and_join(deadline_task)
        inv.deadline_task = None

        for attr in ("maintenance_task", "task", "wake_task"):
            task = getattr(inv, attr)
            if attr == "wake_task" and inv.turn_active and task is not None:
                # Let the wake task publish its sentinel after the run task has
                # been cancelled so a consumer waiting in __anext__ observes
                # the run's CancelledError.
                try:
                    await task
                except BaseException:
                    pass
            elif task is not current:
                await self._cancel_and_join(task)
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
                    self._queued_stream_bytes = max(0, self._queued_stream_bytes - size)
            inv.queued_bytes = 0
            # The consumer may already be blocked in ``queue.get`` after its
            # run task was cancelled; preserve the wake-up sentinel after the
            # discarded buffered records.
            queue.put_nowait(None)
            inv.event_queue = None

        try:
            if release:
                await self.pool.release(inv.session_key, idle_ttl_seconds)
                inv.released = True
                ttl_task = getattr(self.pool, "_ttl_tasks", {}).get(inv.session_key)
                if ttl_task is not None:
                    watcher = asyncio.create_task(self._watch_pool_cleanup(ttl_task))
                    self._ttl_watchers.add(watcher)
                    watcher.add_done_callback(self._ttl_watchers.discard)
            else:
                await self.pool.discard(inv.session_key)
        except asyncio.CancelledError:
            self._unhealthy = True
            self.shutdown_requested = True
            inv.cleanup_error = "native cleanup cancelled"
            raise
        except Exception as exc:
            self._unhealthy = True
            self.shutdown_requested = True
            inv.cleanup_error = str(exc)
            raise
        else:
            if self._invocations.get(inv.handle.invocation_id) is inv:
                self._invocations.pop(inv.handle.invocation_id, None)

    async def _watch_pool_cleanup(self, task: asyncio.Task[Any]) -> None:
        try:
            await task
        except asyncio.CancelledError:
            return
        except BaseException:
            self._unhealthy = True
            self.shutdown_requested = True

    async def _await_cleanup(self, task: asyncio.Task[None]) -> None:
        """Wait for cleanup despite caller cancellation, without false confirmation."""
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
        except TimeoutError:
            self._unhealthy = True
            self.shutdown_requested = True
            raise RuntimeError("native cleanup deadline expired")
        except asyncio.CancelledError:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
            except TimeoutError:
                self._unhealthy = True
                self.shutdown_requested = True
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
        cleanup = self._start_cleanup(inv, release=False)
        try:
            await asyncio.wait_for(asyncio.shield(cleanup), timeout=10.0)
        except TimeoutError:
            self._unhealthy = True
            self.shutdown_requested = True
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
                await self._cancel_and_join(wake)
                if inv.wake_task is wake:
                    inv.wake_task = None
            if inv.turn_active and inv.cleanup_task is None:
                inv.turn_active = False
                cleanup = self._start_cleanup(inv, release=False)
                try:
                    await self._await_cleanup(cleanup)
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
            # ``reserve`` is defined below for queued events; this direct event is
            # validated by the HTTP adapter before it is written.
            yield session_event
            if inv.terminal or inv.released:
                raise ValueError("invocation is terminal")

        queue: asyncio.Queue[tuple[ExecutionEvent, int] | None] = asyncio.Queue()
        inv.event_queue = queue

        def reserve(event: ExecutionEvent) -> int:
            size = len(_event_bytes(event))
            if size > MAX_NDJSON_RECORD_BYTES:
                raise OutputLimitExceeded("NDJSON record exceeds 1 MiB")
            if inv.stream_events >= MAX_STREAM_EVENTS:
                raise OutputLimitExceeded("invocation stream exceeds 256 events")
            if inv.stream_bytes + size > MAX_INVOCATION_STREAM_BYTES:
                raise OutputLimitExceeded("invocation stream exceeds 4 MiB")
            inv.stream_events += 1
            inv.stream_bytes += size
            return size

        def enqueue(event: ExecutionEvent) -> None:
            try:
                size = reserve(event)
            except OutputLimitExceeded as exc:
                inv.output_error = exc
                if inv.task is not None and not inv.task.done():
                    inv.task.cancel()
                queue.put_nowait(None)
                return
            if self._queued_stream_bytes + size > MAX_AGGREGATE_QUEUED_BYTES:
                inv.output_error = OutputLimitExceeded("aggregate output queue exceeds 32 MiB")
                if inv.task is not None and not inv.task.done():
                    inv.task.cancel()
                queue.put_nowait(None)
                return
            self._queued_stream_bytes += size
            inv.queued_bytes += size
            queue.put_nowait((event, size))
        stats = {"_cached_session": inv.cached_ref_pending}

        async def on_session_resolved(session_ref: str) -> None:
            inv.current_ref = session_ref
            enqueue(
                ExecutionEvent(
                    kind="session_resolved",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload={"session_ref": session_ref},
                )
            )

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
                queue.put_nowait(None)

        inv.wake_task = asyncio.create_task(wake())
        while True:
            event = await queue.get()
            if event is None:
                if inv.output_error is not None:
                    raise inv.output_error
                break
            output, size = event
            self._queued_stream_bytes = max(0, self._queued_stream_bytes - size)
            inv.queued_bytes = max(0, inv.queued_bytes - size)
            yield output
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
                reserve(usage_event)
                yield usage_event
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
            reserve(done_event)
            yield done_event
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a failed turn is a serializable engine result
            yield ExecutionEvent(
                kind="error",
                execution_id=inv.handle.execution_id,
                invocation_id=inv.handle.invocation_id,
                turn_id=request.turn_id,
                payload={"type": type(exc).__name__, "message": str(exc)},
            )
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
                await self._cancel_and_join(maintenance_task)
            inv.maintenance_active = False
            if inv.maintenance_task is maintenance_task:
                inv.maintenance_task = None

    async def release(self, request: ReleaseRequest) -> None:
        self._assert_controller(request.controller_id)
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.released:
            return
        if inv.terminal and inv.cleanup_task is not None:
            await self._await_cleanup(inv.cleanup_task)
            return
        if inv.terminal or inv.turn_active or inv.maintenance_active or (
            inv.task is not None and not inv.task.done()
        ):
            raise ValueError("cannot release an active invocation")
        cleanup = self._start_cleanup(
            inv, release=True, idle_ttl_seconds=request.idle_ttl_seconds
        )
        await self._await_cleanup(cleanup)

    async def cancel(self, controller_id: str, invocation_id: str) -> None:
        self._assert_controller(controller_id)
        inv = self._invocations.get(invocation_id)
        if inv is None or inv.handle.controller_id != controller_id:
            raise ValueError("unknown invocation")
        if inv.terminal:
            if inv.cleanup_task is not None:
                await self._await_cleanup(inv.cleanup_task)
                return
            raise ValueError("invocation is terminal")
        cleanup = self._start_cleanup(inv, release=False)
        await self._await_cleanup(cleanup)

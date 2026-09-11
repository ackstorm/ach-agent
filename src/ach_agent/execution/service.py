"""Native execution service used by the mini-harness.

This module is deliberately a thin supervisor: native protocol parsing and session
maintenance remain in the selected driver, while invocation identity, deadlines and
serializable events live here.
"""

from __future__ import annotations

import asyncio
import dataclasses
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


def _engine_config(public: Any) -> EngineConfig:
    """Copy the approved wire fields into the native driver's config type."""
    values = public.model_dump(exclude={"mcp_templates"})
    values["extra_mcp_servers"] = {
        name: to_engine_entry(spec) for name, spec in public.mcp_templates.items()
    }
    return EngineConfig(**values)


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)  # type: ignore[arg-type]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


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
        self.instance_id = str(uuid.uuid4())
        self._unhealthy = False

    async def acquire(self, request: AcquireRequest) -> ExecutionHandle:
        if self._unhealthy:
            raise RuntimeError("native cleanup failed; execution service is unhealthy")
        if request.invocation_id in self._invocations or request.invocation_id in self._acquiring:
            raise ValueError(f"invocation already acquired: {request.invocation_id}")
        self._acquiring.add(request.invocation_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + request.remaining_seconds
        cfg = _engine_config(request.config)
        try:
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

    async def _deadline_watch(self, inv: _Invocation) -> None:
        await asyncio.sleep(max(0.0, inv.deadline - asyncio.get_running_loop().time()))
        if inv.terminal or inv.released:
            return
        inv.terminal = True
        if inv.task is not None and not inv.task.done():
            inv.task.cancel()
            try:
                await inv.task
            except asyncio.CancelledError:
                pass
        if inv.maintenance_task is not None and not inv.maintenance_task.done():
            inv.maintenance_task.cancel()
        try:
            await self.pool.discard(inv.session_key)
        except Exception as exc:
            self._unhealthy = True
            inv.cleanup_error = str(exc)
        else:
            self._invocations.pop(inv.handle.invocation_id, None)

    def _get(self, controller_id: str, execution_id: str, invocation_id: str) -> _Invocation:
        inv = self._invocations.get(invocation_id)
        if (
            inv is None
            or inv.handle.controller_id != controller_id
            or inv.handle.execution_id != execution_id
        ):
            raise ValueError("unknown execution")
        return inv

    async def turn(self, request: TurnRequest) -> AsyncIterator[ExecutionEvent]:
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.terminal or inv.released:
            raise ValueError("invocation is terminal")
        if inv.turn_active:
            raise ValueError("turn already running")
        try:
            async for event in self._turn_impl(request):
                yield event
        finally:
            if inv.turn_active:
                inv.terminal = True
                inv.turn_active = False
                if inv.task is not None and not inv.task.done():
                    inv.task.cancel()
                    try:
                        await inv.task
                    except (asyncio.CancelledError, Exception):
                        pass
                try:
                    await self.pool.discard(inv.session_key)
                except Exception as exc:
                    self._unhealthy = True
                    inv.cleanup_error = str(exc)
                else:
                    self._invocations.pop(inv.handle.invocation_id, None)

    async def _turn_impl(self, request: TurnRequest) -> AsyncIterator[ExecutionEvent]:
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.terminal or inv.released:
            raise ValueError("invocation is terminal")
        if inv.turn_active or inv.maintenance_active:
            raise ValueError(
                "maintenance active" if inv.maintenance_active else "turn already running"
            )
        inv.turn_active = True
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
            yield ExecutionEvent(
                kind="session_resolved",
                execution_id=inv.handle.execution_id,
                invocation_id=inv.handle.invocation_id,
                turn_id=request.turn_id,
                payload={"session_ref": inv.current_ref},
            )
            if inv.terminal or inv.released:
                raise ValueError("invocation is terminal")

        queue: asyncio.Queue[ExecutionEvent | None] = asyncio.Queue()
        stats = {"_cached_session": inv.cached_ref_pending}

        async def on_session_resolved(session_ref: str) -> None:
            inv.current_ref = session_ref
            queue.put_nowait(
                ExecutionEvent(
                    kind="session_resolved",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload={"session_ref": session_ref},
                )
            )

        def on_text(text: str) -> None:
            queue.put_nowait(
                ExecutionEvent(
                    kind="text",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload=text,
                )
            )

        def on_tool(tool: Any) -> None:
            queue.put_nowait(
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
                break
            yield event
        if inv.wake_task is not None:
            await inv.wake_task
        try:
            result = inv.task.result()
            inv.current_ref = result.session_ref
            inv.cached_ref_pending = False
            stats.pop("on_session_resolved", None)
            if stats.get("usage") is not None:
                yield ExecutionEvent(
                    kind="usage",
                    execution_id=inv.handle.execution_id,
                    invocation_id=inv.handle.invocation_id,
                    turn_id=request.turn_id,
                    payload=_json_value(stats["usage"]),
                )
            yield ExecutionEvent(
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
            inv.maintenance_active = False
            inv.maintenance_task = None

    async def release(self, request: ReleaseRequest) -> None:
        inv = self._get(request.controller_id, request.execution_id, request.invocation_id)
        if inv.released:
            return
        if (
            inv.terminal
            or inv.turn_active
            or inv.maintenance_active
            or (inv.task is not None and not inv.task.done())
        ):
            raise ValueError("cannot release an active invocation")
        if inv.deadline_task is not None:
            inv.deadline_task.cancel()
        try:
            await self.pool.release(inv.session_key, request.idle_ttl_seconds)
        except Exception:
            self._unhealthy = True
            raise
        inv.released = True
        self._invocations.pop(request.invocation_id, None)

    async def cancel(self, controller_id: str, invocation_id: str) -> None:
        inv = self._invocations.get(invocation_id)
        if inv is None or inv.handle.controller_id != controller_id:
            raise ValueError("unknown invocation")
        if inv.terminal:
            raise ValueError("invocation is terminal")
        inv.terminal = True
        if inv.deadline_task is not None:
            inv.deadline_task.cancel()
        if inv.maintenance_task is not None and not inv.maintenance_task.done():
            inv.maintenance_task.cancel()
        if inv.task is not None and not inv.task.done():
            inv.task.cancel()
            try:
                await inv.task
            except asyncio.CancelledError:
                pass
        try:
            await self.pool.discard(inv.session_key)
        except Exception as exc:
            self._unhealthy = True
            inv.cleanup_error = str(exc)
            raise
        self._invocations.pop(invocation_id, None)

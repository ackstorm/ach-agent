"""Small test-only client seam for runner policy tests.

Production uses ``ExecutionClient``. These tests exercise harness policy with their
existing controllable native doubles while keeping the production runner constructor
free of pool/driver compatibility parameters.
"""

from __future__ import annotations

import inspect
from typing import Any

from ach_agent.engine.workspace import workspace_dir
from ach_agent.execution.wire import ExecutionHandle


class RunnerClient:
    controller_id = "controller"

    def __init__(self, pool: Any, driver: Any) -> None:
        self.pool = pool
        self.driver = driver
        self._states: dict[str, list[Any]] = {}
        self.session_ops: list[str] = []

    async def acquire(self, request: Any) -> ExecutionHandle:
        server = await self.pool.acquire(request.lane_key, request.config)
        token = getattr(server, "proxy_route", "")
        if not isinstance(token, str) or not token:
            token = getattr(server, "proxy_token", "")
        if not isinstance(token, str) or not token:
            token = "test-route"
        handle = ExecutionHandle(
            instance_id="test-instance",
            controller_id=self.controller_id,
            execution_id=f"execution-{request.invocation_id}",
            invocation_id=request.invocation_id,
            proxy_route=token,
        )
        self._states[handle.invocation_id] = [
            server,
            request.conversation_key,
            request.lane_key,
            request.reuse,
            "",
        ]
        return handle

    def turn_callable(self, handle: ExecutionHandle):
        state = self._states[handle.invocation_id]
        server, conversation_key = state[0], state[1]

        async def run_turn(**kwargs: Any) -> Any:
            if not hasattr(self.driver, "run_turn"):
                from ach_agent.engine.base.driver import TurnResult

                result = TurnResult(text='{"action":"none","text":""}', session_ref="")
            else:
                result = await self.driver.run_turn(
                    server,
                    conv_key=conversation_key,
                    prompt=kwargs["prompt"],
                    reuse=state[3],
                    sessions=getattr(self.pool, "sessions", {}),
                    session_ref=state[4] or None,
                    on_text=kwargs["on_text"],
                    on_tool=kwargs["on_tool"],
                    max_tool_calls=kwargs["max_tool_calls"],
                    stats=kwargs["stats"],
                )
            state[4] = result.session_ref
            return result

        return run_turn

    async def session_op(self, request: Any) -> None:
        state = self._states[request.invocation_id]
        server, conversation_key, _lane_key, _reuse, session_ref = state
        self.session_ops.append(request.operation)
        if request.operation == "compact":
            await self.driver.compact_session(server, session_ref)
        elif request.operation == "discard":
            await self.driver.discard_session(server, session_ref)
        elif request.operation == "forget":
            getattr(self.pool, "sessions", {}).pop(conversation_key, None)

    async def release(self, request: Any) -> None:
        _server, _conversation_key, lane_key, _reuse, _session_ref = self._states.pop(
            request.invocation_id
        )
        await self.pool.release(lane_key, request.idle_ttl_seconds)

    async def cancel(self, controller_id: str, invocation_id: str) -> None:
        self._states.pop(invocation_id, None)
        discard = getattr(self.pool, "discard", None)
        if discard is not None:
            result = discard(invocation_id)
            if inspect.isawaitable(result):
                await result

    async def cancel_handle(self, handle: ExecutionHandle) -> None:
        await self.cancel(handle.controller_id, handle.invocation_id)

    async def prepare_workspace(self, request: Any) -> dict[str, str]:
        workspace = workspace_dir(request.work_dir, request.session_key)
        workspace.mkdir(parents=True, exist_ok=True)
        return {"status": "ok", "workspace": str(workspace)}

    async def handoff_workspace(self, request: Any) -> dict[str, str]:
        workspace = workspace_dir(request.work_dir, request.session_key)
        return {"status": "ok", "workspace": str(workspace)}

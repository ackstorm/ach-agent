from __future__ import annotations

import httpx
import pytest

from ach_agent.engine import trace
from ach_agent.execution.wire import ExecutionEvent, ExecutionHandle, TurnRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["opencode", "pi"])
@pytest.mark.parametrize("cost_source", ["engine", "proxy", "none"])
async def test_first_split_session_ack_has_trace_and_native_session(
    engine: str, cost_source: str
) -> None:
    from ach_agent.boot.execution_client import ExecutionClient

    del cost_source  # all cost sources share the engine-neutral trace wire contract
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery")
    handle = ExecutionHandle(
        instance_id="instance",
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        proxy_route=token,
    )
    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/execution/v1/session-ready"
        headers = trace.headers(token)
        seen.update(headers)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    client._handles["invocation"] = handle
    request = TurnRequest(
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        turn_id="turn-1",
        prompt="prompt",
        max_tool_calls=0,
    )
    event = ExecutionEvent(
        kind="session_resolved",
        execution_id="execution",
        invocation_id="invocation",
        turn_id="turn-1",
        payload={"session_ref": "ses_native" if engine == "opencode" else "/pi/sess.json"},
    )
    await client._ack_session(request, event)
    assert seen["langfuse_session_id"] == trace.session_id_for(event.payload["session_ref"])
    assert "traceparent" in seen
    await client.close()
    trace.drop(token)

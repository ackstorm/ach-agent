from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from ach_agent.engine import trace
from ach_agent.engine.base.events import OpenCodeUsage
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import (
    AcquireRequest,
    ExecutionHandle,
    PublicEngineConfig,
    SessionReadyRequest,
    TurnRequest,
)


class _ReplacementDriver:
    engine_type = "opencode"

    def __init__(self) -> None:
        self.refs: list[str] = []

    async def launch(self, _cfg, _session_key):
        from tests.execution.conftest import FakeServer

        return FakeServer(proxy_token="token")

    async def health(self, server):
        return server.is_alive()

    async def resolve_session(self, _server, *, conv_key, reuse, sessions, stats):
        del conv_key, reuse, stats
        ref = sessions.setdefault("conversation", "old-ref")
        return ref

    async def run_turn(self, _server, **kwargs):
        await kwargs["on_session_resolved"]("new-ref")
        self.refs.append("sent")
        kwargs["on_text"]("reply")
        from ach_agent.engine.base.driver import TurnResult

        return TurnResult(text='{"action":"none","text":"reply"}', session_ref="new-ref")

    async def discard_session(self, _server, _session_ref):
        pass

    async def compact_session(self, _server, _session_ref):
        pass

    async def stop(self, server):
        server.stopped = True


@pytest.mark.asyncio
async def test_service_waits_for_session_ready_before_native_model_send(fake_driver) -> None:
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    await service.claim_controller("controller")
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="invocation",
            lane_key="lane",
            conversation_key="conversation",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(),
        )
    )
    request = TurnRequest(
        controller_id="controller",
        execution_id=handle.execution_id,
        invocation_id=handle.invocation_id,
        turn_id="turn-1",
        prompt="prompt",
        max_tool_calls=0,
    )

    stream = service.turn(request)
    first = await stream.__anext__()
    assert first.kind == "session_resolved"
    assert fake_driver.turn_session_refs == []

    pending = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    assert not pending.done()
    await service.session_ready(
        SessionReadyRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id=handle.invocation_id,
            turn_id="turn-1",
        )
    )
    assert (await pending).kind in {"text", "usage", "turn_done"}
    await stream.aclose()


@pytest.mark.asyncio
async def test_service_repeats_session_ready_gate_for_replacement(fake_driver) -> None:
    driver = _ReplacementDriver()
    service = ExecutionService(driver, {})
    service.controller_required = True
    await service.claim_controller("controller")
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="invocation",
            lane_key="lane",
            conversation_key="conversation",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(),
        )
    )
    request = TurnRequest(
        controller_id="controller",
        execution_id=handle.execution_id,
        invocation_id=handle.invocation_id,
        turn_id="turn-1",
        prompt="prompt",
        max_tool_calls=0,
    )
    stream = service.turn(request)
    assert (await stream.__anext__()).kind == "session_resolved"
    await service.session_ready(
        SessionReadyRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id=handle.invocation_id,
            turn_id="turn-1",
        )
    )
    replacement = await stream.__anext__()
    assert replacement.kind == "session_resolved"
    assert driver.refs == []
    await service.session_ready(
        SessionReadyRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id=handle.invocation_id,
            turn_id="turn-1",
        )
    )
    assert (await stream.__anext__()).kind == "text"
    assert driver.refs == ["sent"]
    await stream.aclose()


def test_execution_client_has_separate_control_pool() -> None:
    from ach_agent.boot.execution_client import ExecutionClient

    client = ExecutionClient("http://execution", controller_id="controller")
    assert client.stream_client is not client.control_client
    assert client.controller_client is not client.control_client


@pytest.mark.asyncio
async def test_execution_client_acknowledges_session_before_decoding_turn_result() -> None:
    from ach_agent.boot.execution_client import ExecutionClient

    token = trace.mint_token()
    handle = ExecutionHandle(
        instance_id="instance",
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        proxy_route=token,
    )
    ready_order: list[str] = []
    events = [
        {
            "kind": "session_resolved",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": {"session_ref": "ses_native_1"},
        },
        {
            "kind": "tool",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": {
                "session_id": "ses_native_1",
                "part_id": "part",
                "message_id": "message",
                "tool_name": "lookup",
                "call_id": "call",
                "state": {"status": "completed", "output": "ok", "input": {}},
            },
        },
        {
            "kind": "usage",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": {
                "session_id": "ses_native_1",
                "message_id": "message",
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read": 3,
                "cache_write": 2,
                "cost": 0.42,
                "duration_ms": 125,
            },
        },
        {
            "kind": "turn_done",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": {
                "session_ref": "ses_native_1",
                "text": "reply",
                "aborted": False,
                "stats": {},
            },
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execution/v1/session-ready":
            ready_order.append("ack")
            assert trace.headers(token)["langfuse_session_id"] == "ses_native_1"
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.url.path == "/execution/v1/turn":
            ready_order.append("turn")
            body = b"".join(json.dumps(event).encode() + b"\n" for event in events)
            return httpx.Response(200, content=body, request=request)
        return httpx.Response(200, json={}, request=request)

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    client._handles[handle.invocation_id] = handle
    client._turn_ids[handle.invocation_id] = iter([1])  # type: ignore[assignment]
    stats: dict[str, object] = {}
    seen_tools: list[object] = []
    result = await client.turn_callable(handle)(
        prompt="prompt",
        max_tool_calls=3,
        on_text=None,
        on_tool=seen_tools.append,
        stats=stats,
    )
    assert ready_order == ["turn", "ack"]
    assert result.session_ref == "ses_native_1"
    assert result.text == "reply"
    assert isinstance(stats["usage"], OpenCodeUsage)
    assert stats["usage"].cache_read == 3  # type: ignore[union-attr]
    assert len(seen_tools) == 1
    await client.close()


@pytest.mark.asyncio
async def test_client_accepts_more_than_buffer_event_count_when_streaming() -> None:
    from ach_agent.boot.execution_client import ExecutionClient

    events = [
        {
            "kind": "text",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": str(index),
        }
        for index in range(1000)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        body = b"".join(json.dumps(event).encode() + b"\n" for event in events)
        return httpx.Response(200, content=body, request=request)

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    client._handles["invocation"] = ExecutionHandle(
        instance_id="instance",
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        proxy_route="token",
    )
    request = TurnRequest(
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        turn_id="turn-1",
        prompt="prompt",
        max_tool_calls=0,
    )
    assert len([event async for event in client.turn(request)]) == 1000
    await client.close()


@pytest.mark.asyncio
async def test_client_rejects_an_unterminated_oversized_record() -> None:
    from ach_agent.boot.execution_client import ExecutionClient, ExecutionClientError

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * (1024 * 1024 + 1), request=request)

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    client._handles["invocation"] = ExecutionHandle(
        instance_id="instance",
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        proxy_route="token",
    )
    request = TurnRequest(
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        turn_id="turn-1",
        prompt="prompt",
        max_tool_calls=0,
    )
    with pytest.raises(ExecutionClientError, match="exceeds 1 MiB"):
        await anext(client.turn(request))
    await client.close()


@pytest.mark.asyncio
async def test_client_monitors_held_controller_eof_and_validates_hello() -> None:
    from ach_agent.boot.execution_client import ExecutionClient, ExecutionClientError

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execution/v1/health":
            return httpx.Response(200, json={"instance_id": "instance"}, request=request)
        if request.url.path == "/execution/v1/controller":
            return httpx.Response(
                200,
                content=(b'{"version":1,"instance_id":"instance","controller_id":"controller"}\n'),
                request=request,
            )
        return httpx.Response(200, json={}, request=request)

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    hello = await client.connect()
    assert hello.instance_id == "instance"
    await asyncio.sleep(0)
    assert client._controller_lost
    await client.close()

    async def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b'{"version":1,"instance_id":"other","controller_id":"controller"}\n',
            request=request,
        )

    bad = ExecutionClient(
        "http://execution",
        controller_id="controller",
        instance_id="instance",
        transport=httpx.MockTransport(bad_handler),
    )
    with pytest.raises(ExecutionClientError, match="identity mismatch"):
        await bad.connect()
    await bad.close()

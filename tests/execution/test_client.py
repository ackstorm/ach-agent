from __future__ import annotations

import asyncio
import contextlib
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
    assert client.priority_client is not client.control_client


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
    events.append(
        {
            "kind": "turn_done",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": {"text": "done", "session_ref": ""},
        }
    )

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
    streamed = [event async for event in client.turn(request)]
    assert sum(event.kind == "text" for event in streamed) == 1000
    assert sum(event.kind == "turn_done" for event in streamed) == 1
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


@pytest.mark.asyncio
async def test_client_decodes_typed_launch_failure_without_poisoning_protocol() -> None:
    from ach_agent.boot.execution_client import ExecutionClient, ExecutionClientLaunchFailed

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={"type": "LaunchFailed", "message": "native did not start"},
            request=request,
        )

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    request = AcquireRequest(
        controller_id="controller",
        invocation_id="invocation",
        lane_key="lane",
        conversation_key="conversation",
        reuse=True,
        remaining_seconds=5,
        config=PublicEngineConfig(),
    )
    with pytest.raises(ExecutionClientLaunchFailed, match="native did not start"):
        await client.acquire(request)
    await client.close()


@pytest.mark.asyncio
async def test_client_rejects_truncated_stream_and_confirms_cancel() -> None:
    from ach_agent.boot.execution_client import ExecutionClient, ExecutionClientError

    canceled = asyncio.Event()
    handle = ExecutionHandle(
        instance_id="instance",
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        proxy_route="token",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execution/v1/turn":
            event = {
                "kind": "text",
                "execution_id": "execution",
                "invocation_id": "invocation",
                "turn_id": "turn-1",
                "payload": "partial",
            }
            return httpx.Response(200, content=json.dumps(event).encode() + b"\n", request=request)
        if request.url.path == "/execution/v1/cancel":
            canceled.set()
        return httpx.Response(200, json={"status": "ok"}, request=request)

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    client._handles[handle.invocation_id] = handle
    request = TurnRequest(
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        turn_id="turn-1",
        prompt="prompt",
        max_tool_calls=0,
    )
    with pytest.raises(ExecutionClientError, match="before turn_done"):
        _ = [event async for event in client.turn(request)]
    assert canceled.is_set()
    await client.close()


@pytest.mark.asyncio
async def test_real_http_client_ack_cancel_pool_and_controller_loss(fake_driver) -> None:
    from ach_agent.boot.execution_client import ExecutionClient, ExecutionClientError
    from tests.execution.test_http import _running_server

    service = ExecutionService(fake_driver, {})
    from ach_agent.execution.app import create_execution_app

    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        try:
            await client.connect()
            first = await client.acquire(
                AcquireRequest(
                    controller_id="controller",
                    invocation_id="first",
                    lane_key="first-lane",
                    conversation_key="first-conversation",
                    reuse=True,
                    remaining_seconds=5,
                    config=PublicEngineConfig(),
                )
            )
            second = await client.acquire(
                AcquireRequest(
                    controller_id="controller",
                    invocation_id="second",
                    lane_key="second-lane",
                    conversation_key="second-conversation",
                    reuse=True,
                    remaining_seconds=5,
                    config=PublicEngineConfig(),
                )
            )
            barrier = asyncio.Event()
            fake_driver.turn_barrier = barrier

            async def consume(handle: ExecutionHandle, turn_id: str) -> list[object]:
                request = TurnRequest(
                    controller_id=handle.controller_id,
                    execution_id=handle.execution_id,
                    invocation_id=handle.invocation_id,
                    turn_id=turn_id,
                    prompt="prompt",
                    max_tool_calls=0,
                )
                return [event async for event in client.turn(request)]

            first_task = asyncio.create_task(consume(first, "turn-first"))
            second_task = asyncio.create_task(consume(second, "turn-second"))
            fake_driver.launch_barrier = asyncio.Event()
            slow_acquire = asyncio.create_task(
                client.acquire(
                    AcquireRequest(
                        controller_id="controller",
                        invocation_id="slow-acquire",
                        lane_key="slow-acquire-lane",
                        conversation_key="slow-acquire-conversation",
                        reuse=True,
                        remaining_seconds=5,
                        config=PublicEngineConfig(),
                    )
                )
            )
            for _ in range(100):
                if fake_driver.turn_session_refs:
                    break
                await asyncio.sleep(0.01)
            assert not first_task.done()
            assert not second_task.done()
            await asyncio.sleep(0)
            assert not slow_acquire.done()
            await client.cancel("controller", "first")
            assert not second_task.done()
            assert not slow_acquire.done()
            fake_driver.launch_barrier.set()
            await asyncio.wait_for(slow_acquire, timeout=2)
            barrier.set()
            await asyncio.wait_for(second_task, timeout=2)
            with contextlib.suppress(Exception):
                await first_task
            assert client._controller_response is not None
            await client._controller_response.aclose()
            for _ in range(100):
                if client._controller_lost:
                    break
                await asyncio.sleep(0.01)
            assert client._controller_lost
            with pytest.raises(ExecutionClientError):
                await client.acquire(
                    AcquireRequest(
                        controller_id="controller",
                        invocation_id="closed",
                        lane_key="closed-lane",
                        conversation_key="closed-conversation",
                        reuse=True,
                        remaining_seconds=5,
                        config=PublicEngineConfig(),
                    )
                )
            assert first_task.done()
            assert service._invocations.get("first") is None
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_real_http_session_ack_failure_prevents_native_send(fake_driver, monkeypatch) -> None:
    from ach_agent.boot.execution_client import ExecutionClient, ExecutionClientError
    from ach_agent.execution.app import create_execution_app
    from tests.execution.test_http import _running_server

    service = ExecutionService(fake_driver, {})

    async def reject_session_ready(_request) -> None:
        raise RuntimeError("ack rejected")

    monkeypatch.setattr(service, "session_ready", reject_session_ready)
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        try:
            await client.connect()
            handle = await client.acquire(
                AcquireRequest(
                    controller_id="controller",
                    invocation_id="ack-failure",
                    lane_key="ack-failure-lane",
                    conversation_key="ack-failure-conversation",
                    reuse=True,
                    remaining_seconds=5,
                    config=PublicEngineConfig(),
                )
            )
            request = TurnRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id=handle.invocation_id,
                turn_id="turn-ack-failure",
                prompt="prompt",
                max_tool_calls=0,
            )
            with pytest.raises(ExecutionClientError):
                _ = [event async for event in client.turn(request)]
            assert fake_driver.turn_session_refs == []
        finally:
            await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_case", ["wrap", "repair"])
async def test_real_http_terminal_policy_preserves_session_and_deadline(
    fake_driver, terminal_case: str
) -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.engine.base.terminal import run_contract_turn
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.wire import AcquireRequest, PublicEngineConfig
    from tests.execution.test_http import _running_server

    class TerminalDriver(type(fake_driver)):
        def __init__(self) -> None:
            super().__init__()
            self.refs: list[str | None] = []
            self.calls = 0

        async def run_turn(self, server, **kwargs):
            self.calls += 1
            self.refs.append(kwargs["session_ref"])
            from ach_agent.engine.base.driver import TurnResult

            if terminal_case == "wrap" and self.calls == 1:
                return TurnResult(
                    text="partial", session_ref=kwargs["session_ref"] or "", aborted=True
                )
            if terminal_case == "repair" and self.calls == 1:
                return TurnResult(text="not terminal json", session_ref=kwargs["session_ref"] or "")
            return TurnResult(
                text='{"action":"none","text":"terminal"}',
                session_ref=kwargs["session_ref"] or "",
            )

    driver = TerminalDriver()
    service = ExecutionService(driver, {})
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        try:
            await client.connect()
            handle = await client.acquire(
                AcquireRequest(
                    controller_id="controller",
                    invocation_id=f"terminal-{terminal_case}",
                    lane_key=f"terminal-{terminal_case}-lane",
                    conversation_key=f"terminal-{terminal_case}-conversation",
                    reuse=True,
                    remaining_seconds=5,
                    config=PublicEngineConfig(),
                )
            )
            deadline = service._invocations[handle.invocation_id].deadline
            stats: dict[str, object] = {}
            result = await run_contract_turn(
                client.turn_callable(handle),
                prompt="prompt",
                free_form=False,
                terminal_action="none",
                terminal_retries=1,
                on_text=None,
                on_tool=None,
                max_tool_calls=0,
                stats=stats,
            )
            assert result["action"] == "none"
            assert driver.refs == ["native-ref", "native-ref"]
            assert service._invocations[handle.invocation_id].deadline == deadline
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_real_http_replacement_session_ack_precedes_native_send(fake_driver) -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.execution.app import create_execution_app
    from tests.execution.test_http import _running_server

    class ReplacementDriver(type(fake_driver)):
        def __init__(self) -> None:
            super().__init__()
            self.replacement_acknowledged = asyncio.Event()

        async def run_turn(self, server, **kwargs):
            await kwargs["on_session_resolved"]("replacement-ref")
            self.replacement_acknowledged.set()
            from ach_agent.engine.base.driver import TurnResult

            return TurnResult(
                text='{"action":"none","text":"reply"}', session_ref="replacement-ref"
            )

    driver = ReplacementDriver()
    service = ExecutionService(driver, {})
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        try:
            await client.connect()
            handle = await client.acquire(
                AcquireRequest(
                    controller_id="controller",
                    invocation_id="replacement",
                    lane_key="replacement-lane",
                    conversation_key="replacement-conversation",
                    reuse=True,
                    remaining_seconds=5,
                    config=PublicEngineConfig(),
                )
            )
            result = await client.turn_callable(handle)(
                prompt="prompt",
                max_tool_calls=0,
                on_text=None,
                on_tool=None,
                stats={},
            )
            assert result.session_ref == "replacement-ref"
            assert driver.replacement_acknowledged.is_set()
        finally:
            await client.close()

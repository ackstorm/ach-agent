from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
import uvicorn

from ach_agent.execution.app import (
    EXECUTION_API_VERSION,
    _BoundedStreamingResponse,
    create_execution_app,
)
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import AcquireRequest, PublicEngineConfig, TurnRequest


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://execution")


@contextlib.asynccontextmanager
async def _running_server(app):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started and server.servers:
                break
            await asyncio.sleep(0.01)
        assert server.servers
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_controller_hello_is_versioned_and_owns_service(fake_driver):
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        async with httpx.AsyncClient(base_url=base_url, timeout=2) as client:
            async with client.stream(
                "POST",
                "/execution/v1/controller",
                json={
                    "version": EXECUTION_API_VERSION,
                    "instance_id": service.instance_id,
                    "controller_id": "controller-a",
                },
            ) as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("application/x-ndjson")
                hello = await response.aiter_lines().__anext__()
                assert (
                    httpx.Response(200, content=hello).json()["instance_id"] == service.instance_id
                )
                await response.aclose()
                await asyncio.sleep(0.1)
    for _ in range(100):
        if service.can_accept_controller:
            break
        await asyncio.sleep(0.01)
    assert service.can_accept_controller
    assert not service._unhealthy, service.controller_cleanup_error


@pytest.mark.asyncio
async def test_controller_version_and_instance_are_checked(fake_driver):
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)

    async with _client(app) as client:
        bad_version = await client.post(
            "/execution/v1/controller",
            json={"version": 99, "instance_id": service.instance_id, "controller_id": "a"},
        )
        bad_instance = await client.post(
            "/execution/v1/controller",
            json={"version": 1, "instance_id": "old-instance", "controller_id": "a"},
        )

    assert bad_version.status_code == 409
    assert bad_instance.status_code == 409
    assert not service._unhealthy


@pytest.mark.asyncio
async def test_controller_eof_cleans_active_and_warm_execution_before_reconnect(fake_driver):
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)

    async with _running_server(app) as base_url:
        controller_client = httpx.AsyncClient(base_url=base_url, timeout=2)
        operations = httpx.AsyncClient(base_url=base_url, timeout=2)
        try:
            response = await controller_client.send(
                controller_client.build_request(
                    "POST",
                    "/execution/v1/controller",
                    json={
                        "version": EXECUTION_API_VERSION,
                        "instance_id": service.instance_id,
                        "controller_id": "controller-a",
                    },
                ),
                stream=True,
            )
            assert response.status_code == 200
            controller_lines = response.aiter_lines()
            assert await controller_lines.__anext__()
            acquired = await operations.post(
                "/execution/v1/acquire",
                json={
                    "controller_id": "controller-a",
                    "invocation_id": "active",
                    "lane_key": "lane",
                    "conversation_key": "conversation",
                    "reuse": True,
                    "remaining_seconds": 5,
                    "config": {},
                },
            )
            assert acquired.status_code == 200
            warm = await operations.post(
                "/execution/v1/acquire",
                json={
                    "controller_id": "controller-a",
                    "invocation_id": "warm",
                    "lane_key": "warm-lane",
                    "conversation_key": "warm-conversation",
                    "reuse": True,
                    "remaining_seconds": 5,
                    "config": {},
                },
            )
            assert warm.status_code == 200
            assert len(fake_driver.servers) == 2
            assert fake_driver.servers[0] is not fake_driver.servers[1]
            assert all(not server.stopped for server in fake_driver.servers)
            released = await operations.post(
                "/execution/v1/release",
                json={
                    "controller_id": "controller-a",
                    "execution_id": warm.json()["execution_id"],
                    "invocation_id": "warm",
                    "idle_ttl_seconds": 1,
                },
            )
            assert released.status_code == 200
            await response.aclose()
            for _ in range(100):
                if service.can_accept_controller:
                    break
                await asyncio.sleep(0.01)
            assert service.can_accept_controller
            assert fake_driver.stopped
            assert {id(server) for server in fake_driver.stopped_servers} == {
                id(server) for server in fake_driver.servers
            }

            async with operations.stream(
                "POST",
                "/execution/v1/controller",
                json={
                    "version": EXECUTION_API_VERSION,
                    "instance_id": service.instance_id,
                    "controller_id": "controller-b",
                },
            ) as replacement:
                assert replacement.status_code == 200
                assert await replacement.aiter_lines().__anext__()
        finally:
            await controller_client.aclose()
            await operations.aclose()


@pytest.mark.asyncio
async def test_http_cancel_of_stalled_execution_leaves_other_execution_responsive(fake_driver):
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    fake_driver.turn_barriers_by_conversation["slow"] = asyncio.Event()

    async with _running_server(app) as base_url:
        controller_client = httpx.AsyncClient(base_url=base_url, timeout=3)
        operations = httpx.AsyncClient(base_url=base_url, timeout=3)
        slow_response = None
        try:
            controller = await controller_client.send(
                controller_client.build_request(
                    "POST",
                    "/execution/v1/controller",
                    json={
                        "version": EXECUTION_API_VERSION,
                        "instance_id": service.instance_id,
                        "controller_id": "controller-a",
                    },
                ),
                stream=True,
            )
            controller_lines = controller.aiter_lines()
            assert await controller_lines.__anext__()
            handles = {}
            for invocation_id, conversation_key in (("slow", "slow"), ("fast", "fast")):
                acquired = await operations.post(
                    "/execution/v1/acquire",
                    json={
                        "controller_id": "controller-a",
                        "invocation_id": invocation_id,
                        "lane_key": invocation_id,
                        "conversation_key": conversation_key,
                        "reuse": True,
                        "remaining_seconds": 5,
                        "config": {},
                    },
                )
                assert acquired.status_code == 200, acquired.text
                handles[invocation_id] = acquired.json()
            assert len(fake_driver.servers) == 2
            slow_server, fast_server = fake_driver.servers
            assert slow_server is not fast_server
            assert not slow_server.stopped and not fast_server.stopped

            def turn_request(invocation_id: str, conversation_key: str):
                return operations.build_request(
                    "POST",
                    "/execution/v1/turn",
                    json={
                        "controller_id": "controller-a",
                        "execution_id": handles[invocation_id]["execution_id"],
                        "invocation_id": invocation_id,
                        "turn_id": "main",
                        "prompt": conversation_key,
                        "max_tool_calls": 0,
                    },
                )

            slow_response = await operations.send(turn_request("slow", "slow"), stream=True)
            slow_lines = slow_response.aiter_lines()
            slow_event = httpx.Response(200, content=await slow_lines.__anext__()).json()
            assert slow_event["kind"] == "session_resolved"
            acknowledged = await operations.post(
                "/execution/v1/session-ready",
                json={
                    "controller_id": "controller-a",
                    "execution_id": handles["slow"]["execution_id"],
                    "invocation_id": "slow",
                    "turn_id": "main",
                },
            )
            assert acknowledged.status_code == 200
            fast = await operations.send(turn_request("fast", "fast"), stream=True)
            fast_lines = fast.aiter_lines()
            fast_event = httpx.Response(200, content=await fast_lines.__anext__()).json()
            assert fast_event["kind"] == "session_resolved"
            acknowledged = await operations.post(
                "/execution/v1/session-ready",
                json={
                    "controller_id": "controller-a",
                    "execution_id": handles["fast"]["execution_id"],
                    "invocation_id": "fast",
                    "turn_id": "main",
                },
            )
            assert acknowledged.status_code == 200
            fast_tail = "\n".join([line async for line in fast_lines])
            await fast.aclose()
            assert fast.status_code == 200
            assert "turn_done" in fast_tail

            canceled = await operations.post(
                "/execution/v1/cancel",
                json={"controller_id": "controller-a", "invocation_id": "slow"},
            )
            assert canceled.status_code == 200
            for _ in range(100):
                if slow_server.stopped:
                    break
                await asyncio.sleep(0.01)
            assert slow_server.stopped
            assert not fast_server.stopped
        finally:
            if slow_response is not None:
                await slow_response.aclose()
            with contextlib.suppress(Exception):
                await controller.aclose()
            await controller_client.aclose()
            await operations.aclose()


@pytest.mark.asyncio
async def test_duplicate_turn_id_is_rejected_without_poisoning_service(fake_driver):
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    # ASGITransport buffers streaming responses; this legacy duplicate-ID test does not
    # exercise the held-stream session gate (the dedicated client test covers that path).
    service.controller_required = False
    # This test exercises the route's validation after a controller is attached by
    # directly claiming it; the held-controller transport test covers disconnect.
    await service.claim_controller("controller")

    async with _client(app) as client:
        acquire = await client.post(
            "/execution/v1/acquire",
            json={
                "controller_id": "controller",
                "invocation_id": "invocation",
                "lane_key": "lane",
                "conversation_key": "conversation",
                "reuse": True,
                "remaining_seconds": 5,
                "config": {},
            },
        )
        assert acquire.status_code == 200
        handle = acquire.json()
        body = {
            "controller_id": "controller",
            "execution_id": handle["execution_id"],
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "prompt": "hello",
            "max_tool_calls": 0,
        }
        first = await client.post("/execution/v1/turn", json=body)
        second = await client.post("/execution/v1/turn", json=body)

    assert first.status_code == 200
    assert second.status_code == 409
    assert not service._unhealthy
    await service.cancel("controller", "invocation")


@pytest.mark.asyncio
async def test_oversized_request_body_is_rejected_before_json_parse(fake_driver):
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)

    async with _client(app) as client:
        response = await client.post(
            "/execution/v1/acquire",
            content=b"{" + b"x" * (1024 * 1024) + b"}",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 413
    assert service.can_accept_controller


@pytest.mark.asyncio
async def test_stalled_write_cleans_owned_invocation_after_native_stop(monkeypatch, fake_driver):
    monkeypatch.setattr("ach_agent.execution.app.WRITE_TIMEOUT_SECONDS", 0.01)
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="inv",
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
        invocation_id="inv",
        turn_id="main",
        prompt="prompt",
        max_tool_calls=0,
    )

    async def body():
        stream = service.turn(request)
        try:
            async for event in stream:
                yield event.model_dump_json().encode() + b"\n"
        finally:
            await stream.aclose()

    fake_driver.stop_barrier = asyncio.Event()
    fake_driver.suppress_stop_cancellation = True

    async def send(message):
        if message["type"] == "http.response.body" and message.get("more_body"):
            await asyncio.Event().wait()

    response = _BoundedStreamingResponse(
        body(),
        media_type="application/x-ndjson",
        on_close=lambda: service.cancel("controller", "inv"),
    )
    response_task = asyncio.create_task(response.stream_response(send))
    try:
        await asyncio.wait_for(fake_driver.stop_started.wait(), timeout=1)
        assert not fake_driver.stopped
        fake_driver.stop_barrier.set()
        with pytest.raises(TimeoutError):
            await response_task
        assert fake_driver.stopped
        assert "inv" not in service._invocations
    finally:
        fake_driver.stop_barrier.set()
        if not response_task.done():
            response_task.cancel()
            with contextlib.suppress(BaseException):
                await response_task


@pytest.mark.asyncio
async def test_controller_owner_is_released_when_first_write_fails(fake_driver):
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")

    async def body():
        yield b"hello\n"

    async def failing_send(_message):
        raise ConnectionError("client disconnected")

    response = _BoundedStreamingResponse(
        body(),
        media_type="application/x-ndjson",
        on_close=lambda: service.release_controller("controller"),
    )
    with pytest.raises(ConnectionError):
        await response.stream_response(failing_send)
    assert service.can_accept_controller

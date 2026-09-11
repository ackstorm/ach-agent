from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest
import uvicorn

from ach_agent.execution.app import EXECUTION_API_VERSION, create_execution_app
from ach_agent.execution.service import ExecutionService


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
                hello = (await response.aiter_lines().__anext__())
                assert (
                    httpx.Response(200, content=hello).json()["instance_id"]
                    == service.instance_id
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
async def test_duplicate_turn_id_is_rejected_without_poisoning_service(fake_driver):
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
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

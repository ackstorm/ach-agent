# SPDX-License-Identifier: Apache-2.0
"""F5: a rejected sessionStart / handoff import fails the invocation, never the controller."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from ach_agent.boot.execution_client import (
    ExecutionClient,
    ExecutionClientError,
    WorkspaceOperationFailed,
)

Handler = Callable[[httpx.Request], httpx.Response]


def _client(route: Handler) -> ExecutionClient:
    never = asyncio.Event()

    class Held(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"version":1,"instance_id":"instance","controller_id":"c"}\n'
            await never.wait()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execution/v1/controller":
            return httpx.Response(200, stream=Held(), request=request)
        return route(request)

    return ExecutionClient(
        "http://execution",
        controller_id="c",
        instance_id="instance",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("status", [404, 409, 422])
async def test_session_start_rejection_keeps_the_controller(status: int) -> None:
    client = _client(
        lambda r: httpx.Response(status, json={"detail": "sessionStart failed: exited 1: boom"})
    )
    await client.connect()
    try:
        with pytest.raises(WorkspaceOperationFailed) as info:
            await client.start_session("c", "inv-1")
        assert info.value.rejection and info.value.confirmed
        assert "exited 1" in str(info.value)
        assert not client.controller_lost
    finally:
        await client.close()


@pytest.mark.parametrize("status", [404, 409, 413, 422])
async def test_handoff_rejection_keeps_the_controller(tmp_path: Path, status: int) -> None:
    archive = tmp_path / "h.tar.gz"
    archive.write_bytes(b"x")
    client = _client(lambda r: httpx.Response(status, json={"detail": "archive is too large"}))
    await client.connect()
    try:
        with pytest.raises(WorkspaceOperationFailed):
            await client.import_handoff(archive, "inv-1")
        assert not client.controller_lost
    finally:
        await client.close()


async def test_session_start_503_is_still_controller_fatal() -> None:
    client = _client(
        lambda r: httpx.Response(503, json={"detail": "execution service is unhealthy"})
    )
    await client.connect()
    try:
        with pytest.raises(ExecutionClientError) as info:
            await client.start_session("c", "inv-1")
        assert not isinstance(info.value, WorkspaceOperationFailed)
        assert client.controller_lost
    finally:
        await client.close()


async def test_session_start_transport_error_is_still_controller_fatal() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gone", request=request)

    client = _client(boom)
    await client.connect()
    try:
        with pytest.raises(ExecutionClientError):
            await client.start_session("c", "inv-1")
        assert client.controller_lost
    finally:
        await client.close()

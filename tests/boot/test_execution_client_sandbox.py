# SPDX-License-Identifier: Apache-2.0
"""ExecutionClient sandbox additions: bearer on every client, archive import, health, close."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ach_agent.boot.execution_client import (
    ExecutionClient,
    ExecutionClientError,
    WorkspaceOperationFailed,
)


def _client(handler, **kw) -> ExecutionClient:
    return ExecutionClient(
        "http://sb",
        controller_id="c",
        transport=httpx.MockTransport(handler),
        auth_token="tok",
        **kw,
    )


def test_bearer_header_on_all_six_clients() -> None:
    client = _client(lambda r: httpx.Response(200))
    for name in (
        "controller_client",
        "control_client",
        "cleanup_client",
        "priority_client",
        "acquire_client",
        "stream_client",
    ):
        assert getattr(client, name).headers["authorization"] == "Bearer tok", name


async def test_import_archive_returns_path_and_sends_bearer(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"path": "/tmp/ach-sandbox-hydration-x"})

    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    client = _client(handler)
    assert await client.import_archive(archive, "hydration") == "/tmp/ach-sandbox-hydration-x"
    assert seen[0].url.path == "/execution/v1/sandbox/archive/hydration"
    assert seen[0].headers["authorization"] == "Bearer tok"
    await client.close()


async def test_import_archive_413_is_a_workspace_failure(tmp_path: Path) -> None:
    archive = tmp_path / "a.tar.gz"
    archive.write_bytes(b"x")
    client = _client(lambda r: httpx.Response(413, json={"detail": "archive is too large"}))
    with pytest.raises(WorkspaceOperationFailed):
        await client.import_archive(archive, "home")
    await client.close()


async def test_sandbox_health_and_close() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execution/v1/health":
            return httpx.Response(200, json={"configured": False, "closing": False})
        return httpx.Response(200, json={"status": "ok"})

    client = _client(handler)
    assert (await client.sandbox_health())["configured"] is False
    await client.close_session()
    await client.close()


async def test_connect_409_session_closing_carries_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execution/v1/health":
            return httpx.Response(200, json={"instance_id": "i"})
        return httpx.Response(409, json={"detail": "session closing"})

    client = _client(handler)
    with pytest.raises(ExecutionClientError) as info:
        await client.connect()
    assert info.value.status_code == 409
    await client.close()

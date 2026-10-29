# SPDX-License-Identifier: MIT
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ach_runtime.config.schema import AgentIdentity, ArtifactsBlock
from ach_runtime.facades import artifacts as artifact_module
from ach_runtime.facades.artifacts import ArtifactOperations
from ach_runtime.harness.tokens import facade_token
from ach_runtime.storage.api import ArtifactProducer
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage
from tests.storage.fakes import FakeS3Client

KEY = b"artifact-tools-key"
WORKSPACE = "owned-ws"
INVOCATION = "invocation-one"
BASE = "http://localhost:8093"
TOKEN = facade_token(KEY, WORKSPACE)
CONTENT = "Résumé 🧪"
OWNER = StateRef(
    agent=AgentIdentity(
        name="agent", namespace="agents", uid="11111111-1111-1111-1111-111111111111"
    ),
    workspace_ref="a" * 64,
    domain="workspace",
)


async def _owned(claim: str) -> bool:
    return claim == WORKSPACE


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _operations(storage: Any) -> ArtifactOperations:
    return ArtifactOperations(
        storage=storage,
        owner=OWNER,
        producer=ArtifactProducer(session_ref="b" * 64, invocation_id=INVOCATION),
        policy=ArtifactsBlock(enabled=True, maxArtifactBytes=4096, retentionDays=30),
        workspace_name=WORKSPACE,
        key=KEY,
        owned_workspace=_owned,
    )


@contextlib.asynccontextmanager
async def _session(app: Any, *, base_url: str = BASE) -> AsyncIterator[ClientSession]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=base_url
    ) as client:
        async with streamable_http_client(
            f"{base_url}/t/{TOKEN}/runs/{INVOCATION}/", http_client=client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", [BASE, "http://ach-control-agent.ach.svc:8081"])
async def test_mcp_artifact_roundtrip_and_failures(base_url: str) -> None:
    """A text report crosses the SDK as a receipt and a standard binary resource."""
    assert hasattr(artifact_module, "ArtifactFacade")
    ArtifactFacade = artifact_module.ArtifactFacade
    backend = FakeS3Client()
    storage: Any = S3Storage(bucket="reports", prefix="ach", client=backend)
    calls: list[tuple[str, str]] = []

    async def resolve(token: str, invocation_id: str) -> ArtifactOperations:
        calls.append((token, invocation_id))
        return _operations(storage)

    facade = ArtifactFacade(
        key=KEY,
        owned_workspace=_owned,
        operations=resolve,
        exclude_tools=frozenset(),
    )
    app = facade.app()
    async with app.router.lifespan_context(app), _session(app, base_url=base_url) as session:
        listed = await session.list_tools()
        tools = {tool.name: tool for tool in listed.tools}
        assert set(tools) == {"create_artifact", "read_artifact"}
        assert set(tools["create_artifact"].inputSchema["properties"]) == {
            "idempotency_key", "content", "mime", "name"
        }
        assert set(tools["read_artifact"].inputSchema["properties"]) == {"artifact_id"}

        created = await session.call_tool(
            "create_artifact",
            {
                "idempotency_key": "daily-report",
                "content": CONTENT,
                "mime": "text/plain; charset=utf-8",
                "name": "report.txt",
            },
        )
        assert not created.isError
        receipt = json.loads(created.content[0].text)
        artifact_id = receipt["artifact_id"]
        assert receipt["metadata"]["size"] == len(CONTENT.encode("utf-8"))
        assert receipt["metadata"]["checksum"] == hashlib.sha256(CONTENT.encode("utf-8")).hexdigest()

        read = await session.call_tool("read_artifact", {"artifact_id": artifact_id})
        assert not read.isError
        resource = read.content[0].resource
        assert str(resource.uri) == f"artifact:{artifact_id}"
        assert base64.b64decode(resource.blob) == CONTENT.encode("utf-8")

        missing = await session.call_tool("read_artifact", {"artifact_id": "f" * 64})
        assert missing.isError
        assert missing.structuredContent is None

        storage = None
        unavailable = await session.call_tool(
            "create_artifact",
            {
                "idempotency_key": "no-storage",
                "content": "no receipt",
                "mime": "text/plain",
                "name": "no.txt",
            },
        )
        assert unavailable.isError
        assert unavailable.structuredContent is None

    assert calls and all(token == TOKEN and invocation == INVOCATION for token, invocation in calls)


@pytest.mark.asyncio
async def test_mcp_artifact_tool_exclusion_only_removes_requested_tool() -> None:
    assert hasattr(artifact_module, "ArtifactFacade")
    ArtifactFacade = artifact_module.ArtifactFacade

    async def resolve(_token: str, _invocation_id: str) -> ArtifactOperations:
        return _operations(None)

    facade = ArtifactFacade(
        key=KEY,
        owned_workspace=_owned,
        operations=resolve,
        exclude_tools=frozenset({"create_artifact"}),
    )
    app = facade.app()
    async with app.router.lifespan_context(app), _session(app) as session:
        listed = await session.list_tools()
    assert [tool.name for tool in listed.tools] == ["read_artifact"]

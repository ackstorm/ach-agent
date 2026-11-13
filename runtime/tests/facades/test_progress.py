# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ach_runtime.facades.progress import ProgressFacade
from ach_runtime.harness.tokens import FACADE_TOKEN_HEADER, facade_token

KEY = b"progress-key"
BASE = "http://localhost:8093"


async def _owned(claim: str) -> bool:
    return claim == "owned-ws"


@contextlib.asynccontextmanager
async def _session(app: Any, token: str) -> AsyncIterator[ClientSession]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE, headers={FACADE_TOKEN_HEADER: token}
    ) as client:
        async with streamable_http_client(f"{BASE}/sessions/session-1/", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


@pytest.mark.asyncio
async def test_inform_user_forwards_message_with_token_and_session() -> None:
    calls: list[tuple[str, str, str]] = []
    listening = True

    async def inform(token: str, invocation_id: str, message: str) -> bool:
        calls.append((token, invocation_id, message))
        return listening

    token = facade_token(KEY, "owned-ws")
    app = ProgressFacade(key=KEY, owned_workspace=_owned, inform=inform).app()
    async with app.router.lifespan_context(app), _session(app, token) as session:
        tools = (await session.list_tools()).tools
        assert [tool.name for tool in tools] == ["inform_user"]
        assert set(tools[0].inputSchema["properties"]) == {"message"}

        result = await session.call_tool("inform_user", {"message": "Reading the repo"})
        assert not result.isError
        assert calls == [(token, "session-1", "Reading the repo")]

        # Nobody listening (blocking caller gone, stream closed): still a success for the agent.
        listening = False
        result = await session.call_tool("inform_user", {"message": "Still going"})
        assert not result.isError


@pytest.mark.asyncio
async def test_inform_user_rejects_a_foreign_workspace_token() -> None:
    async def inform(token: str, invocation_id: str, message: str) -> bool:
        raise AssertionError("must not be reached")

    app = ProgressFacade(key=KEY, owned_workspace=_owned, inform=inform).app()
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE,
        headers={FACADE_TOKEN_HEADER: facade_token(KEY, "other-ws")},
    ) as client:
        response = await client.post("/sessions/session-1/", json={})
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_inform_user_description_carries_the_update_rules() -> None:
    async def inform(token: str, invocation_id: str, message: str) -> bool:
        return True

    app = ProgressFacade(key=KEY, owned_workspace=_owned, inform=inform).app()
    async with app.router.lifespan_context(app), _session(app, facade_token(KEY, "owned-ws")) as session:
        description = (await session.list_tools()).tools[0].description
    for rule in ("never the tool", "language of the user's request", "30 seconds"):
        assert rule in description

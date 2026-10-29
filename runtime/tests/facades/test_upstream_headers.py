# SPDX-License-Identifier: MIT
"""Injected upstream headers replace inbound ones case-insensitively; hop-by-hop never forwarded."""

from __future__ import annotations

import httpx

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.harness.tokens import facade_token

KEY = b"k" * 32
IDENTITY = ProcessIdentity(agent="a", environment="e")


async def _owned(claim: str) -> bool:
    return claim == "ws"


async def test_mcp_injects_exactly_one_header_per_configured_name():
    seen: list[list[tuple[str, str]]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append([(k.lower(), v) for k, v in request.headers.multi_items()])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    facade = McpFacade(
        servers={"serper": "https://mcp.serper.dev/mcp"},
        server_headers={"serper": {"X-Custom-Token": "real-secret"}},
        key=KEY, owned_workspace=_owned, exclude=frozenset(), exclude_tools=frozenset(),
        identity=IDENTITY, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    token = facade_token(KEY, "ws")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=facade.app()), base_url="http://f") as client:
        response = await client.post(
            f"/t/{token}/mcp/serper",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"x-custom-token": "sandbox-guess", "Connection": "keep-alive", "Upgrade": "h2c", "TE": "trailers"},
        )
    assert response.status_code == 200
    names = [name for name, _ in seen[0]]
    assert names.count("x-custom-token") == 1
    assert dict(seen[0])["x-custom-token"] == "real-secret"
    assert "upgrade" not in names and "te" not in names

# SPDX-License-Identifier: Apache-2.0
"""One initialized MCP client session, shared by every harness→MCP caller.

SDK note: the installed ``streamable_http_client`` takes NO ``headers=`` kwarg — auth is
injected by pre-building an httpx client via ``create_mcp_http_client(headers=...)``, which
also applies the SDK's recommended MCP timeouts. We own that client's lifecycle (the
transport only closes clients it created), hence the nested ``async with``. Centralised here
so an SDK upgrade is a one-line fix rather than one per call site.

Headers carry credentials (the ek_, the Hindsight bearer) — never log them (SEC-01).
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


@asynccontextmanager
async def mcp_session(endpoint: str, headers: dict[str, str] | None):  # type: ignore[no-untyped-def]
    """Open an initialized MCP session against ``endpoint`` with ``headers`` injected.

    Empty/absent headers → unauthenticated client (the internal/no-auth URL path).
    """
    async with create_mcp_http_client(headers=headers or None) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

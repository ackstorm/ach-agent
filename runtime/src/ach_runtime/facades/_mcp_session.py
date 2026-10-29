# SPDX-License-Identifier: MIT
"""One initialized MCP client session, opened fresh per ach-memory call (contract §5/§10).

Copied verbatim from old-ach-agent `engine/mcp_session.py`: the installed
`streamable_http_client` takes no `headers=` kwarg, so auth is injected by pre-building an
httpx client via `create_mcp_http_client(headers=...)`, which also applies the SDK's
recommended MCP timeouts. The transport only closes clients it created, hence the nested
`async with` -- this module owns that whole lifecycle, torn down again on every call via
the context manager exit (success, tool error or cancellation alike).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


@asynccontextmanager
async def mcp_session(endpoint: str, headers: dict[str, str] | None) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP session against `endpoint` with `headers` injected.

    Empty/absent headers -> unauthenticated client (the internal/no-auth URL path).
    """
    async with create_mcp_http_client(headers=headers or None) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

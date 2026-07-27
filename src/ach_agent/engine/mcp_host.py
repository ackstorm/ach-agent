# SPDX-License-Identifier: Apache-2.0
"""Host a FastMCP server on an ephemeral loopback port.

The three harness-hosted facades (memory, repo checkout, a2a egress) all need the same
bind/await-ready/teardown sequence. It lives here once so a uvicorn behaviour change is a
single fix. Bounded wait: a port-0 loopback bind flips `started` within ms, so we cap at
~5s and fail loud rather than hang (CLAUDE.md: no unbounded polling) — a local bind failing
is a genuine boot error, same as the sibling localhost proxies.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
import uvicorn

log = structlog.get_logger(__name__)


class LocalMcpHost:
    """Serve one FastMCP app on 127.0.0.1:0; `start` returns its MCP URL."""

    def __init__(self, mcp: Any, label: str, started_event: str | None = None) -> None:
        self._mcp = mcp
        self._label = label
        # repo facade logs "repo checkout facade started" but errors as "repo facade" — the
        # two strings are operator-visible, so keep them independently settable.
        self._started_event = started_event or f"{label} started"
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self, **log_fields: object) -> str:
        config = uvicorn.Config(
            self._mcp.streamable_http_app(), host="127.0.0.1", port=0, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(250):
            if self._server.started:
                break
            if self._task.done():  # serve() exited before starting → surface its error
                self._task.result()
                break
            await asyncio.sleep(0.02)
        if not self._server.started:
            raise RuntimeError(f"{self._label} failed to start within 5s")
        port = self._server.servers[0].sockets[0].getsockname()[1]
        log.info(self._started_event, port=port, **log_fields)
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        """Signal uvicorn to exit and await the serve task."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._server = None
        self._task = None

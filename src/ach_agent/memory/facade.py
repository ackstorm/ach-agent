# SPDX-License-Identifier: Apache-2.0
"""Harness-hosted memory MCP facade.

Fronts Hindsight for opencode on 127.0.0.1, exposing ONLY four agent-facing tools
(recall/reflect/get_mental_model/retain). Each call injects the harness-owned ``bank_id``
and the admin auth secret, then maps to the real ``hindsight_*`` tool. The agent never sees
``bank_id``, the admin secret, or any admin/destructive Hindsight tool.

opencode's ``memory-0`` MCP server points at this facade's URL, not at Hindsight.
"""

from __future__ import annotations

import asyncio

import structlog
import uvicorn
from mcp.server.fastmcp import FastMCP

from ach_agent.memory.hindsight import (
    HINDSIGHT_GET_MENTAL_MODEL,
    HINDSIGHT_RECALL,
    HINDSIGHT_REFLECT,
    HINDSIGHT_RETAIN,
    call_hindsight,
)

log = structlog.get_logger(__name__)


class MemoryFacade:
    """FastMCP server exposing 4 memory tools; proxies to Hindsight with bank_id + auth."""

    def __init__(self, endpoint: str, secret: str | None, bank_id: str) -> None:
        self._endpoint = endpoint
        self._secret = secret  # closure-only, never logged; None → internal/no-auth URL
        self._bank_id = bank_id
        self._mcp = FastMCP("ach-memory")
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._register_tools()

    async def _invoke(self, tool: str, args: dict[str, object]) -> str:
        """Inject bank_id + call the mapped Hindsight tool. Fail-soft: return a short note."""
        try:
            return await call_hindsight(
                self._endpoint, self._secret, tool, {"bank_id": self._bank_id, **args}
            )
        except Exception as exc:
            log.warning("memory facade: hindsight call failed", tool=tool, error=str(exc))
            return "Memory temporarily unavailable."

    def _register_tools(self) -> None:
        @self._mcp.tool(
            name="memory_recall", description="Search past memories by topic or filename."
        )
        async def memory_recall(query: str, tags: list[str] | None = None) -> str:
            return await self._invoke(HINDSIGHT_RECALL, {"query": query, "tags": tags})

        @self._mcp.tool(
            name="memory_reflect",
            description="Synthesize across memories — patterns, not single facts.",
        )
        async def memory_reflect(query: str, tags: list[str] | None = None) -> str:
            return await self._invoke(HINDSIGHT_REFLECT, {"query": query, "tags": tags})

        @self._mcp.tool(
            name="memory_get_mental_model",
            description="Read a pre-built mental-model summary by id.",
        )
        async def memory_get_mental_model(mental_model_id: str) -> str:
            return await self._invoke(
                HINDSIGHT_GET_MENTAL_MODEL, {"mental_model_id": mental_model_id}
            )

        @self._mcp.tool(
            name="memory_retain",
            description="Store an insight for future sessions. Tag it, e.g. tags=['repo:<name>'].",
        )
        async def memory_retain(content: str, tags: list[str] | None = None) -> str:
            return await self._invoke(HINDSIGHT_RETAIN, {"content": content, "tags": tags})

    async def start(self) -> str:
        """Bind the facade on an ephemeral localhost port; return its MCP URL."""
        config = uvicorn.Config(
            self._mcp.streamable_http_app(), host="127.0.0.1", port=0, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Bounded wait: a port-0 loopback bind flips `started` within ms. Cap at ~5s and fail
        # loud rather than hang (CLAUDE.md: no unbounded polling) — a local bind failing is a
        # genuine boot error, same as the sibling localhost proxies.
        for _ in range(250):
            if self._server.started:
                break
            if self._task.done():  # serve() exited before starting → surface its error
                self._task.result()
                break
            await asyncio.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("memory facade failed to start within 5s")
        port = self._server.servers[0].sockets[0].getsockname()[1]
        log.info("memory facade started", port=port, bank_id=self._bank_id)
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        """Signal uvicorn to exit and await the serve task."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._server = None
        self._task = None

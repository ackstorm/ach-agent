# SPDX-License-Identifier: MIT
"""Stateless fixture ach-memory MCP upstreams for Task 2's real SDK round-trip tests: one
FastMCP app with bare tool names (a direct ach-memory) and one with gateway-prefixed names
(``ach-memory.<tool>``, an aggregating gateway), each recording every call's
``(tool, arguments)`` in receipt order for assertion.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette

# Every backend tool a fixture upstream can answer: the five agent-facing ones, plus
# `load_context` (used by `_memory_client.fetch_context`) and `sync_retain` (a governance
# tool that must never be dispatched -- present only so `advertised_name` negative tests
# have something real to not match).
BARE_TOOLS = (
    "recall",
    "reflect",
    "retain",
    "get_mental_model",
    "list_mental_models",
    "load_context",
    "sync_retain",
)


class RecordingUpstream:
    """One fixture ach-memory MCP server.

    ``responses[tool]`` is the text the next call to that tool returns (default ``""``).
    ``raises`` names tools that raise instead of returning, so the SDK reports them back to
    the client as a normal ``CallToolResult`` with ``isError=True`` (never an exception on
    the wire). ``calls`` records every ``(tool, arguments)`` actually received, in order,
    including the injected ``scope``/``project_slug``.
    """

    def __init__(self, *, prefix: str = "") -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses: dict[str, str] = {}
        self.raises: set[str] = set()
        # Tools named here block forever (until the caller cancels) after recording the
        # call -- a bounded way to prove in-flight cancellation/teardown without a real
        # network hang (review I1).
        self.blocking: set[str] = set()
        self._prefix = prefix
        self._mcp: FastMCP[None] = FastMCP(
            "fixture-ach-memory", streamable_http_path="/", stateless_http=True, json_response=True
        )
        for bare in BARE_TOOLS:
            self._register(bare)

    def _wire_name(self, bare: str) -> str:
        return f"{self._prefix}.{bare}" if self._prefix else bare

    def _register(self, bare: str) -> None:
        name = self._wire_name(bare)

        @self._mcp.tool(name=name)
        async def _tool(
            query: str | None = None,
            tags_filter: list[str] | None = None,
            model_key: str | None = None,
            content: str | None = None,
            memory_type: str | None = None,
            basis: str | None = None,
            trigger: str | None = None,
            evidence: list[dict[str, Any]] | None = None,
            tags: list[str] | None = None,
            project_slug: str | None = None,
            scope: str | None = None,
        ) -> str:
            arguments = {
                k: v
                for k, v in {
                    "query": query,
                    "tags_filter": tags_filter,
                    "model_key": model_key,
                    "content": content,
                    "memory_type": memory_type,
                    "basis": basis,
                    "trigger": trigger,
                    "evidence": evidence,
                    "tags": tags,
                    "project_slug": project_slug,
                    "scope": scope,
                }.items()
                if v is not None
            }
            self.calls.append((bare, arguments))
            if bare in self.blocking:
                await asyncio.Event().wait()
            if bare in self.raises:
                raise RuntimeError(f"fixture: {bare} failed")
            return self.responses.get(bare, "")

    def app(self) -> Starlette:
        return self._mcp.streamable_http_app()

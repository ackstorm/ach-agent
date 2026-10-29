# SPDX-License-Identifier: MIT
"""Ach-memory facade -- exposes exactly five agent-facing tools over the real `mcp` SDK,
injects fixed `scope="project"`/`project_slug` on every call overriding any client-supplied
value, and excludes the raw backend from the generic MCP facade even when degraded
(contract §5/§10). Adapted from old-ach-agent `memory/{ach_memory,ach_memory_facade}.py`.

Reuses FastMCP / the `mcp` SDK streamable-HTTP transport (`facades/_mcp_session.py`)
instead of Task 1's raw-JSON-RPC rewrite: `tools/list` answers with the real typed schema
and `tools/call` is a genuine SDK round-trip to the upstream ach-memory MCP server, both
behind the same Workspace HMAC guard as every other facade -- re-verified on every
request, including the GET/DELETE session follow-ups the streamable-HTTP transport issues
after the first POST, not just the first call.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount
from starlette.types import Receive, Scope, Send

from ach_runtime.facades._memory_client import (
    ACH_MEMORY_GET_MENTAL_MODEL,
    ACH_MEMORY_LIST_MENTAL_MODELS,
    ACH_MEMORY_RECALL,
    ACH_MEMORY_REFLECT,
    ACH_MEMORY_RETAIN,
    AuthSecretResolver,
    _describe,
    call_ach_memory,
    excluded_mcp_server_ids,
    normalize_slug,
    resolve_ach_memory_auth,
    resolve_endpoint,
    resolve_project,
)
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.harness.tokens import WorkspaceAuthenticationFailed, authenticate_facade

log = logging.getLogger(__name__)

OwnedWorkspace = Callable[[str], Awaitable[bool]]

__all__ = [
    "ACH_MEMORY_GET_MENTAL_MODEL",
    "ACH_MEMORY_LIST_MENTAL_MODELS",
    "ACH_MEMORY_RECALL",
    "ACH_MEMORY_REFLECT",
    "ACH_MEMORY_RETAIN",
    "ALLOWED_TOOLS",
    "AchMemoryFacade",
    "AuthSecretResolver",
    "excluded_mcp_server_ids",
    "normalize_slug",
    "resolve_ach_memory_auth",
    "resolve_endpoint",
    "resolve_project",
]

# Agent-facing allowlist (legacy `ACH_MEMORY_TOOLS`) -- governance tools (forget/correct/
# restore/...) are deliberately absent and never registered, forwarded or advertised.
ALLOWED_TOOLS = frozenset(
    {
        ACH_MEMORY_RECALL,
        ACH_MEMORY_REFLECT,
        ACH_MEMORY_RETAIN,
        ACH_MEMORY_GET_MENTAL_MODEL,
        ACH_MEMORY_LIST_MENTAL_MODELS,
    }
)

# The typed-retain vocabulary, mirrored from ach-memory's memory_types.py (legacy
# `ach_memory_facade.py`). Literals so the agent gets real enums in the tool schema and a
# wrong value fails locally instead of bouncing on the service's own validation.
MemoryType = Literal["preference", "constraint", "decision", "convention", "fact", "gotcha"]
EvidenceBasis = Literal["human_explicit", "agent_verified"]
RetainTrigger = Literal["user_requested", "agent_proactive"]
EvidenceKind = Literal["user_quote", "tool_result", "artifact_excerpt"]

# One bank holds everything this agent knows, so tags are how it separates repositories
# inside that bank -- never by switching banks, which it cannot do.
Tags = Annotated[
    list[str] | None,
    Field(
        description="Optional tags, e.g. ['repo:group/name']. One bank holds all of your "
        "memory — tag by repository when you work across several."
    ),
]


class RetainEvidence(BaseModel):
    """One bounded provenance excerpt. Never stored as searchable memory itself."""

    kind: EvidenceKind
    raw: Annotated[str, Field(max_length=1024, description="The excerpt, verbatim.")]
    source_ref: Annotated[
        str | None, Field(default=None, max_length=512, description="Where it came from.")
    ] = None


class _AuthGuard:
    """Verifies the Workspace HMAC on every request before any SDK delegation -- POST, and
    the GET/DELETE session follow-ups the streamable-HTTP transport issues afterwards --
    never only the first call of a session."""

    def __init__(self, app: Any, *, key: bytes, owned_workspace: OwnedWorkspace) -> None:
        self._app = app
        self._key = key
        self._owned_workspace = owned_workspace

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        token = scope.get("path_params", {}).get("token", "")
        try:
            await authenticate_facade(self._key, token, owned_workspace=self._owned_workspace)
        except WorkspaceAuthenticationFailed:
            await JSONResponse({"detail": "forbidden"}, status_code=403)(scope, receive, send)
            return
        await self._app(scope, receive, send)


class AchMemoryFacade:
    """FastMCP server exposing 5 memory tools; proxies to ach-memory with scope + project."""

    def __init__(
        self,
        *,
        endpoint: str,
        headers: dict[str, str],
        project: str,
        key: bytes,
        owned_workspace: OwnedWorkspace,
        identity: ProcessIdentity,
        exclude_tools: frozenset[str] = frozenset(),
    ) -> None:
        self._endpoint = endpoint
        # Already resolved for whichever auth mode the operator chose; instance-local,
        # never logged, reaches no config file the agent can read.
        self._headers = headers
        self._project = project
        self._key = key
        self._owned_workspace = owned_workspace
        self._identity = identity
        # Mounted in the Harness's cluster-facing listener, not a loopback server.
        self._mcp: FastMCP[None] = FastMCP("ach-memory", host="0.0.0.0", streamable_http_path="/")
        self._register_tools()
        # `ach.capability.filter.exclude.tools` names memory tools bare (`memory_retain`).
        for name in exclude_tools:
            with contextlib.suppress(ToolError):  # not a memory tool
                self._mcp.remove_tool(name)

    async def _invoke(self, tool: str, args: dict[str, Any]) -> str:
        """Inject scope + project_slug, then call ach-memory. Fail-soft: return a short
        note, never raise into the engine. The injection OVERRIDES anything in ``args``
        rather than filling a gap -- a containment boundary, not a convenience for a
        forgetful model. Keys whose value is None are dropped so an omitted optional (tags)
        is never sent as an explicit null. A missing endpoint degrades with zero SDK
        traffic."""
        if not self._endpoint:
            return "Memory temporarily unavailable."
        payload: dict[str, object] = {k: v for k, v in args.items() if v is not None}
        payload["scope"] = "project"
        payload["project_slug"] = self._project
        try:
            return await call_ach_memory(
                self._endpoint, self._headers, tool, payload, identity=self._identity
            )
        except Exception as exc:
            log.warning("ach-memory facade: call failed tool=%s error=%s", tool, _describe(exc))
            return "Memory temporarily unavailable."

    def _register_tools(self) -> None:
        @self._mcp.tool(
            name="memory_recall",
            description=(
                "Semantic search over stored memories; returns the facts/insights most "
                "relevant to `query` (ranked by relevance, not recency). Call this BEFORE "
                "acting to pull prior context on a topic, file, decision, or person. "
                "Returns plain text, or an 'unavailable' note if memory is down."
            ),
        )
        async def memory_recall(query: str, tags: Tags = None) -> str:
            return await self._invoke(ACH_MEMORY_RECALL, {"query": query, "tags_filter": tags})

        @self._mcp.tool(
            name="memory_reflect",
            description=(
                "Synthesize an answer ACROSS many memories — patterns, themes, a summary — "
                "instead of returning individual facts (use `memory_recall` for specific "
                "facts). Ask things like 'what recurring problems have we seen'. Broader "
                "and slower than recall."
            ),
        )
        async def memory_reflect(query: str) -> str:
            return await self._invoke(ACH_MEMORY_REFLECT, {"query": query})

        @self._mcp.tool(
            name="memory_get_mental_model",
            description=(
                "Read one mental model — a living summary of a fixed topic that the service "
                "refreshes as memories grow. Fetch by key when you need that topic's current "
                "overview without searching. The available keys head your ## Memory section."
            ),
        )
        async def memory_get_mental_model(
            model_key: Annotated[str, Field(description="Logical key, e.g. 'project-context'.")],
        ) -> str:
            return await self._invoke(ACH_MEMORY_GET_MENTAL_MODEL, {"model_key": model_key})

        @self._mcp.tool(
            name="memory_list_mental_models",
            description="List the mental models available to you, with their keys.",
        )
        async def memory_list_mental_models() -> str:
            return await self._invoke(ACH_MEMORY_LIST_MENTAL_MODELS, {})

        @self._mcp.tool(
            name="memory_retain",
            description=(
                "Store ONE durable, independently-correctable claim for FUTURE sessions — a "
                "decision, convention, constraint, recurring bug or gotcha — never transient "
                "chatter about the current task. Every typed field is REQUIRED and a retain "
                "missing any of them is rejected. Write content in English whatever language "
                "the conversation is in: retrieval reranks in English only."
            ),
        )
        async def memory_retain(
            content: Annotated[
                str,
                Field(
                    max_length=4096,
                    description="The claim, in English. One claim — split anything compound.",
                ),
            ],
            memory_type: Annotated[MemoryType, Field(description="What kind of claim.")],
            basis: Annotated[
                EvidenceBasis,
                Field(description="human_explicit: the user said it. agent_verified: you checked."),
            ],
            trigger: Annotated[RetainTrigger, Field(description="Who initiated storing this.")],
            evidence: Annotated[
                list[RetainEvidence],
                Field(min_length=1, max_length=4, description="1-4 provenance excerpts."),
            ],
            tags: Tags = None,
        ) -> str:
            return await self._invoke(
                ACH_MEMORY_RETAIN,
                {
                    "content": content,
                    "memory_type": memory_type,
                    "basis": basis,
                    "trigger": trigger,
                    "evidence": [e.model_dump(exclude_none=True) for e in evidence],
                    "tags": tags,
                },
            )

    def app(self) -> Starlette:
        mcp_app = self._mcp.streamable_http_app()
        guarded = _AuthGuard(mcp_app, key=self._key, owned_workspace=self._owned_workspace)

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

        return Starlette(routes=[Mount("/t/{token}/memory", app=guarded)], lifespan=lifespan)

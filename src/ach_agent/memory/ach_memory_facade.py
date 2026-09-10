# SPDX-License-Identifier: Apache-2.0
"""Harness-hosted ach-memory MCP facade.

Fronts ach-memory for opencode on 127.0.0.1, exposing five agent-facing tools. Every call
gets ``scope="project"`` and the harness-owned ``project_slug`` injected on the way through;
neither appears on any exposed signature, so the agent cannot choose — or be argued into
choosing — another bank. The agent never sees the user key or the real endpoint.

Same shape as ``RepoCheckoutFacade`` and the a2a egress facade: a FastMCP app on an
ephemeral loopback port via ``LocalMcpHost``. opencode's ``memory`` MCP server points here.
See CLAUDE.md, THE INVARIANT.
"""

from __future__ import annotations

from typing import Annotated, Literal

import structlog
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from ach_agent.engine.mcp_host import LocalMcpHost
from ach_agent.memory.ach_memory import (
    ACH_MEMORY_GET_MENTAL_MODEL,
    ACH_MEMORY_LIST_MENTAL_MODELS,
    ACH_MEMORY_RECALL,
    ACH_MEMORY_REFLECT,
    ACH_MEMORY_RETAIN,
    Headers,
    call_ach_memory,
)

log = structlog.get_logger(__name__)

# The typed-retain vocabulary, mirrored from ach-memory's memory_types.py. Declared as
# Literals so the agent gets real enums in the tool schema and a wrong value fails locally
# instead of bouncing on the service's validation.
MemoryType = Literal["preference", "constraint", "decision", "convention", "fact", "gotcha"]
EvidenceBasis = Literal["human_explicit", "agent_verified"]
RetainTrigger = Literal["user_requested", "agent_proactive"]
EvidenceKind = Literal["user_quote", "tool_result", "artifact_excerpt"]

# One bank holds everything this agent knows, so tags are how it separates repositories
# inside that bank — never by switching banks, which it cannot do.
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


class AchMemoryFacade:
    """FastMCP server exposing 5 memory tools; proxies to ach-memory with scope + project."""

    def __init__(self, endpoint: str, headers: Headers, project: str) -> None:
        self._endpoint = endpoint
        # The credential, already resolved for whichever auth mode the operator chose
        # (`x-ach-key` through ACH, or a Bearer user key direct). Instance-local and never
        # logged; it reaches no config file the agent can read.
        self._headers = headers
        self._project = project
        self._mcp = FastMCP("ach-memory")
        self._host = LocalMcpHost(self._mcp, "ach-memory facade")
        self._register_tools()

    async def _invoke(self, tool: str, args: dict[str, object]) -> str:
        """Inject scope + project_slug, then call ach-memory. Fail-soft: return a short note.

        The injection OVERRIDES anything in ``args`` rather than filling a gap — this is a
        containment boundary, not a convenience for a forgetful model. Keys whose value is
        None are dropped so an omitted optional (tags) is never sent as an explicit null.
        """
        payload: dict[str, object] = {k: v for k, v in args.items() if v is not None}
        payload["scope"] = "project"
        payload["project_slug"] = self._project
        try:
            return await call_ach_memory(self._endpoint, self._headers, tool, payload)
        except Exception as exc:
            log.warning("ach-memory facade: call failed", tool=tool, error=str(exc))
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
            # `tags_filter` on the wire, `tags` to the agent: ach-memory renamed recall's
            # parameter (and added tags_filter_mode beside it) while retain kept `tags`.
            # Exposing that split would make the agent's two calls disagree for no reason.
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

    async def start(self) -> str:
        """Bind the facade on an ephemeral localhost port; return its MCP URL."""
        return await self._host.start(project=self._project)

    async def stop(self) -> None:
        """Signal uvicorn to exit and await the serve task."""
        await self._host.stop()

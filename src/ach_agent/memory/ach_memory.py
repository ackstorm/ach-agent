# SPDX-License-Identifier: Apache-2.0
"""ach-memory backend: project resolution, the harness→service seam, standing context.

One bank per AGENT, never per repository. The project slug is derived from the agent's own
identity (``{namespace}-{agent.name}``) at boot and is static for the process's whole life:
it selects a memory bank through ``projects.resolve``, so deriving it from an inbound payload
would let an event choose which bank the agent reads and writes. An agent that works across
several repositories differentiates them with TAGS inside its one bank.

Standing context is assembled server-side: ``load_context`` returns text that is already
ordered, heading-neutralised, per-section budgeted and globally capped. The harness wraps it
and adds nothing — see :func:`fetch_context`.

The secret never leaves this module and ``memory/ach_memory_facade.py``: the agent reaches
ach-memory only through the loopback facade (CLAUDE.md, THE INVARIANT).
"""

from __future__ import annotations

import re

import structlog

from ach_agent.config.schema import AchMemoryMemory
from ach_agent.engine.mcp_session import mcp_session
from ach_agent.memory.common import (
    inc_memory_degraded,
    probe_memory_endpoint,
    resolve_memory_secret,
)

log = structlog.get_logger(__name__)

# memory.type discriminator — the key this module is registered under in MEMORY_BACKENDS,
# which is how tools_spec_for() finds this backend's TOOLS_SPEC for the system prompt.
TYPE = "ach-memory"

# Canonical ach-memory MCP tool names (as advertised by memory/mcp/*_tools.py).
ACH_MEMORY_RECALL = "recall"
ACH_MEMORY_REFLECT = "reflect"
ACH_MEMORY_RETAIN = "retain"
ACH_MEMORY_GET_MENTAL_MODEL = "get_mental_model"
ACH_MEMORY_LIST_MENTAL_MODELS = "list_mental_models"
ACH_MEMORY_LOAD_CONTEXT = "load_context"

# The agent-facing tool set. Fixed in code, not configurable: everything governance-shaped
# (create/update/delete_mental_model, forget, correct, restore, documents, operations,
# working state, sync_retain) is deliberately absent — a memory tool the operator did not
# ask for is a tool nobody reviewed.
ACH_MEMORY_TOOLS = (
    ACH_MEMORY_RECALL,
    ACH_MEMORY_REFLECT,
    ACH_MEMORY_RETAIN,
    ACH_MEMORY_GET_MENTAL_MODEL,
    ACH_MEMORY_LIST_MENTAL_MODELS,
)

# Kubernetes namespace, conventionally injected via the downward API. Absent (local runs,
# docker-compose) → the slug is the agent name alone.
NAMESPACE_ENV = "POD_NAMESPACE"

# Mirrors ach-memory's own `normalize_slug` (src/memory/slugs.py): lowercase alphanumerics,
# dots and hyphens, everything else collapsed to '-'. Pre-normalising here means BOTH ends
# agree on one string — the server would otherwise silently rewrite ours, and `acme/app` and
# `acme-app` collapse to the same bank.
_NON_SLUG = re.compile(r"[^a-z0-9.-]+")
MAX_SLUG_LENGTH = 128


def normalize_slug(raw: str) -> str:
    """Lowercase, collapse non-slug runs to '-', trim, cap. '' when nothing survives."""
    slug = _NON_SLUG.sub("-", raw.strip().lower()).strip("-.")[:MAX_SLUG_LENGTH]
    return slug if any(c.isalnum() for c in slug) else ""


def resolve_project(params: object, agent_name: str) -> str:
    """The agent's memory project slug: explicit override, else `{namespace}-{agent.name}`.

    Boot-time and static. NOT the pod name — a Deployment pod is `<name>-<rs>-<rand>` and a
    fresh one on every restart and scale event, which would silently hand the agent an empty
    bank after each rollout. Namespace + agent name is stable across both.
    """
    import os

    override = getattr(params, "project", "") or ""
    if override:
        return normalize_slug(override)
    namespace = os.environ.get(NAMESPACE_ENV, "").strip()
    return normalize_slug(f"{namespace}-{agent_name}" if namespace else agent_name)


def ach_memory_auth_headers(secret: str | None) -> dict[str, str]:
    """Bearer header for the ach-memory user key. Empty when no secret (internal URL)."""
    return {"Authorization": f"Bearer {secret}"} if secret else {}


async def call_ach_memory(
    endpoint: str, secret: str | None, tool: str, args: dict[str, object]
) -> str:
    """Call one ach-memory MCP tool; return the first text content ('' if none).

    The single harness→ach-memory seam (probe/context/facade all route here, so tests
    monkeypatch one function). ``secret`` builds headers and is never logged.
    """
    async with mcp_session(f"{endpoint.rstrip('/')}/mcp", ach_memory_auth_headers(secret)) as s:
        result = await s.call_tool(tool, args)
        text: str = getattr(result.content[0], "text", "") if result.content else ""
        # A tool-level error arrives as a NORMAL result with isError=True, not an exception.
        # Raise so every caller degrades loudly instead of the error text masquerading as
        # a valid memory or a valid standing-context section.
        if getattr(result, "isError", False):
            raise RuntimeError(f"ach-memory tool {tool!r} errored: {text}")
        return text


async def fetch_context(endpoint: str, secret: str | None, project: str) -> str:
    """The ``## Memory`` prompt section, from one ``load_context`` call.

    ``ContextPayload.text`` is taken VERBATIM. The service has already ordered the sections,
    neutralised forged headings, applied every per-section budget and enforced its global
    token ceiling; re-rendering the parts would undo all of it. ``omissions``/``overages``/
    ``total_tokens`` are logged and appear nowhere in the prompt.

    Never raises — any failure yields the degraded note (D-02 fail-open).
    """
    import json

    try:
        raw = await call_ach_memory(
            endpoint, secret, ACH_MEMORY_LOAD_CONTEXT, {"project_slug": project}
        )
        payload = json.loads(raw) if raw else {}
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        if not text:
            return "## Memory\n\nNo standing context yet."
        log.info(
            "memory: standing context loaded",
            project=project,
            total_tokens=payload.get("total_tokens"),
            omissions=payload.get("omissions"),
            overages=payload.get("overages"),
        )
        return f"## Memory\n\n{text}"
    except Exception as exc:
        log.warning("memory: load_context failed — running degraded", error=str(exc))
        inc_memory_degraded()
        return "## Memory\n\nUnavailable (context load failed)."


async def prepare_ach_memory(memory_cfg: AchMemoryMemory, project: str) -> tuple[bool, str]:
    """Probe the backend and build the prompt section. Returns (available, prompt_section).

    Called BEFORE pool.acquire in engine_runner so the opencode.json written for that server
    includes or excludes the memory MCP server. Never raises (MEM-02 / D-02 fail-open).
    """
    try:
        params = memory_cfg.ach_memory
        ok, secret = resolve_memory_secret(params.auth)
        if not ok:
            log.warning("memory: auth configured but env unset — running degraded")
            inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (auth unset)."

        if not await probe_memory_endpoint(params.endpoint):
            log.warning(
                "memory backend unreachable — running degraded (MEM-02, D-02)",
                endpoint=params.endpoint,
                project=project,
            )
            inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (backend unreachable)."

        log.info("memory: ach-memory backend active", endpoint=params.endpoint, project=project)
        return True, await fetch_context(params.endpoint, secret, project)

    except Exception as exc:
        log.warning("memory: prepare_ach_memory failed unexpectedly", error=str(exc))
        inc_memory_degraded()
        return False, "## Memory\n\nUnavailable (unexpected error)."


# Boot-static harness text appended to the system prompt (operator contract §2: each
# memory.type owns its own TOOLS_SPEC). The typed-retain contract is stated because
# ach-memory REJECTS a retain that does not meet it — a spec that omits these produces an
# agent whose every retain bounces on validation. Not a hint the model can be argued out of.
TOOLS_SPEC = """\
Memory tools (the harness fills project and scope for you — never pass them):
- `memory_recall(query, tags?)`: search past memories by topic, file or decision.
- `memory_reflect(query)`: synthesize across memories — patterns, not single facts.
- `memory_get_mental_model(model_key)`: read one pre-built summary (ids head ## Memory).
- `memory_list_mental_models()`: list the summaries available to you.
- `memory_retain(content, memory_type, basis, trigger, evidence, tags?)`: save ONE durable,
  independently-correctable claim. ALL of memory_type, basis, trigger and evidence are
  REQUIRED — a retain missing any of them is rejected.
  - memory_type: preference | constraint | decision | convention | fact | gotcha
  - basis: human_explicit (the user said it) | agent_verified (you checked it)
  - trigger: user_requested | agent_proactive
  - evidence: 1-4 excerpts, each {kind, raw, source_ref?} with
    kind: user_quote | tool_result | artifact_excerpt
Write content in English whatever language the conversation is in — retrieval reranks in
English only. Content is capped at 4 KiB and secrets are rejected. Retain durable insights
(decisions, conventions, recurring bugs, gotchas), never transient chatter about this task.
Working across several repositories? Tag them, e.g. tags=["repo:<group>/<name>"] — one bank
holds all of your memory."""

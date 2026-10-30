# SPDX-License-Identifier: MIT
"""Ach-memory backend: project/endpoint/auth resolution and the one harness -> ach-memory
MCP seam, built on the real `mcp` SDK session (`facades/_mcp_session.py`). Adapted from
old-ach-agent `memory/ach_memory.py`.

Namespace is always the configured, nonempty agent namespace passed in by the caller --
never ambient `POD_NAMESPACE` (a boot-time value, not read from the process environment
here).
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable

import httpx

from ach_runtime.config.schema import AchMemoryAuth, AchMemoryParams
from ach_runtime.facades._mcp_session import mcp_session
from ach_runtime.facades._proxy import ProcessIdentity, with_identity_headers

log = logging.getLogger(__name__)

# Canonical ach-memory MCP tool names (as advertised by the real backend).
ACH_MEMORY_RECALL = "recall"
ACH_MEMORY_REFLECT = "reflect"
ACH_MEMORY_RETAIN = "retain"
ACH_MEMORY_GET_MENTAL_MODEL = "get_mental_model"
ACH_MEMORY_LIST_MENTAL_MODELS = "list_mental_models"
ACH_MEMORY_LOAD_CONTEXT = "load_context"

# ---------------------------------------------------------------------------
# Pure config-resolution helpers (legacy memory/ach_memory.py)
# ---------------------------------------------------------------------------

_NON_SLUG = re.compile(r"[^a-z0-9.-]+")
MAX_SLUG_LENGTH = 128


def normalize_slug(raw: str) -> str:
    """Lowercase, collapse non-slug runs to '-', trim, cap. '' when nothing survives
    (mirrors ach-memory's own `normalize_slug` so both ends agree on one string)."""
    slug = _NON_SLUG.sub("-", raw.strip().lower()).strip("-.")[:MAX_SLUG_LENGTH]
    return slug if any(c.isalnum() for c in slug) else ""


def resolve_project(params: AchMemoryParams, agent_name: str, *, namespace: str) -> str:
    """The agent's memory project slug: explicit override, else `{namespace}-{agent_name}`
    (or bare `agent_name` with no namespace). Boot-static, never Workspace/session/event/
    repo-derived (legacy `resolve_project`, namespace made an explicit input)."""
    if params.project:
        return normalize_slug(params.project)
    return normalize_slug(f"{namespace}-{agent_name}" if namespace else agent_name)


def resolve_endpoint(*, endpoint: str, mcp_server_id: str, mcp_servers: dict[str, str]) -> str:
    """The configured endpoint, else the hydrated server named by `mcpServerId`, else ''
    -- never a guessed URL (legacy `resolve_endpoint`; config already enforces exactly one
    of `endpoint`/`mcpServerId` -- `config/schema.py::AchMemoryParams`)."""
    if endpoint:
        return endpoint
    return mcp_servers.get(mcp_server_id, "")


def _endpoints_equivalent(a: str, b: str) -> bool:
    """Trailing-slash-equivalent only -- never a host/alias rewrite (exclusion comparison
    ignores a trailing `/`; the SDK's configured endpoint is not touched anywhere else)."""
    return bool(a) and bool(b) and a.rstrip("/") == b.rstrip("/")


def excluded_mcp_server_ids(
    *, mcp_server_id: str, endpoint: str, mcp_servers: dict[str, str]
) -> set[str]:
    """Server ids the generic MCP facade must exclude so this ach-memory backend is never
    also reachable raw -- including every hydrated id that is a trailing-slash-equivalent
    alias of the one actually selected, by id or by explicit endpoint (the legacy
    `excluded_mcp_server` returned '' for both the explicit-endpoint and the alias case)."""
    if mcp_server_id:
        resolved = mcp_servers.get(mcp_server_id, "")
        ids = {mcp_server_id}
        if resolved:
            ids |= {sid for sid, ep in mcp_servers.items() if _endpoints_equivalent(ep, resolved)}
        return ids
    if endpoint:
        return {sid for sid, ep in mcp_servers.items() if _endpoints_equivalent(ep, endpoint)}
    return set()


AuthSecretResolver = Callable[[str], str | None]


def _default_resolve_secret(env_name: str) -> str | None:
    return os.environ.get(env_name) or None


def resolve_ach_memory_auth(
    auth: AchMemoryAuth | None,
    ek: str | None,
    *,
    resolve_secret: AuthSecretResolver = _default_resolve_secret,
) -> tuple[bool, dict[str, str]]:
    """(ok, headers) for the configured auth mode (legacy `resolve_ach_memory_auth`).

    None -> no auth needed. type=ach -> `x-ach-key: {ek}`, degrades without an ek_.
    type=bearer -> the resolved secret, `Bearer `-prefixed only on `Authorization`
    (where ach-memory's JWT provider looks); degrades when the env alias is unset.
    """
    if auth is None:
        return True, {}
    if auth.type == "ach":
        if not ek:
            return False, {}
        return True, {"x-ach-key": ek}
    secret = resolve_secret(auth.env) if auth.env else None
    if not secret:
        return False, {}
    value = f"Bearer {secret}" if auth.header.lower() == "authorization" else secret
    return True, {auth.header: value}


# A gateway that aggregates several MCP servers namespaces what it advertises: LiteLLM
# serves ach-memory's `recall` as `ach-memory.recall`. '_' is deliberately NOT a separator
# here -- `sync_retain` ends with `_retain`, and treating it as one would silently route a
# `retain` at the tool that writes a whole batch (legacy `_PREFIXED_TOOL`/`advertised_name`).
_PREFIXED_TOOL = re.compile(r"^.+[.\-/](?P<tool>[a-z_]+)$")


def advertised_name(advertised: set[str], tool: str) -> str:
    """What THIS server calls ``tool``. Falls back to the bare name when unsure: an exact
    match always wins; a single unambiguous prefixed candidate is accepted; 0 or 2+ matches
    both fall back to the bare name (0 means try it anyway, 2+ means guessing would call the
    wrong tool with the right arguments, worse than the call failing)."""
    if tool in advertised:
        return tool
    hits = [n for n in advertised if (m := _PREFIXED_TOOL.match(n)) and m["tool"] == tool]
    return hits[0] if len(hits) == 1 else tool


def _describe(exc: BaseException) -> str:
    """`str(exc)` for a normal exception; the leaf causes for an ExceptionGroup (an MCP
    call runs inside a TaskGroup, whose own str() names only how many failures it hides)."""
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    return "; ".join(_describe(sub) for sub in subs)


# ---------------------------------------------------------------------------
# The harness -> ach-memory MCP seam (legacy memory/ach_memory.py)
# ---------------------------------------------------------------------------


async def call_ach_memory(
    endpoint: str,
    headers: dict[str, str],
    tool: str,
    args: dict[str, object],
    *,
    identity: ProcessIdentity,
    auth: httpx.Auth | None = None,
) -> str:
    """Call one ach-memory MCP tool through a fresh initialized session; return the first
    text content ('' if none). `endpoint` is used VERBATIM -- the operator configures the
    complete MCP endpoint, nothing is appended.

    The tool is named by asking the server rather than assuming, since the same ach-memory
    is reachable both directly and behind a gateway that renames what it proxies (see
    `advertised_name`). A tool-level error arrives as a normal result with `isError=True`,
    not an exception -- raised here so every caller degrades loudly instead of the error
    text masquerading as a valid memory or standing-context section (legacy
    `call_ach_memory`).
    """
    async with mcp_session(endpoint, with_identity_headers(headers, identity), auth) as session:
        try:
            wire_name = advertised_name({t.name for t in (await session.list_tools()).tools}, tool)
        except Exception as exc:  # a server that will not enumerate can still dispatch
            log.warning("ach-memory: tools/list failed -- calling by bare name (%s)", _describe(exc))
            wire_name = tool
        result = await session.call_tool(wire_name, args)
        text: str = getattr(result.content[0], "text", "") if result.content else ""
        if getattr(result, "isError", False):
            raise RuntimeError(f"ach-memory tool {tool!r} errored: {text}")
        return text


async def fetch_context(
    endpoint: str, headers: dict[str, str], project: str, *, identity: ProcessIdentity,
    auth: httpx.Auth | None = None,
) -> str:
    """The ``## Memory`` prompt section, from one ``load_context`` call. The service has
    already ordered sections, neutralised forged headings and applied every budget; the
    text is taken VERBATIM. Never raises -- any failure yields the degraded note (legacy
    `fetch_context`, D-02 fail-open)."""
    try:
        raw = await call_ach_memory(
            endpoint, headers, ACH_MEMORY_LOAD_CONTEXT, {"project_slug": project}, identity=identity,
            auth=auth,
        )
        payload = json.loads(raw) if raw else {}
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        if not text:
            return "## Memory\n\nNo standing context yet."
        return f"## Memory\n\n{text}"
    except Exception as exc:
        log.warning("memory: load_context failed -- running degraded (%s)", _describe(exc))
        return "## Memory\n\nUnavailable (context load failed)."


async def prepare_ach_memory(
    endpoint: str, project: str, headers: dict[str, str], *, identity: ProcessIdentity,
    auth: httpx.Auth | None = None,
) -> tuple[bool, str]:
    """Load standing context and build the prompt section. Returns (available, section).
    Never raises (legacy `prepare_ach_memory`, D-02 fail-open). No health probe:
    `load_context` IS the reachability test."""
    if not endpoint:
        return False, "## Memory\n\nUnavailable (no endpoint)."
    try:
        section = await fetch_context(endpoint, headers, project, identity=identity, auth=auth)
        if section.startswith("## Memory\n\nUnavailable"):
            return False, section
        return True, section
    except Exception as exc:
        log.warning("memory: prepare_ach_memory failed unexpectedly (%s)", _describe(exc))
        return False, "## Memory\n\nUnavailable (unexpected error)."


# Boot-static harness text appended to the system prompt (legacy memory/ach_memory.py;
# copied unchanged -- not wired into any prompt/engine assembly here).
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

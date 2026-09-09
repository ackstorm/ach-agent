# SPDX-License-Identifier: Apache-2.0
"""Hindsight memory backend — probe + prompt-inject + MCP-server-config (MEM-01/02, D-01/D-02).

Locked decisions:
  - Fail-open via pre-check (D-02): probe BEFORE pool.acquire() so opencode.json
    includes or excludes memory MCP server BEFORE subprocess launch (RESEARCH.md Pitfall 3).
  - bank_id: use MemoryBlock.bank as bank_id (static, operator config — never from inbound payload).
  - MCP client: mcp.client.streamable_http.streamable_http_client per-call (ackbot pattern).
  - Fail-open: any exception in probe/fetch → degrade, never raise to caller.
  - Metric: MEMORY_DEGRADED counter from router/metrics.py (extended in Phase 4 Plan 01).

RTR-06: no router.* imports used here (only MEMORY_DEGRADED metric is imported at call time).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from ach_agent.engine.mcp_session import mcp_session
from ach_agent.memory.common import (
    inc_memory_degraded,
    probe_memory_endpoint,
    resolve_memory_secret,
)

if TYPE_CHECKING:
    from ach_agent.config.schema import HindsightMemory

log = structlog.get_logger(__name__)

TYPE = "hindsight"

# Tool names on the live Hindsight deployment (verified 2026-07-05). A boot list_tools
# probe (facade) logs the real names so a rename is caught, not silently 404'd.
HINDSIGHT_RECALL = "hindsight_recall"
HINDSIGHT_REFLECT = "hindsight_reflect"
HINDSIGHT_RETAIN = "hindsight_retain"
HINDSIGHT_GET_MENTAL_MODEL = "hindsight_get_mental_model"
HINDSIGHT_CREATE_BANK = "hindsight_create_bank"
HINDSIGHT_CREATE_MENTAL_MODEL = "hindsight_create_mental_model"
HINDSIGHT_REFRESH_MENTAL_MODEL = "hindsight_refresh_mental_model"

_CANONICAL_TOOLS = (
    HINDSIGHT_RECALL,
    HINDSIGHT_REFLECT,
    HINDSIGHT_RETAIN,
    HINDSIGHT_GET_MENTAL_MODEL,
    HINDSIGHT_CREATE_BANK,
    HINDSIGHT_CREATE_MENTAL_MODEL,
    HINDSIGHT_REFRESH_MENTAL_MODEL,
)

# canonical name -> the name the live deployment actually publishes. Populated once at boot by
# init_hindsight_tool_aliases (a tools/list probe); empty = identity (call_hindsight falls back to
# the canonical name). Handles gateways that (un)prefix tools, e.g. `recall` vs `hindsight_recall`.
_TOOL_ALIASES: dict[str, str] = {}


def build_tool_aliases(published: list[str]) -> dict[str, str]:
    """Map each canonical `hindsight_*` tool to the published name (exact/unprefixed/re-prefixed).

    A gateway may publish `hindsight_recall`; the raw service may publish `recall` (or `x_recall`).
    Prefer an exact match, then the bare suffix, then any name ending in `_<suffix>`.
    """
    names = set(published)
    aliases: dict[str, str] = {}
    for canonical in _CANONICAL_TOOLS:
        suffix = canonical.removeprefix("hindsight_")
        if canonical in names:
            aliases[canonical] = canonical
        elif suffix in names:
            aliases[canonical] = suffix
        else:
            match = next((p for p in published if p.endswith("_" + suffix)), None)
            if match is not None:
                aliases[canonical] = match
    return aliases


TOOLS_SPEC = """\
Memory tools (the harness fills the memory bank for you — do NOT pass a bank id):
- `memory_recall(query, tags?)`: search past memories by topic or filename.
- `memory_reflect(query, tags?)`: synthesize across memories — patterns, not single facts.
- `memory_get_mental_model(mental_model_id)`: read a pre-built summary (see ## Memory for ids).
- `memory_retain(content, tags?)`: save an insight for later. Tag it, e.g. tags=["repo:<name>"]."""


def hindsight_auth_headers(secret: str | None) -> dict[str, str]:
    """Admin auth header (assumed Bearer). Empty when no secret — internal/no-auth URL."""
    return {"Authorization": f"Bearer {secret}"} if secret else {}


async def init_hindsight_tool_aliases(endpoint: str, secret: str | None) -> dict[str, str]:
    """Boot-once: list the endpoint's tools and map canonical names → published names.

    Sets the module ``_TOOL_ALIASES`` so every ``call_hindsight`` targets the real name even when
    the deployment (un)prefixes tools. Fail-open: on any error, leaves aliases empty (identity).
    """
    try:
        async with mcp_session(endpoint, hindsight_auth_headers(secret)) as session:
            listed = await session.list_tools()
        published = [t.name for t in listed.tools]
    except Exception as exc:
        log.warning("memory: tool discovery failed — using canonical names", error=str(exc))
        return {}
    aliases = build_tool_aliases(published)
    _TOOL_ALIASES.clear()
    _TOOL_ALIASES.update(aliases)
    remapped = {c: a for c, a in aliases.items() if a != c}
    missing = [c for c in _CANONICAL_TOOLS if c not in aliases]
    log.info(
        "memory: hindsight tools resolved",
        resolved=len(aliases),
        remapped=remapped or None,
        missing=missing or None,
    )
    return aliases


async def call_hindsight(
    endpoint: str, secret: str | None, tool: str, args: dict[str, object]
) -> str:
    """Call one Hindsight MCP tool; return first text content ('' if none).

    The single harness→Hindsight seam (probe/fetch/provision/facade all route here so tests
    monkeypatch one function). ``secret`` (if any) is used only to build headers — never logged.
    ``tool`` is a canonical ``hindsight_*`` name; it is translated through ``_TOOL_ALIASES``
    (populated at boot) to whatever the live deployment publishes.
    """
    async with mcp_session(endpoint, hindsight_auth_headers(secret)) as session:
        actual = _TOOL_ALIASES.get(tool, tool)
        result = await session.call_tool(actual, args)
        text: str = getattr(result.content[0], "text", "") if result.content else ""
        # A tool-level error (e.g. unknown tool / bad bank) comes back as a normal
        # result with isError=True, NOT an exception. Raise so every caller degrades
        # (facade → "unavailable", fetch → skip model, provision → "failed") and logs
        # loud — instead of the error text masquerading as a valid memory/summary.
        if getattr(result, "isError", False):
            raise RuntimeError(f"hindsight tool {tool!r} (as {actual!r}) errored: {text}")
        return text


async def fetch_mental_model_summaries(
    endpoint: str,
    secret: str | None,
    bank_id: str,
    mental_model_ids: list[str],
) -> str:
    """Fetch mental model summaries and return a '## Memory\\n...' section string.

    Routes through the single ``call_hindsight`` seam (admin-authed, corrected tool name).
    Partial failures (single model unreachable): log warning + skip that model, never raise.
    Returns '## Memory\\n\\nUnavailable' if all fetches fail or mental_model_ids is empty.

    ``detail="content"`` is REQUIRED: Hindsight defaults to ``"full"``, which appends the whole
    ``reflect_response`` (every cited fact's text under ``based_on``, plus the refresh trace) to
    each model. Only ``content`` is bounded by the model's ``max_tokens`` — the ``full`` envelope
    is unbounded, and this section is re-fetched and injected on EVERY invocation.
    """
    sections: list[str] = []
    for mid in mental_model_ids:
        try:
            text = await call_hindsight(
                endpoint,
                secret,
                HINDSIGHT_GET_MENTAL_MODEL,
                {"bank_id": bank_id, "mental_model_id": mid, "detail": "content"},
            )
            if text:
                sections.append(f"### {mid}\n{text}")
        except Exception as exc:
            log.warning(
                "memory: mental model fetch failed — skipping",
                model=mid,
                error=str(exc),
            )

    if sections:
        return "## Memory\n\n" + "\n\n".join(sections)
    return "## Memory\n\nUnavailable"


async def prepare_memory(
    memory_cfg: HindsightMemory,
) -> tuple[bool, str]:
    """Probe endpoint and fetch mental-model summaries. Returns (available, prompt_section).

    Call BEFORE pool.acquire() in engine_runner (RESEARCH.md Pitfall 3) so the
    opencode.json written for that server includes or excludes the memory MCP server.

    bank_id = memory_cfg.bank (static, operator config — never from inbound payload).
    T-04-03: bank_id is always from operator config, never from inbound payload.

    Never raises — MEM-02 fail-open contract (D-02).
    On unreachable: increments MEMORY_DEGRADED counter, logs WARN, returns
    (False, unavailable section).
    """
    try:
        params = memory_cfg.hindsight
        bank_id = params.bank

        ok, secret = resolve_memory_secret(params.auth)
        if not ok:
            log.warning("memory: auth configured but env unset — running degraded", bank_id=bank_id)
            inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (auth unset)."

        available = await probe_memory_endpoint(params.endpoint)
        if not available:
            log.warning(
                "memory backend unreachable — running degraded (MEM-02, D-02)",
                endpoint=params.endpoint,
                bank_id=bank_id,
            )
            inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (backend unreachable)."

        log.info("memory: hindsight backend active", endpoint=params.endpoint, bank_id=bank_id)
        prompt_section = await fetch_mental_model_summaries(
            endpoint=params.endpoint,
            secret=secret,
            bank_id=bank_id,
            mental_model_ids=[m.id for m in params.mental_models],
        )
        return True, prompt_section

    except Exception as exc:
        # Catch-all: never propagate exceptions to the caller (MEM-02, D-02 fail-open)
        log.warning(
            "memory: prepare_memory failed unexpectedly — running degraded",
            error=str(exc),
        )
        inc_memory_degraded()
        return False, "## Memory\n\nUnavailable (unexpected error)."


async def provision_memory(memory_cfg: object) -> None:
    """Provision the bank + mental models in Hindsight (boot-once, idempotent, fail-open).

    No-op unless ``memory_cfg`` is a HindsightMemory with a resolvable admin secret (or no
    auth configured, i.e. an internal URL). Never raises — a provisioning failure degrades
    memory, it does not stop boot.
    """
    from ach_agent.config.schema import HindsightMemory

    if not isinstance(memory_cfg, HindsightMemory):
        return
    params = memory_cfg.hindsight
    ok, secret = resolve_memory_secret(params.auth)  # secret may be None (internal URL)
    if not ok:
        log.warning(
            "memory: auth configured but env unset — skipping provisioning", bank_id=params.bank
        )
        return

    # Discover the deployment's real tool names FIRST (a gateway may (un)prefix them); every
    # call_hindsight below — and the per-turn facade/fetch paths — then targets the right name.
    await init_hindsight_tool_aliases(params.endpoint, secret)

    try:
        await call_hindsight(
            params.endpoint,
            secret,
            HINDSIGHT_CREATE_BANK,
            {"bank_id": params.bank, "name": params.bank, "mission": params.mission or None},
        )
        log.info("memory: bank ensured", bank_id=params.bank)
        for spec in params.mental_models:
            try:
                await call_hindsight(
                    params.endpoint,
                    secret,
                    HINDSIGHT_CREATE_MENTAL_MODEL,
                    {
                        "bank_id": params.bank,
                        "name": spec.name,
                        "source_query": spec.source_query,
                        "mental_model_id": spec.id,
                        "max_tokens": spec.max_tokens,
                        "trigger_refresh_after_consolidation": spec.auto_refresh,
                    },
                )
                log.info("memory: mental_model ensured", model=spec.id)
            except Exception as exc:  # one bad model must not abort the rest
                log.warning("memory: create_mental_model failed", model=spec.id, error=str(exc))
        for spec in params.mental_models:
            if spec.auto_refresh:
                try:
                    await call_hindsight(
                        params.endpoint,
                        secret,
                        HINDSIGHT_REFRESH_MENTAL_MODEL,
                        {"bank_id": params.bank, "mental_model_id": spec.id},
                    )
                    log.info("memory: mental_model refresh triggered", model=spec.id)
                except Exception as exc:
                    log.warning(
                        "memory: refresh_mental_model failed", model=spec.id, error=str(exc)
                    )
        log.info(
            "memory: provisioning complete",
            bank_id=params.bank,
            models=len(params.mental_models),
        )
    except Exception as exc:  # ensure_bank failed → degrade, never raise
        log.warning(
            "memory: provisioning failed — running degraded", bank_id=params.bank, error=str(exc)
        )

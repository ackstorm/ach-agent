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

import asyncio
from typing import TYPE_CHECKING

import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

if TYPE_CHECKING:
    from ach_agent.config.schema import HindsightMemory, HindsightParams

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

TOOLS_SPEC = """\
Memory tools (the harness fills the memory bank for you — do NOT pass a bank id):
- `memory_recall(query, tags?)`: search past memories by topic or filename.
- `memory_reflect(query, tags?)`: synthesize across memories — patterns, not single facts.
- `memory_get_mental_model(mental_model_id)`: read a pre-built summary (ids are in the ## Memory section).
- `memory_retain(content, tags?)`: store an insight for future sessions. Tag it (e.g. tags=["repo:<name>"])."""


def hindsight_auth_headers(secret: str | None) -> dict[str, str]:
    """Admin auth header (assumed Bearer). Empty when no secret — internal/no-auth URL."""
    return {"Authorization": f"Bearer {secret}"} if secret else {}


async def call_hindsight(
    endpoint: str, secret: str | None, tool: str, args: dict[str, object]
) -> str:
    """Call one Hindsight MCP tool; return first text content ('' if none).

    The single harness→Hindsight seam (probe/fetch/provision/facade all route here so tests
    monkeypatch one function). ``secret`` (if any) is used only to build headers — never logged.

    NOTE: the installed mcp ``streamable_http_client`` takes no ``headers=`` kwarg; auth is
    injected via a pre-built httpx client (``create_mcp_http_client(headers=...)``, which also
    applies the SDK's recommended MCP timeouts) passed as ``http_client=``. We own that client's
    lifecycle (the transport only closes clients it created), hence the ``async with``.
    """
    headers = hindsight_auth_headers(secret)
    async with create_mcp_http_client(headers=headers or None) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
                if not result.content:
                    return ""
                text: str = getattr(result.content[0], "text", "")
                return text


def resolve_memory_secret(params: HindsightParams) -> tuple[bool, str | None]:
    """(ok, secret) gate reused by prepare/provision/main.

    (True, None)  → no auth configured (internal URL) — proceed unauthenticated.
    (False, None) → auth configured but env unset (misconfig) — caller degrades.
    (True, secret)→ auth resolved — proceed with Bearer.
    """
    from ach_agent.config.schema import resolve_secret

    if params.auth is None:
        return True, None
    secret = resolve_secret(params.auth)
    return (False, None) if secret is None else (True, secret)


async def probe_memory_endpoint(endpoint: str, timeout: float = 2.0) -> bool:
    """Return True if the Hindsight memory endpoint is reachable within timeout.

    Uses aiohttp (direct dependency in pyproject.toml).
    Any exception (network error, timeout, non-2xx/3xx) → returns False, never raises.

    ASSUMPTION A2 (RESEARCH.md): endpoint exposes /health for probing.
    T-04-02/T-04-04: bounded 2s timeout; probe targets only the operator-rendered config URL,
    never user input (SSRF mitigation).
    """
    import aiohttp  # direct dependency

    try:
        async with asyncio.timeout(timeout):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{endpoint}/health") as resp:
                    return resp.status < 500
    except Exception:
        return False


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
    """
    sections: list[str] = []
    for mid in mental_model_ids:
        try:
            text = await call_hindsight(
                endpoint,
                secret,
                HINDSIGHT_GET_MENTAL_MODEL,
                {"bank_id": bank_id, "mental_model_id": mid},
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

        ok, secret = resolve_memory_secret(params)
        if not ok:
            log.warning(
                "memory: auth configured but env unset — running degraded", bank_id=bank_id
            )
            _inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (auth unset)."

        available = await probe_memory_endpoint(params.endpoint)
        if not available:
            log.warning(
                "memory backend unreachable — running degraded (MEM-02, D-02)",
                endpoint=params.endpoint,
                bank_id=bank_id,
            )
            _inc_memory_degraded()
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
        _inc_memory_degraded()
        return False, "## Memory\n\nUnavailable (unexpected error)."


def _inc_memory_degraded() -> None:
    """Increment the MEMORY_DEGRADED counter via lazy import (RTR-06 compliance).

    The counter is declared in router/metrics.py. This function performs a
    deferred import inside the function body — not at module top level — so
    memory/hindsight.py has no top-level 'from ach_agent.router' dependency.
    Silently suppressed on any error (fail-open).
    """
    try:
        # Deferred import: acceptable per RTR-06 (no top-level router.* at module level)
        import importlib  # noqa: PLC0415

        metrics = importlib.import_module("ach_agent.router.metrics")
        metrics.MEMORY_DEGRADED.inc()
    except Exception:
        pass

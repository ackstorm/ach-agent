# SPDX-License-Identifier: Apache-2.0
"""Fail-open memory adapter — probe + prompt-inject + MCP-server-config (MEM-01/02, D-01/D-02).

Locked decisions:
  - Fail-open via pre-check (D-02): probe BEFORE pool.acquire() so opencode.json
    includes or excludes memory MCP server BEFORE subprocess launch (RESEARCH.md Pitfall 3).
  - bank_id: use MemoryBlock.bank as bank_id (static, operator config — never from inbound payload).
  - MCP client: mcp.client.streamable_http.streamable_http_client per-call (ackbot pattern).
  - Fail-open: any exception in probe/fetch → degrade, never raise to caller.
  - Metric: MEMORY_DEGRADED counter from router/metrics.py (extended in Phase 4 Plan 01).

RTR-06: no router.* imports used here (only MEMORY_DEGRADED metric is imported at call time).
No hermes_agent.* imports here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ach_agent.config.schema import MemoryBlock

log = structlog.get_logger(__name__)


async def probe_memory_endpoint(endpoint: str, timeout: float = 2.0) -> bool:
    """Return True if the Hindsight memory endpoint is reachable within timeout.

    Uses aiohttp (transitive dep via Hermes — do not add separately to pyproject.toml).
    Any exception (network error, timeout, non-2xx/3xx) → returns False, never raises.

    ASSUMPTION A2 (RESEARCH.md): endpoint exposes /health for probing.
    T-04-02/T-04-04: bounded 2s timeout; probe targets only the operator-rendered config URL,
    never user input (SSRF mitigation).
    """
    import aiohttp  # transitive via hermes-agent

    try:
        async with asyncio.timeout(timeout):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{endpoint}/health") as resp:
                    return resp.status < 500
    except Exception:
        return False


async def fetch_mental_model_summaries(
    endpoint: str,
    bank_id: str,
    mental_model_ids: list[str],
) -> str:
    """Fetch mental model summaries and return a '## Memory\\n...' section string.

    Per-call mcp.client.streamable_http pattern (clean-room from ackbot hindsight.py).
    Partial failures (single model unreachable): log warning + skip that model, never raise.
    Returns '## Memory\\n\\nUnavailable' if all fetches fail or mental_model_ids is empty.
    """
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    sections: list[str] = []
    for mid in mental_model_ids:
        try:
            async with streamable_http_client(endpoint) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "memory_get_mental_model",
                        {"bank_id": bank_id, "mental_model_id": mid},
                    )
                    text = getattr(result.content[0], "text", "") if result.content else ""
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
    memory_cfg: MemoryBlock,
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
        bank_id = memory_cfg.bank

        available = await probe_memory_endpoint(memory_cfg.endpoint)
        if not available:
            log.warning(
                "memory backend unreachable — running degraded (MEM-02, D-02)",
                endpoint=memory_cfg.endpoint,
                bank_id=bank_id,
            )
            _inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (backend unreachable)."

        prompt_section = await fetch_mental_model_summaries(
            endpoint=memory_cfg.endpoint,
            bank_id=bank_id,
            mental_model_ids=memory_cfg.mental_models,
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
    memory/adapter.py has no top-level 'from ach_agent.router' dependency.
    Silently suppressed on any error (fail-open).
    """
    try:
        # Deferred import: acceptable per RTR-06 (no top-level router.* at module level)
        import importlib  # noqa: PLC0415

        metrics = importlib.import_module("ach_agent.router.metrics")
        metrics.MEMORY_DEGRADED.inc()
    except Exception:
        pass

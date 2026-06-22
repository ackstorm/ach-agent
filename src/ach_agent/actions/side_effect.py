# SPDX-License-Identifier: Apache-2.0
"""SideEffectExecutor Protocol + DryRunSideEffectExecutor (ACT-03, D-01, D-02).

Named Protocol seam for sideEffect action execution.

ACT-03: sideEffect execution is governed by the consent gate in dispatch_actions;
the executor is only called when consent passes.

D-01: v1 execution is dry-run / no-op — the DryRunSideEffectExecutor logs the
intended mutation summary and returns it, but performs NO real external API call.
Do NOT add aiohttp, requests, or any network call here.

D-02: Real mutation, rollback/compensation, external authorization, sideEffect-level
idempotency, and a richer auto-tier policy engine are ALL DEFERRED and not built here.
The Protocol seam exists so a real executor can be swapped in without callsite changes.

Constraint: NEVER import from hermes_agent.* or engine.* or router.* here (RTR-06).
"""

from __future__ import annotations

from typing import Any, Protocol

import structlog

log = structlog.get_logger(__name__)


class SideEffectExecutor(Protocol):
    """Execute a sideEffect action (ACT-03, D-01).

    v1 implementation: DryRunSideEffectExecutor (no external call).
    Future: real executor that calls the external API, handles rollback, etc. (D-02).
    """

    async def execute(self, action: dict[str, Any], context: dict[str, Any]) -> str:
        """Execute the sideEffect action and return a human-readable summary.

        Args:
            action: Engine-emitted sideEffect action dict (name, kind, input).
            context: Delivery context (project_id, mr_iid, etc.) — opaque to executor.

        Returns:
            Human-readable summary string describing what was (or would have been) done.
        """
        ...


class DryRunSideEffectExecutor:
    """Dry-run sideEffect executor — no-op v1 implementation (D-01).

    Logs the intended mutation and returns a summary string.
    Never calls any real external API (anti-pattern: no aiohttp/requests/http here).

    Implements SideEffectExecutor Protocol structurally.
    """

    async def execute(self, action: dict[str, Any], context: dict[str, Any]) -> str:
        """Build and log the intended-mutation summary; return it. No real external call.

        Args:
            action: Engine-emitted sideEffect action dict.
            context: Delivery context (ignored in v1 dry-run).

        Returns:
            DRY-RUN summary string.
        """
        name = action.get("name", "<unnamed>")
        kind = action.get("kind", "sideEffect")
        summary = f"DRY-RUN: would execute {name} ({kind})"
        log.info(
            "sideeffect.dryrun",
            action_name=name,
            action_kind=kind,
            summary=summary,
        )
        return summary

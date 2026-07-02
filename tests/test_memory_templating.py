# SPDX-License-Identifier: Apache-2.0
"""Task 3: template codemem.project + hindsight.bank from the triggering event.

Tests that engine_runner renders {{ internal.session.key }} into codemem_project
on the EngineConfig passed to pool.acquire, and that a literal project passes through.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.tui import _CONSOLE_SESSION_KEY
from ach_agent.config.schema import CodememMemory, CodememParams
from ach_agent.engine.lifecycle import EngineConfig
from ach_agent.main import _make_engine_runner
from ach_agent.templating import build_template_context, render_template


class _CapturingPool:
    """Pool that records the EngineConfig passed to acquire."""

    def __init__(self) -> None:
        self.acquired_cfgs: list[EngineConfig] = []
        self.oc_sessions: dict[str, str] = {}

    async def acquire(self, _session_key: str, cfg: Any) -> Any:
        self.acquired_cfgs.append(cfg)
        return object()  # fake server

    async def release(self, _session_key: str, ttl_seconds: float = 0.0) -> None:
        return None


def _make_event(session_key: str) -> MessageEvent:
    return MessageEvent(
        idempotency_key="test-k",
        session_key=session_key,
        channel_name="gitlab-mr",
        payload={"object_attributes": {"title": "T"}},
        delivery_context={},
        source_trait="async_no_retry",
    )


@pytest.mark.asyncio
async def test_codemem_project_template_rendered_into_acquire_cfg() -> None:
    """engine_runner renders {{ internal.session.key }} into codemem_project before pool.acquire."""
    import ach_agent.engine.lifecycle as lifecycle

    async def _fake_run(**_kw: Any) -> dict[str, Any]:
        return {"action": "none", "text": ""}

    pool = _CapturingPool()
    memory_cfg = CodememMemory(
        type="codemem",
        codemem=CodememParams(project="{{ internal.session.key }}"),
    )
    base_cfg = EngineConfig(codemem_project="{{ internal.session.key }}")

    with patch.object(lifecycle, "run_invocation", new=AsyncMock(side_effect=_fake_run)):
        runner = _make_engine_runner(
            pool=pool,
            engine_cfg=base_cfg,
            max_invocation_seconds=30,
            memory_cfg=memory_cfg,
        )
        await runner(_make_event("gitlab.com/g/repo"), lambda: None)

    assert pool.acquired_cfgs, "acquire must have been called"
    assert pool.acquired_cfgs[0].codemem_project == "gitlab.com/g/repo", (
        f"expected rendered project, got {pool.acquired_cfgs[0].codemem_project!r}"
    )


@pytest.mark.asyncio
async def test_codemem_project_literal_passes_through() -> None:
    """engine_runner leaves a literal codemem_project unchanged (no {{ token)."""
    import ach_agent.engine.lifecycle as lifecycle

    async def _fake_run(**_kw: Any) -> dict[str, Any]:
        return {"action": "none", "text": ""}

    pool = _CapturingPool()
    memory_cfg = CodememMemory(type="codemem", codemem=CodememParams(project="ach-agent"))
    base_cfg = EngineConfig(codemem_project="ach-agent")

    with patch.object(lifecycle, "run_invocation", new=AsyncMock(side_effect=_fake_run)):
        runner = _make_engine_runner(
            pool=pool,
            engine_cfg=base_cfg,
            max_invocation_seconds=30,
            memory_cfg=memory_cfg,
        )
        await runner(_make_event("some-session"), lambda: None)

    assert pool.acquired_cfgs[0].codemem_project == "ach-agent"


# ---------------------------------------------------------------------------
# Console pre-warm render (main.py pre-warm block)
# ---------------------------------------------------------------------------
# These tests exercise the actual build_template_context + render_template
# functions used in the pre-warm fix, with the exact console-specific args.
# Residual gap: the isinstance(cfg.memory, CodememMemory) guard and the
# main() pre-warm block itself are not covered (invoking main() end-to-end
# requires mocking config file, ek_, proxies, and opencode — disproportionate).


def test_console_prewarm_template_renders_session_key() -> None:
    """Pre-warm ctx with _CONSOLE_SESSION_KEY renders {{ internal.session.key }} correctly."""
    ctx = build_template_context(
        {},
        channel_name="tui",
        channel_type="tui",
        channel_source="",
        agent_name="test-agent",
        memory_bank="",
        event_id="",
        session_key=_CONSOLE_SESSION_KEY,
    )
    result = render_template("{{ internal.session.key }}", ctx)
    assert result == _CONSOLE_SESSION_KEY, (
        f"expected {_CONSOLE_SESSION_KEY!r}, got {result!r}"
    )


def test_console_prewarm_literal_project_unchanged() -> None:
    """A literal codemem.project (no {{ token) passes through the pre-warm render unchanged."""
    ctx = build_template_context(
        {},
        channel_name="tui",
        channel_type="tui",
        channel_source="",
        agent_name="test-agent",
        memory_bank="",
        event_id="",
        session_key=_CONSOLE_SESSION_KEY,
    )
    result = render_template("ach-agent", ctx)
    assert result == "ach-agent", f"literal project must not change, got {result!r}"

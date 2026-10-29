# SPDX-License-Identifier: MIT
"""Focused native adapter integration (contract §4, task-4-reuse-handoff.md steps 1-7) —
runs the pinned real @opencode/cli@2.0.20 binary against the Task 1 spike's proven fake
model+MCP fixture (tests/engine/fake_facades.py), not an unreachable HTTPS facade — review
finding evidence requirement: deterministic receipt/readiness/cancel/historical-terminal
cases, not fast-failure-by-accident. Not a re-run of Task 1's 121-case audit."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine.opencode import EngineInvocation, OpenCodeEngine
from fake_facades import fake_facades

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


@pytest.fixture
def public():
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config = RuntimeConfig.model_validate(wire)
    return public_config(config, engine_env=config.engine.env)


@pytest.fixture
async def engine():
    instance = OpenCodeEngine()
    try:
        yield instance
    finally:
        for session in list(instance._sessions.values()):
            await session._kill(confirm_timeout=5.0)


def _invocation(public, tmp_path: Path, *, invocation_id: str, session_ref: str, model_url: str, mcp: dict | None = None):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(exist_ok=True)
    return EngineInvocation(
        invocation_id=invocation_id,
        session_ref=session_ref,
        workspace_dir=workspace_dir,
        prompt="reply with the single word: pong",
        public=public,
        engine_env=dict(public.engine.env),
        mcp=mcp,
        provider_base_url_override=model_url,
    )












async def _first_request_tool_names(public, tmp_path: Path) -> set[str]:
    tmp_path.mkdir()
    engine = OpenCodeEngine()
    async with fake_facades("text") as fakes:
        invocation = _invocation(
            public, tmp_path, invocation_id="deny-1", session_ref="deny-session-1", model_url=fakes.model_url
        )
        try:
            await engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")
            assert await asyncio.wait_for(engine.wait_settled("deny-1"), timeout=15.0) == "completed"
            captured = await fakes.control({"command": "capture"})
        finally:
            for session in list(engine._sessions.values()):
                await session._kill(confirm_timeout=5.0)
    body = captured["records"][0]["body"]
    return {d["name"] for group in body["tools"] for d in group["functionDeclarations"]}



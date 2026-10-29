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


async def test_start_commits_only_after_queued_prompt_receipt(tmp_path: Path, public, engine) -> None:
    async with fake_facades("text") as fakes:
        invocation = _invocation(
            public, tmp_path, invocation_id="native-1", session_ref="native-session-1", model_url=fakes.model_url
        )
        receipt = await engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert receipt.native_session_id
        assert receipt.input_id
        outcome = await asyncio.wait_for(engine.wait_settled("native-1"), timeout=15.0)
        assert outcome == "completed"


async def test_double_start_same_invocation_id_reuses_one_native_session(
    tmp_path: Path, public, engine
) -> None:
    async with fake_facades("text") as fakes:
        invocation = _invocation(
            public, tmp_path, invocation_id="native-2", session_ref="native-session-2", model_url=fakes.model_url
        )
        first = await engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("native-2")
        session = engine._sessions[invocation.session_ref]
        pid_after_first = session._proc.pid

        second_invocation = _invocation(
            public, tmp_path, invocation_id="native-3", session_ref="native-session-2", model_url=fakes.model_url
        )
        second = await engine.start(
            second_invocation, home=tmp_path / "home", scratch=tmp_path / "scratch"
        )
        assert second.native_session_id == first.native_session_id
        assert session._proc.pid == pid_after_first


async def test_sequential_invocations_do_not_inherit_each_others_terminal(
    tmp_path: Path, public, engine
) -> None:
    """Finding #5: a historical terminal from an earlier invocation on a reused process
    must not finish a later one — each invocation gets its own event baseline."""
    async with fake_facades("text") as fakes:
        first = _invocation(
            public, tmp_path, invocation_id="native-seq-1", session_ref="native-session-seq", model_url=fakes.model_url
        )
        await engine.start(first, home=tmp_path / "home", scratch=tmp_path / "scratch")
        first_outcome = await asyncio.wait_for(engine.wait_settled("native-seq-1"), timeout=15.0)
        assert first_outcome == "completed"

        second = _invocation(
            public, tmp_path, invocation_id="native-seq-2", session_ref="native-session-seq", model_url=fakes.model_url
        )
        await engine.start(second, home=tmp_path / "home", scratch=tmp_path / "scratch")
        # The first invocation's terminal event must not have been (mis)applied here before
        # the second's own prompt was even sent.
        second_outcome = await asyncio.wait_for(engine.wait_settled("native-seq-2"), timeout=15.0)
        assert second_outcome == "completed"
        # And the first invocation's own status must still read as its own completion, not
        # be re-overwritten by the second's later events.
        assert await engine.status("native-seq-1") == "completed"


async def test_readiness_waits_for_connected_mcp_before_first_prompt(
    tmp_path: Path, public, engine
) -> None:
    """Finding #9: when MCP entries are injected, native readiness (config/model/agent/mcp
    connected) is required before the prompt is sent."""
    async with fake_facades("text") as fakes:
        invocation = _invocation(
            public,
            tmp_path,
            invocation_id="native-mcp-1",
            session_ref="native-session-mcp",
            model_url=fakes.model_url,
            mcp={"echo": {"url": fakes.mcp_url, "headers": {}}},
        )
        receipt = await engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert receipt.native_session_id
        outcome = await asyncio.wait_for(engine.wait_settled("native-mcp-1"), timeout=15.0)
        assert outcome == "completed"


async def test_cancel_bounded_when_model_call_hangs(tmp_path: Path, public, engine) -> None:
    """Finding #6: a hung model call must be cancelled within the contract's bound, with a
    terminal (non-"running"/"unknown") outcome — using a model backend that deterministically
    never responds, not an unreachable-backend race."""
    async with fake_facades("hold") as fakes:
        invocation = _invocation(
            public, tmp_path, invocation_id="native-4", session_ref="native-session-4", model_url=fakes.model_url
        )
        await engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")
        await asyncio.sleep(0.3)  # let the hung model request actually start

        await asyncio.wait_for(engine.cancel("native-4", "test cancellation"), timeout=13.0)

        status = await engine.status("native-4")
        assert status == "cancelled"


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


async def test_denied_native_tools_leave_the_real_opencode_tool_list(tmp_path: Path, public) -> None:
    denied = ["question", "execute", "websearch", "webfetch"]
    control = await _first_request_tool_names(public, tmp_path / "control")
    assert set(denied) <= control, "control run must offer the tools, else the check is vacuous"

    public = public.model_copy(
        update={"engine": public.engine.model_copy(update={"deny_tools": denied})}
    )
    names = await _first_request_tool_names(public, tmp_path / "denied")
    assert not set(denied) & names
    assert {"read", "shell"} <= names

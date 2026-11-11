# SPDX-License-Identifier: Apache-2.0
"""Contract of the pi engine: every case runs on PiG and on upstream pi."""

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ach_runtime.engine import pi as pi_module
from ach_runtime.engine.base import NativeLaunchFailed, _HaltBudget
from ach_runtime.engine.pi import PiEngine
from ach_runtime.engine.pi_config import agent_dir
from ach_runtime.execution.service import ExecutionService
from fake_facades import fake_facades
from pi_helpers import _invocation, binary, engine  # noqa: F401


async def test_turn_completes_with_result_and_usage(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        receipt = await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert receipt.native_session_id
        assert await engine.wait_settled("inv-1") == "completed"
        assert engine.result("inv-1") == "pong"
        usage = engine.usage("inv-1")
        assert usage.source == "pi" and usage.steps == 1 and usage.reasoning is None


async def test_max_steps_aborts_as_failed_with_no_result(tmp_path, engine) -> None:
    async with fake_facades("tool-loop") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url, max_steps=3), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert await engine.wait_settled("inv-1") == "failed"
        assert engine.result("inv-1") is None
        # soft limit: the abort races the turn loop; a real model's latency narrows the overshoot
        assert 3 <= engine.usage("inv-1").steps <= 3 + 2


async def test_each_step_and_tool_call_is_logged_with_its_invocation(tmp_path, engine, caplog) -> None:
    caplog.set_level(logging.INFO, logger="ach_runtime.observability.runs")
    async with fake_facades("tool-loop") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url, max_steps=2), home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("inv-1")
    steps = [r for r in caplog.records if r.getMessage() == "run step"]
    tools = [r for r in caplog.records if r.getMessage() == "run tool"]
    assert steps and tools
    step = steps[0]
    assert step.invocation_id == "inv-1" and step.outcome == "toolUse"
    assert (step.usage_input, step.usage_output) == (100, 10)
    # The fake model waits 200 ms before its first byte: that wait is time to first token.
    assert step.duration_ms >= step.ttft_ms >= 150
    tool = tools[0]
    assert (tool.invocation_id, tool.tool, tool.outcome) == ("inv-1", "bash", "success")
    assert tool.duration_ms >= 0


async def test_provider_error_fails(tmp_path, engine) -> None:
    async with fake_facades("model-error") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert await engine.wait_settled("inv-1") == "failed"


async def test_mcp_tools_reach_the_model(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        mcp = {"demo": {"url": fakes.mcp_url, "headers": {}}}
        await engine.start(_invocation(tmp_path, fakes.model_url, mcp=mcp), home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("inv-1")
        records = (await fakes.control({"command": "capture"}))["records"]
        names = [tool["function"]["name"] for tool in records[0]["body"]["tools"]]
        assert any(name.startswith("mcp__demo__") for name in names), names


async def test_model_and_mcp_requests_carry_the_session_baggage(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        mcp = {"demo": {"url": fakes.mcp_url, "headers": {}}}
        await engine.start(_invocation(tmp_path, fakes.model_url, mcp=mcp), home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("inv-1")
        capture = await fakes.control({"command": "capture"})
    expected = "session.id=session-1"
    assert capture["records"] and {record["baggage"] for record in capture["records"]} == {expected}
    assert capture["mcpBaggage"] and set(capture["mcpBaggage"]) == {expected}


async def test_planted_system_prompt_files_never_reach_the_model(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        invocation = _invocation(tmp_path, fakes.model_url)
        invocation.public = invocation.public.model_copy(update={"prompt": None})
        directory = agent_dir(tmp_path / "home")
        directory.mkdir(parents=True)
        (directory / "SYSTEM.md").write_text("PLANTED", encoding="utf-8")
        (directory / "APPEND_SYSTEM.md").write_text("PLANTED", encoding="utf-8")
        await engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("inv-1")
        records = (await fakes.control({"command": "capture"}))["records"]
        assert "PLANTED" not in json.dumps(records[0]["body"])


async def test_progress_advances_during_a_turn(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        engine._state("inv-1").last_progress_at = 0.0
        await engine.wait_settled("inv-1")
        assert engine.last_progress("inv-1") > 0.0


async def test_missing_binary_names_the_engine(tmp_path, engine, monkeypatch) -> None:
    monkeypatch.setattr(pi_module, "PI_BIN", "/nonexistent/pi")
    with pytest.raises(NativeLaunchFailed, match="pi engine"):
        await engine.start(_invocation(tmp_path, "http://127.0.0.1:9/v1"), home=tmp_path / "home", scratch=tmp_path / "scratch")


FAKE_PI = Path(__file__).parent / "fixtures" / "fake_pi.py"


@pytest.fixture
async def scripted(tmp_path, monkeypatch):
    """A PiEngine on the scripted fake pi, and `launch(mode)`, which starts a run that misbehaves as `mode`."""
    binary = tmp_path / "fake-pi"
    shutil.copy(FAKE_PI, binary)
    binary.chmod(0o755)
    monkeypatch.setattr(pi_module, "PI_BIN", str(binary))
    instance = PiEngine()

    async def launch(mode: str, invocation_id: str = "inv-1"):
        invocation = _invocation(tmp_path, "http://127.0.0.1:9/v1", invocation_id=invocation_id)
        real_env = pi_module.build_pi_env
        monkeypatch.setattr(
            pi_module, "build_pi_env",
            lambda **kwargs: {**real_env(**kwargs), "FAKE_MODE": mode, "FAKE_MODEL": invocation.public.model.name},
        )
        return await instance.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")

    yield instance, launch
    await instance.close()


async def test_unreadable_stdout_settles_failed(scripted) -> None:
    engine, launch = scripted
    await launch("non-json")
    assert await asyncio.wait_for(engine.wait_settled("inv-1"), 10) == "failed"


async def test_crash_after_settled_is_not_reported_completed(scripted) -> None:
    engine, launch = scripted
    await launch("die-after-settled")
    assert await asyncio.wait_for(engine.wait_settled("inv-1"), 10) == "failed"
    assert engine.result("inv-1") is None


async def test_unusable_stats_still_settle(scripted) -> None:
    engine, launch = scripted
    await launch("null-tokens")
    assert await asyncio.wait_for(engine.wait_settled("inv-1"), 10) == "completed"
    assert engine.usage("inv-1") is None


async def test_lost_stdin_is_a_native_launch_failure(scripted) -> None:
    engine, launch = scripted
    with pytest.raises(NativeLaunchFailed):
        await launch("stdin-closed-before-prompt")
    assert not engine._sessions["session-1"]._pending


async def test_service_cleanup_of_a_hung_pi_ends_timed_out_and_releases_the_session(scripted) -> None:
    engine, launch = scripted
    await launch("hang")
    now = time.monotonic()
    budget = _HaltBudget(cleanup_started_at=now, force_deadline=now + 1.0)
    inv = SimpleNamespace(
        request=SimpleNamespace(invocation_id="inv-1"), cleanup_budget=budget, state="running",
        error=None, terminal_at=None, engine_cancel_task=None, owner_stop_task=None,
    )
    released = []
    service = SimpleNamespace(_engine=engine, _retain_reporting=lambda _inv: None, _release_session_lock=released.append)

    async def join() -> None:
        return None

    await ExecutionService._confirm_halt_and_release(service, inv, "deadline", "timed_out", join=join)
    assert (inv.state, bool(released)) == ("timed_out", True), inv.error
    assert time.monotonic() - now < 1.5
    assert await engine.status("inv-1") == "cancelled"
    assert not engine._sessions["session-1"].alive


async def test_an_old_reader_finishing_after_the_relaunch_leaves_the_new_turn_alone(scripted) -> None:
    engine, launch = scripted
    await launch("hang")
    session = engine._sessions["session-1"]
    old = session._proc
    release = asyncio.Event()
    wait = old.wait

    async def held_wait() -> int:  # holds the old reader in its `finally`, past the relaunch
        await release.wait()
        return await wait()

    old.wait = held_wait
    os.killpg(old.pid, signal.SIGKILL)
    deadline = time.monotonic() + 5
    while session.alive:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    await launch("hang", "inv-2")
    assert session._proc is not old
    release.set()
    await asyncio.sleep(0.5)  # the old reader runs its exit handling
    assert await engine.status("inv-2") == "running"
    assert session.running

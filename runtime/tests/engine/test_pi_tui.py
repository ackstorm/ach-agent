# SPDX-License-Identifier: MIT
"""The TUI takeover of a warmed-up pi session, on PiG and on upstream pi."""

import asyncio
import shlex
import shutil
import subprocess
import time

import pytest

from fake_facades import fake_facades
from pi_helpers import _invocation, binary, engine  # noqa: F401


async def test_pi_tui_command_waits_for_the_warm_up_then_stops_the_rpc_process(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("inv-1")
        session = engine._sessions["session-1"]
        command = await engine.tui_command("session-1")
        assert not session.alive
        assert command.clean_env is True
        assert command.args[-2:] == ["--session", session.sid]
        assert "--mode" not in command.args
        assert command.env["PI_CODING_AGENT_DIR"].endswith(".pi/agent")


async def test_pi_tui_command_is_none_while_a_turn_runs(tmp_path, engine) -> None:
    async with fake_facades("hold") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert await engine.tui_command("session-1") is None
        await engine.cancel("inv-1", "test")


async def test_pi_tui_command_is_none_after_a_failed_turn_and_leaves_the_process_running(tmp_path, engine) -> None:
    async with fake_facades("model-error") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert await engine.wait_settled("inv-1") == "failed"
        session = engine._sessions["session-1"]
        assert await engine.tui_command("session-1") is None
        assert session.alive


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
async def test_pi_tui_opens_on_the_warmed_up_session_and_quits_cleanly(tmp_path, engine, binary) -> None:
    sock, exit_file = str(tmp_path / "tmux.sock"), tmp_path / "tui.exit"

    def tmux(*args: str) -> str:
        return subprocess.run(["tmux", "-S", sock, *args], capture_output=True, text=True, timeout=10).stdout

    async def until(predicate, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            await asyncio.sleep(0.2)
        return False

    async with fake_facades("text") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("inv-1")
        command = await engine.tui_command("session-1")
        env = " ".join(f"{k}={shlex.quote(v)}" for k, v in command.env.items())
        argv = " ".join(shlex.quote(a) for a in [*command.program, *command.args])
        workspace = tmp_path / "workspace"
        try:
            tmux("new-session", "-d", "-x", "120", "-y", "40", "-c", str(workspace),
                 f"env -i {env} {argv}; echo $? > {shlex.quote(str(exit_file))}")
            assert await until(lambda: "pong" in tmux("capture-pane", "-p"), 15), tmux("capture-pane", "-p")
            await asyncio.sleep(1)
            tmux("send-keys", "-l", "hello from tui")
            await asyncio.sleep(0.7)
            tmux("send-keys", "Enter")
            assert await until(lambda: tmux("capture-pane", "-p").count("pong") >= 2, 15), tmux("capture-pane", "-p")
            tmux("send-keys", "C-d")
            assert await until(exit_file.exists, 10)
            assert exit_file.read_text().strip() == "0"
        finally:
            tmux("kill-server")

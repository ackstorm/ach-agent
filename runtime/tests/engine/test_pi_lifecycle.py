# SPDX-License-Identifier: Apache-2.0
"""Cancel, crash, suspend, snapshot and restore of the pi engine: every case but the takeover runs on
PiG and on upstream pi."""

import asyncio
import contextlib
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from ach_runtime.engine import pi as pi_module
from ach_runtime.engine.base import RestoreMismatch, SnapshotManifest, _HaltBudget
from ach_runtime.engine.pi import PiEngine, _PiInvocation
from fake_facades import fake_facades
from pi_helpers import _invocation, _public, binary, engine  # noqa: F401


async def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10.0
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(f"{path} did not appear within 10 seconds")
        await asyncio.sleep(0.05)


async def _start(engine: PiEngine, tmp_path: Path, model_url: str, invocation_id: str = "inv-1") -> None:
    await engine.start(
        _invocation(tmp_path, model_url, invocation_id=invocation_id),
        home=tmp_path / "home", scratch=tmp_path / "scratch",
    )


def _tools() -> list[int]:
    """PIDs of the live `sleep 60` that the tool-hang scenario's tool runs."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
            raw = (entry / "stat").read_bytes()
        except OSError:
            continue
        if command == "sleep 60" and raw[raw.rfind(b")") + 2:].split()[0] != b"Z":
            found.append(int(entry.name))
    return found


def _remove_tools() -> None:
    for pid in _tools():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@pytest.fixture(autouse=True)
def _no_leftover_tools():
    """Tools run in groups of their own, so a failing case could leak one into the next."""
    _remove_tools()
    yield
    _remove_tools()


async def _wait_for_tool(tmp_path: Path) -> None:
    """The tool is running and pi's observer has had time to record it."""
    await _wait_for(tmp_path / "workspace" / "marker.txt")
    deadline = time.monotonic() + 10.0
    while not _tools():
        if time.monotonic() > deadline:
            raise TimeoutError("the tool did not start within 10 seconds")
        await asyncio.sleep(0.05)
    await asyncio.sleep(0.2)


def _crash(session) -> None:
    """Kills pi as a crash would: PiG's launcher spawns a native binary in the same process group."""
    os.killpg(session._proc.pid, signal.SIGKILL)


async def test_cancel_during_a_turn(tmp_path, engine) -> None:
    async with fake_facades("hold") as fakes:
        await _start(engine, tmp_path, fakes.model_url)
        await engine.cancel("inv-1", "operator request")
        assert await engine.wait_settled("inv-1") == "cancelled"


async def test_process_death_fails_the_run_and_the_next_one_resumes(tmp_path, engine) -> None:
    async with fake_facades("tool-hang") as fakes:
        await _start(engine, tmp_path, fakes.model_url)
        session = engine._sessions["session-1"]
        sid = session.sid
        await _wait_for_tool(tmp_path)
        _crash(session)
        assert await engine.wait_settled("inv-1") == "failed"
        assert not _tools()

        await _start(engine, tmp_path, fakes.model_url, "inv-2")
        assert await engine.wait_settled("inv-2") == "completed"
        assert session.sid == sid


async def test_crash_during_a_tool_does_not_rerun_it(tmp_path, engine) -> None:
    async with fake_facades("tool-hang") as fakes:
        await _start(engine, tmp_path, fakes.model_url)
        session = engine._sessions["session-1"]
        marker = tmp_path / "workspace" / "marker.txt"
        await _wait_for_tool(tmp_path)
        _crash(session)
        await engine.wait_settled("inv-1")
        assert not _tools()

        await _start(engine, tmp_path, fakes.model_url, "inv-2")
        await engine.wait_settled("inv-2")
        assert marker.read_text().count("ran") == 1
        records = (await fakes.control({"command": "capture"}))["records"]
        assert len(records[-1]["body"]["messages"]) > 2  # history reached the model


async def _snapshot(engine: PiEngine, tmp_path: Path, model_url: str) -> SnapshotManifest:
    await _start(engine, tmp_path, model_url)
    assert await engine.wait_settled("inv-1") == "completed"
    assert await engine.suspend("session-1")
    return await engine.snapshot(
        "session-1", agent={"name": "a"}, workspace_ref="ws", registry={"workspace_path": str(tmp_path / "workspace")},
    )


def _expected(tmp_path: Path, family: str = "pi") -> dict[str, str]:
    return {
        "engine_family": family, "workspace_ref": "ws", "session_ref": "session-1",
        "workspace_path": str(tmp_path / "workspace"),
    }


async def _restore(engine: PiEngine, tmp_path: Path, manifest: SnapshotManifest, family: str = "pi") -> None:
    public = _public()
    await engine.restore(
        "session-1", manifest, expected=_expected(tmp_path, family), public=public, engine_env=dict(public.engine.env),
        home=tmp_path / "home", scratch=tmp_path / "scratch", in_place=True,
    )


async def test_suspend_then_snapshot_then_restore_resumes_the_session(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        manifest = await _snapshot(engine, tmp_path, fakes.model_url)
        assert manifest.engine_family == "pi"
        restored = PiEngine()
        try:
            await _restore(restored, tmp_path, manifest)
            assert restored._sessions["session-1"].sid == manifest.native_session_id
            await _start(restored, tmp_path, fakes.model_url, "inv-2")
            assert await restored.wait_settled("inv-2") == "completed"
            assert restored._sessions["session-1"].sid == manifest.native_session_id
        finally:
            await restored.close()


async def test_restore_with_an_unknown_session_raises_mismatch(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        manifest = await _snapshot(engine, tmp_path, fakes.model_url)
        manifest.native_session_id = "no-such-session"
        restored = PiEngine()
        try:
            with pytest.raises(RestoreMismatch):
                await _restore(restored, tmp_path, manifest)
            assert not restored._sessions["session-1"].alive
        finally:
            await restored.close()


async def test_restore_of_an_opencode_manifest_raises_mismatch(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        manifest = await _snapshot(engine, tmp_path, fakes.model_url)
        manifest.engine_family = "opencode"
        restored = PiEngine()
        try:
            with pytest.raises(RestoreMismatch):
                await _restore(restored, tmp_path, manifest)
        finally:
            await restored.close()


async def test_takeover_from_pig_to_upstream(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pi_module, "PI_BIN", os.environ["PIG_BIN"])
    first = PiEngine()
    async with fake_facades("text") as fakes:
        try:
            manifest = await _snapshot(first, tmp_path, fakes.model_url)
        finally:
            await first.close()
        monkeypatch.setattr(pi_module, "PI_BIN", os.environ["UPSTREAM_PI_BIN"])
        second = PiEngine()
        try:
            await _restore(second, tmp_path, manifest)
            await _start(second, tmp_path, fakes.model_url, "inv-2")
            assert await second.wait_settled("inv-2") == "completed"
        finally:
            await second.close()


def test_pig_targets_the_pinned_pi_version() -> None:
    pig = subprocess.run([os.environ["PIG_BIN"], "--version"], capture_output=True, text=True, timeout=30)
    pi = subprocess.run([os.environ["UPSTREAM_PI_BIN"], "--version"], capture_output=True, text=True, timeout=30)
    pig_version, pi_version = (pig.stdout + pig.stderr).strip(), (pi.stdout + pi.stderr).strip()
    assert pig_version.endswith(f"+{pi_version}"), (pig_version, pi_version)


async def test_cancel_of_a_hung_pi_kills_it_and_its_tools_within_the_bound(tmp_path, engine) -> None:
    async with fake_facades("tool-hang") as fakes:
        await _start(engine, tmp_path, fakes.model_url)
        await _wait_for_tool(tmp_path)
        os.killpg(engine._sessions["session-1"]._proc.pid, signal.SIGSTOP)
        now = time.monotonic()
        budget = _HaltBudget(cleanup_started_at=now, force_deadline=now + 2.0)
        await asyncio.wait_for(engine.cancel("inv-1", "operator request", _halt_budget=budget), 10)
        assert time.monotonic() - now < 2.5
        assert await engine.status("inv-1") == "cancelled"
        assert not _tools()
        assert not engine._sessions["session-1"].alive


async def test_launcher_only_death_is_cancelled_and_stops_the_native_binary(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pi_module, "PI_BIN", os.environ["PIG_BIN"])
    engine = PiEngine()
    try:
        async with fake_facades("hold") as fakes:
            await _start(engine, tmp_path, fakes.model_url)
            session = engine._sessions["session-1"]
            await asyncio.sleep(0.5)  # the observer records the native binary
            os.killpg(session._proc.pid, signal.SIGSTOP)  # the native binary stalls, then only the launcher dies
            session._proc.kill()
            await asyncio.wait_for(session._proc.wait(), 5)
            assert session.alive and not session.running
            await asyncio.wait_for(engine.cancel("inv-1", "operator request"), 15)
            assert await engine.status("inv-1") == "cancelled"
            assert not session.alive
            assert await session.stop()
    finally:
        await engine.close()


async def test_cancel_before_the_prompt_settles(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        await _start(engine, tmp_path, fakes.model_url)
        await engine.wait_settled("inv-1")
        engine._sessions["session-1"].invocations["inv-2"] = _PiInvocation(max_steps=5)
        await engine.cancel("inv-2", "operator request")
        assert await asyncio.wait_for(engine.wait_settled("inv-2"), 5) == "cancelled"


async def test_a_resumed_start_after_a_crash_is_not_failed_by_the_old_reader(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        await _start(engine, tmp_path, fakes.model_url)
        assert await engine.wait_settled("inv-1") == "completed"
        session = engine._sessions["session-1"]
        _crash(session)
        await asyncio.sleep(0.3)

        async def black_hole(reader, writer) -> None:
            await asyncio.sleep(30)

        server = await asyncio.start_server(black_hole, "127.0.0.1", 0)
        try:
            await _start(engine, tmp_path, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/v1", "inv-2")
            await asyncio.sleep(3)
            assert await engine.status("inv-2") == "running"
            assert session.running
        finally:
            server.close()

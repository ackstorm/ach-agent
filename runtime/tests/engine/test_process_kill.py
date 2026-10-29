# SPDX-License-Identifier: MIT
"""`_kill_process_group` escalation (contract §4's 10s cancellation bound), proven against
a real process group independent of opencode's own network/timing behavior. Plus
`_OwnedProcessGroup` (review finding #7): a detached descendant that leaves the process
GROUP (via `setsid`) is still a child in the /proc ppid chain, and must still be killed —
a bare `killpg` would miss it."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from pathlib import Path

from ach_runtime.engine import opencode as opencode_module
from ach_runtime.engine.opencode import _OwnedProcessGroup, _kill_process_group


async def test_kill_process_group_terminates_a_sigterm_ignoring_child() -> None:
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "trap '' TERM; sleep 60",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(0.2)  # let the trap install before we signal

    confirmed = await asyncio.wait_for(
        _kill_process_group(proc.pid, confirm_timeout=1.0), timeout=10.0
    )
    await proc.wait()

    assert confirmed is True
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("process group survived SIGKILL escalation")


async def test_kill_process_group_is_a_noop_on_already_exited_group() -> None:
    proc = await asyncio.create_subprocess_exec(
        "true", start_new_session=True, stdout=asyncio.subprocess.DEVNULL
    )
    await proc.wait()
    confirmed = await _kill_process_group(proc.pid, confirm_timeout=1.0)
    assert confirmed is True


async def test_owned_process_group_kills_a_descendant_that_left_the_process_group(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "owned_test_child_pid"
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        f"setsid sh -c 'trap \"\" TERM; sleep 60' & echo $! > {pid_file}; wait",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(0.3)  # let setsid's child actually detach and install its trap
        child_pid = int(pid_file.read_text().strip())

        # Sanity: the detached child really did leave the leader's process group —
        # killpg alone would not reach it.
        try:
            leader_pgid = os.getpgid(proc.pid)
            child_pgid = os.getpgid(child_pid)
        except ProcessLookupError:
            child_pgid = leader_pgid = None
        assert leader_pgid != child_pgid

        owned = _OwnedProcessGroup(proc.pid)
        owned._refresh()
        assert child_pid in owned._owned, "descendant tracking must find the detached child by ppid"

        confirmed = await owned.kill_and_confirm(confirm_timeout=1.0)
        await proc.wait()

        assert confirmed is True
        # `kill(pid, 0)` alone can't tell a zombie from a live process — a zombie still
        # has a /proc entry until its parent reaps it. `_linux_process_info`'s state char
        # is the same "live" test `kill_and_confirm` itself uses.
        from ach_runtime.engine.opencode import _linux_process_info

        info = _linux_process_info(child_pid)
        assert info is None or info[2] == "Z", f"detached descendant still live: {info}"
    finally:
        # M2: an assertion failure above must not orphan the leader or its detached child.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)
        if pid_file.exists():
            with contextlib.suppress(ValueError, ProcessLookupError, FileNotFoundError):
                os.kill(int(pid_file.read_text().strip()), 9)
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()


async def test_kill_confirms_descendants_after_leader_already_exited(tmp_path: Path) -> None:
    """Finding: "_kill bypasses tracked children once the leader exits" — a leader that
    exits on its own (crash, fast-exit) must not short-circuit descendant confirmation; a
    still-live detached child, already tracked before that exit, must still be found and
    killed rather than returning "confirmed" on the leader's exit alone."""
    child_pid_path = tmp_path / "kill_after_exit_child_pid"
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        f"setsid sh -c 'trap \"\" TERM; sleep 60' & echo $! > {child_pid_path}; "
        "sleep 0.3; exit 0",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    session = opencode_module._NativeSession("exited-leader-session", tmp_path, tmp_path)
    session._proc = proc
    session._owned = _OwnedProcessGroup(proc.pid)
    child_pid: int | None = None
    try:
        # Continuous-tracking stand-in: keep refreshing — like the real
        # ownership-observer task would — until the detached child is discovered, well
        # before the leader (which exits almost immediately) is reaped; a single late
        # refresh would already find the leader gone from /proc and discover nothing.
        for _ in range(200):
            session._owned._refresh()
            if child_pid is None and child_pid_path.exists():
                child_pid = int(child_pid_path.read_text().strip())
            if child_pid is not None and child_pid in session._owned._owned:
                break
            await asyncio.sleep(0.02)
        await proc.wait()
        assert child_pid is not None
        assert child_pid in session._owned._owned, "descendant tracking must find the child before leader exit"

        confirmed = await session._kill(confirm_timeout=2.0)
        assert confirmed is True

        from ach_runtime.engine.opencode import _linux_process_info

        info = _linux_process_info(child_pid)
        assert info is None or info[2] == "Z", f"detached descendant still live: {info}"
    finally:
        # M2: an assertion failure above must not orphan the leader or its detached child.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, 9)
        if child_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(child_pid, 9)
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()


async def test_kill_does_not_touch_an_unrelated_peer_process() -> None:
    """A kill scoped to one owned process group must never touch an unrelated process that
    merely happens to be running alongside it."""
    owned_proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "trap '' TERM; sleep 60",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    peer_proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "trap '' TERM; sleep 60",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.sleep(0.2)
        owned = _OwnedProcessGroup(owned_proc.pid)

        confirmed = await owned.kill_and_confirm(confirm_timeout=1.0)
        await owned_proc.wait()

        assert confirmed is True
        try:
            os.kill(peer_proc.pid, 0)
        except ProcessLookupError:
            raise AssertionError("unrelated peer process was killed") from None
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(peer_proc.pid, 9)
        await peer_proc.wait()


async def test_launch_cancellation_cleans_up_the_orphaned_process(tmp_path: Path) -> None:
    """Finding: "launch cancellation is unhandled" — if `ensure_started`'s own task is
    cancelled before a URL is ever printed (e.g. an outer caller's deadline fired), the
    spawned subprocess must still be confirmed killed, not orphaned."""
    fake_bin = tmp_path / "fake_opencode"
    fake_bin.write_text("#!/bin/sh\ntrap '' TERM\nsleep 60\n")
    fake_bin.chmod(0o755)

    session = opencode_module._NativeSession(
        "launch-cancel-session", tmp_path / "home", tmp_path / "scratch"
    )
    original_bin = opencode_module.OPENCODE_BIN
    opencode_module.OPENCODE_BIN = str(fake_bin)
    try:
        task = asyncio.create_task(
            session.ensure_started(
                env={"OPENCODE_SERVER_PASSWORD": "x"}, config_path=tmp_path / "cfg.json"
            )
        )
        await asyncio.sleep(0.3)  # let the fake binary actually launch
        assert session.alive
        pid = session._proc.pid
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=10.0)
    finally:
        opencode_module.OPENCODE_BIN = original_bin

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("cancelled launch left the native process running")


async def test_lowlevel_term_kill_share_one_total_budget(monkeypatch) -> None:
    """Step1: TERM observation and KILL confirmation must share ONE entry-anchored total
    window — TERM gets at most its first half, KILL confirmation only the remainder to the
    original deadline — never two full `confirm_timeout` windows stacked."""
    proc = await asyncio.create_subprocess_exec(
        "sh",
        "-c",
        "trap '' TERM; sleep 60",
        start_new_session=True,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.sleep(0.2)  # let the trap install before we signal
    signals: list[tuple[float, int]] = []
    original_killpg = os.killpg

    def _spy(pid: int, sig: int):
        signals.append((time.monotonic(), sig))
        return original_killpg(pid, sig)

    monkeypatch.setattr(os, "killpg", _spy)
    start = time.monotonic()
    confirmed = await asyncio.wait_for(
        _kill_process_group(proc.pid, confirm_timeout=1.0), timeout=10.0
    )
    await proc.wait()

    assert confirmed is True
    import signal as signal_module

    kill_calls = [t for t, sig in signals if sig == signal_module.SIGKILL]
    assert kill_calls, "SIGKILL was never sent"
    assert kill_calls[0] - start < 0.75, (
        "SIGKILL must fire inside the shared total window's first half (TERM gets at most "
        f"half of confirm_timeout), fired at {kill_calls[0] - start:.2f}s of a 1.0s budget"
    )


async def test_direct_kill_caps_confirm_timeout_at_five_seconds(tmp_path: Path, monkeypatch) -> None:
    """Step1: a direct `_kill()` call (no `_halt_budget`) must cap its total confirmation
    budget at 5s even when a caller passes a far larger `confirm_timeout` — never actually
    arm the low-level kill-and-confirm with the caller's oversized window."""
    loopback_server = Path(__file__).parent / "fixtures" / "fake_native_server.py"
    monkeypatch.setattr(opencode_module, "OPENCODE_BIN", str(loopback_server))
    session = opencode_module._NativeSession(
        "direct-kill-cap-session", tmp_path / "home", tmp_path / "scratch"
    )
    await session.ensure_started(
        env={"OPENCODE_SERVER_PASSWORD": "x"}, config_path=tmp_path / "cfg.json"
    )
    captured: list[float] = []
    original = opencode_module._OwnedProcessGroup.kill_and_confirm

    async def _spy(self, *, confirm_timeout: float, force_immediately: bool = False) -> bool:
        captured.append(confirm_timeout)
        return await original(
            self, confirm_timeout=confirm_timeout, force_immediately=force_immediately
        )

    monkeypatch.setattr(opencode_module._OwnedProcessGroup, "kill_and_confirm", _spy)
    confirmed = await asyncio.wait_for(session._kill(confirm_timeout=1000.0), timeout=10.0)

    assert confirmed is True
    assert captured, "kill_and_confirm was never invoked"
    assert captured[0] <= 5.0, f"direct _kill must cap its total budget at 5s, saw {captured[0]}"

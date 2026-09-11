"""Real owned-process cleanup acceptance tests."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

_TREE_PROGRAM = """
import signal
import subprocess
import sys
import time
from pathlib import Path

status = Path(sys.argv[1])
workspace = Path(sys.argv[2])
child_program = '''
import signal
import sys
import time
from pathlib import Path

status = Path(sys.argv[1])
workspace = open(sys.argv[2], "w", encoding="utf-8")
def term(_signal, _frame):
    status.write_text(\"term\", encoding=\"utf-8\")
    raise SystemExit(0)
signal.signal(signal.SIGTERM, term)
status.write_text(\"alive\", encoding=\"utf-8\")
workspace.write(\"owned workspace\")
workspace.flush()
while True:
    time.sleep(0.05)
'''
grandchild = subprocess.Popen(
    [sys.executable, \"-c\", child_program, str(status), str(workspace)],
    start_new_session=True,
)
Path(str(status) + \".pid\").write_text(str(grandchild.pid), encoding=\"ascii\")
time.sleep(0.2)
"""

_FAST_ORPHAN_PROGRAM = """
import os
import time
from pathlib import Path

ready = Path({ready!r})
go = Path({go!r})
status = Path({status!r})
pid_file = Path({pid_file!r})
ready.write_text("ready", encoding="utf-8")
while not go.exists():
    time.sleep(0.001)
child = os.fork()
if child == 0:
    os.setsid()
    os.close(0)
    os.close(1)
    os.close(2)
    status.write_text("alive", encoding="utf-8")
    while True:
        time.sleep(1)
pid_file.write_text(str(child), encoding="ascii")
os._exit(0)
"""


async def _wait_for(path: Path, value: str, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if path.exists() and path.read_text(encoding="utf-8") == value:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}={value!r}")


async def _wait_gone(pid: int, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        proc_path = Path(f"/proc/{pid}")
        if not proc_path.exists():
            return
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text(encoding="utf-8").split(" ", 3)[2] == "Z":
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"owned process {pid} remained live")


@pytest.mark.skipif(
    sys.platform != "linux", reason="the orphan fixture uses Linux process semantics"
)
async def test_fast_orphan_is_not_reported_clean_before_death(tmp_path: Path) -> None:
    """A fork/setsid orphan is cleaned despite exiting before the observer's first poll."""
    from unittest.mock import AsyncMock, patch

    from ach_agent.engine.lifecycle import EngineConfig, launch

    ready = tmp_path / "ready"
    go = tmp_path / "go"
    status = tmp_path / "status"
    pid_file = tmp_path / "child.pid"
    binary = tmp_path / "opencode"
    binary.write_text(
        "#!"
        + sys.executable
        + "\n"
        + _FAST_ORPHAN_PROGRAM.format(
            ready=str(ready), go=str(go), status=str(status), pid_file=str(pid_file)
        ),
        encoding="utf-8",
    )
    binary.chmod(0o755)
    config = EngineConfig(binary_path=str(binary), home=str(tmp_path), work_dir=str(tmp_path))
    server = None
    child_pid: int | None = None
    try:
        with patch(
            "ach_agent.engine.opencode.client.OpenCodeClient.check_health",
            new_callable=AsyncMock,
            return_value=True,
        ):
            server = await launch(0, tmp_path, config, "fast-orphan")
        await _wait_for(ready, "ready")
        go.write_text("go", encoding="ascii")
        deadline = asyncio.get_running_loop().time() + 3.0
        while (
            not pid_file.exists() or not pid_file.read_text(encoding="ascii")
        ) and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert pid_file.exists()
        child_pid = int(pid_file.read_text(encoding="ascii"))
        await _wait_for(status, "alive")
        await server.stop()
        await _wait_gone(child_pid)
    finally:
        if child_pid is not None and Path(f"/proc/{child_pid}").exists():
            os.kill(child_pid, signal.SIGKILL)
            await _wait_gone(child_pid)
        if server is not None:
            await server.stop()


@pytest.mark.skipif(sys.platform != "linux", reason="the process barrier fixture uses Linux /proc")
async def test_concurrent_stop_waits_for_forced_process_death(tmp_path: Path) -> None:
    """A second stop caller cannot return while a SIGTERM-resistant process lives."""
    import ach_agent.engine.lifecycle as lifecycle
    from ach_agent.engine.lifecycle import ManagedServer

    ready = tmp_path / "ready"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import signal,sys,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text('ready'); time.sleep(30)",
        str(ready),
        start_new_session=True,
    )
    await _wait_for(ready, "ready")
    server = ManagedServer(port=0)
    server.register_process(proc)
    original_shutdown = lifecycle.SHUTDOWN_TIMEOUT
    original_force = lifecycle._FORCE_CLEANUP_TIMEOUT_S
    lifecycle.SHUTDOWN_TIMEOUT = 0.15
    lifecycle._FORCE_CLEANUP_TIMEOUT_S = 1.0
    try:
        deadline = asyncio.get_running_loop().time() + 1.0
        while not server._live_owned_processes() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert server._owned_processes
        assert server._live_owned_processes()
        started = asyncio.get_running_loop().time()
        await asyncio.gather(server.stop(), server.stop())
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed >= 0.1
        assert proc.returncode is not None
    finally:
        lifecycle.SHUTDOWN_TIMEOUT = original_shutdown
        lifecycle._FORCE_CLEANUP_TIMEOUT_S = original_force
        await server.stop()


@pytest.mark.skipif(sys.platform != "linux", reason="the detached-process fixture uses Linux /proc")
async def test_stop_joins_owned_detached_descendant_after_leader_exit(tmp_path: Path) -> None:
    """A detached child is terminated even after its process-group leader exits."""
    from ach_agent.engine.lifecycle import ManagedServer

    status = tmp_path / "status"
    workspace = tmp_path / "workspace-held-by-child"
    unrelated = await asyncio.create_subprocess_exec("sleep", "30")
    leader = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _TREE_PROGRAM,
        str(status),
        str(workspace),
        start_new_session=True,
    )
    server = ManagedServer(port=0)
    server.register_process(leader)
    child_pid_path = Path(str(status) + ".pid")
    try:
        await _wait_for(status, "alive")
        deadline = asyncio.get_running_loop().time() + 3.0
        while not child_pid_path.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        assert child_pid_path.exists()
        child_pid = int(child_pid_path.read_text(encoding="ascii"))
        await leader.wait()
        assert leader.returncode == 0

        await asyncio.gather(server.stop(), server.stop())
        await _wait_for(status, "term")
        await _wait_gone(child_pid)
        assert unrelated.returncode is None
    finally:
        if unrelated.returncode is None:
            unrelated.terminate()
            await unrelated.wait()
        await server.stop()


@pytest.mark.skipif(sys.platform != "linux", reason="the detached-process fixture uses Linux /proc")
async def test_stopping_one_server_does_not_signal_another_owned_tree(tmp_path: Path) -> None:
    """Independent native executions retain separate ownership boundaries."""
    from ach_agent.engine.lifecycle import ManagedServer

    status_a = tmp_path / "status-a"
    status_b = tmp_path / "status-b"
    leader_a = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _TREE_PROGRAM,
        str(status_a),
        str(tmp_path / "workspace-a"),
        start_new_session=True,
    )
    leader_b = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _TREE_PROGRAM,
        str(status_b),
        str(tmp_path / "workspace-b"),
        start_new_session=True,
    )
    server_a = ManagedServer(port=0)
    server_b = ManagedServer(port=0)
    server_a.register_process(leader_a)
    server_b.register_process(leader_b)
    try:
        await _wait_for(status_a, "alive")
        await _wait_for(status_b, "alive")
        await leader_a.wait()
        await server_a.stop()
        await _wait_for(status_a, "term")
        await _wait_for(status_b, "alive")
    finally:
        await server_a.stop()
        await server_b.stop()

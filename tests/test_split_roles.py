# SPDX-License-Identifier: Apache-2.0
"""Task 8B local role orchestration contract tests."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

import httpx
import pytest

from ach_agent.config.schema import AgentConfig


def _cfg(**updates: object) -> AgentConfig:
    raw: dict[str, object] = {
        "schemaVersion": "1",
        "agent": {"name": "role-test"},
        "model": {"name": "openai.gpt-5", "type": "openai"},
        "capability": {"ach": {"baseUrl": "https://ach.example.test"}},
        "channels": [],
    }
    raw.update(updates)
    return AgentConfig.model_validate(raw)


def test_role_cli_selection_preserves_console_flags() -> None:
    from ach_agent.main import _parse_cli

    role, tui, prompt, debug = _parse_cli(["--role", "engine", "--tui", "--prompt", "hello"])
    assert (role, tui, prompt, debug) == ("engine", True, "hello", False)


def test_engine_child_starts_without_native_process(tmp_path: Path) -> None:
    from ach_agent.boot.local import LocalEngineProcess
    from ach_agent.boot.ipc import engine_socket_path

    runtime_dir = tmp_path / "runtime"
    socket_path = engine_socket_path(runtime_dir)

    async def run() -> None:
        child = await LocalEngineProcess.start(None, env={"ACH_RUNTIME_DIR": str(runtime_dir)})
        try:
            health = await child.wait_ready(
                "http://ach-internal", timeout=10, socket_path=str(socket_path)
            )
            assert child.process.returncode is None
            assert health["instance_id"]
            async with httpx.AsyncClient(
                base_url="http://ach-internal",
                transport=httpx.AsyncHTTPTransport(uds=str(socket_path)),
            ) as client:
                response = await client.get("/readyz")
            assert response.status_code == 200
        finally:
            await child.close(timeout=5)
        assert child.process.returncode is not None

    asyncio.run(run())


def test_local_mcp_refs_are_explicit_engine_env_and_managed_names_are_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ach_agent.boot.roles import build_role_configs

    cfg = _cfg(
        engine={
            "forwardEnv": [
                "SAFE_OPERATOR",
                "ACH_TOKEN",
                "ACH_CHANNELS_HMAC_KEY",
                "ACH_MODEL_HEADER",
            ]
        },
        mcpServers={
            "stdio": {
                "type": "local",
                "command": "demo-mcp",
                "env": ["MCP_OPERATOR", "ACH_API_KEY"],
            },
            "remote": {
                "type": "remote",
                "url": "https://mcp.example.test",
                "headers": {"Authorization": "Bearer ${env:MCP_REMOTE}"},
            },
        },
    )

    monkeypatch.setenv("SAFE_OPERATOR", "safe")
    monkeypatch.setenv("MCP_OPERATOR", "mcp")
    monkeypatch.setenv("MCP_REMOTE", "remote")
    _channels, public = build_role_configs(cfg, split_mode=False)
    assert public["engineEnv"] == {
        "SAFE_OPERATOR": "safe",
        "MCP_OPERATOR": "mcp",
        "MCP_REMOTE": "remote",
    }


def test_local_mcp_reference_cannot_readd_config_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ach_agent.boot.roles import build_role_configs

    cfg = _cfg(
        engine={"forwardEnv": ["SAFE_OPERATOR"]},
        channels=[
            {
                "name": "cron",
                "type": "cron",
                "cron": {"schedule": "* * * * *"},
                "prepare": {"script": "true"},
                "cleanup": {
                    "script": "true",
                    "secretEnv": {"TOKEN": {"env": "GITLAB_TOKEN"}},
                },
            }
        ],
        mcpServers={
            "stdio": {
                "type": "local",
                "command": "demo-mcp",
                "env": ["GITLAB_TOKEN", "MCP_OPERATOR"],
            }
        },
    )

    monkeypatch.setenv("SAFE_OPERATOR", "safe")
    monkeypatch.setenv("MCP_OPERATOR", "mcp")
    monkeypatch.setenv("GITLAB_TOKEN", "managed")
    _channels, public = build_role_configs(cfg, split_mode=False)
    assert public["engineEnv"] == {"SAFE_OPERATOR": "safe", "MCP_OPERATOR": "mcp"}


def test_public_engine_rejects_harness_managed_env_names() -> None:
    from ach_agent.execution.wire import PublicEngineConfig

    with pytest.raises(ValueError, match="harness-managed"):
        PublicEngineConfig(engine_env={"ACH_TOKEN": "secret"})


def test_terminal_child_keeps_inherited_terminal_and_safe_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ach_agent.boot.local import LocalEngineProcess

    artifacts = None
    calls: list[tuple[object, ...]] = []

    class FakeProcess:
        returncode = None
        pid = 1234

        def send_signal(self, _signal: int) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            self.returncode = 0
            return 0

    async def fake_spawn(*command: object, **kwargs: object) -> FakeProcess:
        calls.append(command)
        assert kwargs["start_new_session"] is False
        assert "stdin" not in kwargs and "stdout" not in kwargs
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["TERM"]
        assert env["LANG"]
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_spawn)

    async def run() -> None:
        child = await LocalEngineProcess.start(artifacts, terminal_mode=True)
        assert calls and calls[0][-1] == "--tui"
        await child.close(timeout=1)

    asyncio.run(run())


def test_local_timeout_kills_owned_supervisor_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ach_agent.boot.local import LocalEngineProcess

    artifacts = None
    killed: list[tuple[int, signal.Signals]] = []

    class HungProcess:
        pid = 4567
        returncode = None

        def send_signal(self, _signal: signal.Signals) -> None:
            pass

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            self.returncode = -9
            return -9

    async def always_timeout(*_args: object, **_kwargs: object) -> None:
        future = _args[0] if _args else None
        if hasattr(future, "close"):
            future.close()
        raise TimeoutError

    async def run() -> None:
        child = LocalEngineProcess(HungProcess(), artifacts, isolated_process_group=True)  # type: ignore[arg-type]
        await child.close(timeout=0.01)

    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(asyncio, "wait_for", always_timeout)
    asyncio.run(run())
    assert killed == [(4567, signal.SIGTERM), (4567, signal.SIGKILL)]


def test_local_timeout_reaps_real_supervised_descendant(tmp_path: Path) -> None:
    from ach_agent.boot.local import LocalEngineProcess

    artifacts = None
    sentinel = tmp_path / "descendant-alive"
    supervisor = (
        Path(__file__).parents[1] / "src" / "ach_agent" / "engine" / "process_supervisor.py"
    )
    command = [
        sys.executable,
        str(supervisor),
        "--",
        "/bin/sh",
        "-c",
        f"trap '' TERM; (sleep 1; echo alive > {sentinel}) & wait",
    ]

    async def run() -> None:
        process = await asyncio.create_subprocess_exec(
            *command, start_new_session=True, stdout=asyncio.subprocess.DEVNULL
        )
        child = LocalEngineProcess(process, artifacts, isolated_process_group=True)
        for _ in range(50):
            if process.returncode is None:
                await asyncio.sleep(0.01)
                continue
            break
        await child.close(timeout=0.05)
        await asyncio.sleep(1.2)

    asyncio.run(run())
    assert not sentinel.exists()


@pytest.mark.skipif(sys.platform != "linux", reason="the owned descendant fixture uses Linux /proc")
@pytest.mark.parametrize(
    ("terminal_mode", "start_new_session"),
    ((False, True), (True, False)),
)
def test_local_timeout_reaps_term_resistant_detached_descendant(
    tmp_path: Path, terminal_mode: bool, start_new_session: bool
) -> None:
    """Local cleanup kills a detached child before terminating its supervisor root."""
    from ach_agent.boot.local import LocalEngineProcess

    artifacts = None
    child_pid_file = tmp_path / "child.pid"
    child_ready_file = tmp_path / "child-ready"
    ready = tmp_path / "ready"
    supervisor = (
        Path(__file__).parents[1] / "src" / "ach_agent" / "engine" / "process_supervisor.py"
    )
    leader = """
import signal
import subprocess
import sys
import time
from pathlib import Path

child_pid_file = Path(sys.argv[1])
child_ready_file = Path(sys.argv[2])
ready = Path(sys.argv[3])
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(sys.argv[1]).write_text('ready', encoding='ascii'); time.sleep(30)",
        str(child_ready_file),
    ],
    start_new_session=True,
)
while not child_ready_file.exists():
    time.sleep(0.01)
child_pid_file.write_text(str(child.pid), encoding="ascii")
ready.write_text("ready", encoding="ascii")
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while True:
    time.sleep(1)
"""
    command = [
        sys.executable,
        str(supervisor),
        "--",
        sys.executable,
        "-c",
        leader,
        str(child_pid_file),
        str(child_ready_file),
        str(ready),
    ]
    process: asyncio.subprocess.Process | None = None
    child_pid: int | None = None

    async def wait_gone(pid: int, timeout: float = 3.0, *, allow_zombie: bool = False) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            proc_path = Path(f"/proc/{pid}")
            if not proc_path.exists():
                return
            stat = proc_path / "stat"
            if (
                allow_zombie
                and stat.exists()
                and stat.read_text(encoding="ascii").split(" ", 3)[2] == "Z"
            ):
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"detached process {pid} remained live")

    async def run() -> None:
        nonlocal child_pid, process
        unrelated: asyncio.subprocess.Process | None = None
        try:
            if terminal_mode:
                unrelated = await asyncio.create_subprocess_exec("sleep", "30")
            process = await asyncio.create_subprocess_exec(
                *command,
                start_new_session=start_new_session,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            child = LocalEngineProcess(
                process,
                artifacts,
                isolated_process_group=not terminal_mode,
            )
            await wait_for_path(ready)
            child_pid = int(child_pid_file.read_text(encoding="ascii"))
            assert os.getpgid(child_pid) != os.getpgid(process.pid)
            started = asyncio.get_running_loop().time()
            await child.close(timeout=0.05)
            assert asyncio.get_running_loop().time() - started < 3.0
            await wait_gone(child_pid)
            assert process.returncode is not None
            if unrelated is not None:
                assert unrelated.returncode is None
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            if unrelated is not None and unrelated.returncode is None:
                unrelated.terminate()
                await unrelated.wait()
            if child_pid is not None and Path(f"/proc/{child_pid}").exists():
                os.kill(child_pid, signal.SIGKILL)
                await wait_gone(child_pid, allow_zombie=True)

    async def wait_for_path(path: Path, timeout: float = 3.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if path.exists():
                return
            await asyncio.sleep(0.02)
        raise AssertionError(f"timed out waiting for {path}")

    asyncio.run(run())


def test_public_context_is_linked_from_custom_work_dir(tmp_path: Path) -> None:
    from ach_agent.engine.context import link_public_context

    home = tmp_path / "home"
    work = tmp_path / "work"
    public = tmp_path / "public"
    link_public_context(home, public, work_dir=work)
    assert (work / ".ach-state").is_symlink()
    assert (work / ".ach-state").resolve() == public.resolve()

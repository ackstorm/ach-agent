# SPDX-License-Identifier: Apache-2.0
"""Task 8B local role orchestration contract tests."""

from __future__ import annotations

import asyncio
import json
import socket
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


def test_local_artifacts_are_filtered_and_atomic(tmp_path: Path) -> None:
    from ach_agent.boot.local import RoleArtifacts
    from ach_agent.boot.roles import build_role_configs

    channels, public = build_role_configs(_cfg())
    artifacts = RoleArtifacts(tmp_path)
    paths = artifacts.write(channels, public)

    assert paths.channels.parent == tmp_path
    assert paths.engine.parent == tmp_path
    assert json.loads(paths.channels.read_text()) == channels
    assert json.loads(paths.engine.read_text()) == public
    assert "capability" not in json.loads(paths.engine.read_text())


def test_role_cli_selection_preserves_console_flags() -> None:
    from ach_agent.main import _parse_cli

    role, tui, prompt, debug = _parse_cli(["--role", "engine", "--tui", "--prompt", "hello"])
    assert (role, tui, prompt, debug) == ("engine", True, "hello", False)


def test_engine_child_starts_without_native_process(tmp_path: Path) -> None:
    from ach_agent.boot.local import LocalEngineProcess, RoleArtifacts
    from ach_agent.boot.roles import build_role_configs

    _channels, public = build_role_configs(_cfg())
    artifacts = RoleArtifacts(tmp_path).write(_channels, public)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    async def run() -> None:
        child = await LocalEngineProcess.start(artifacts, port=port)
        try:
            health = await child.wait_ready(f"http://127.0.0.1:{port}", timeout=10)
            assert child.process.returncode is None
            assert health["instance_id"]
            async with httpx.AsyncClient() as client:
                response = await client.get(f"http://127.0.0.1:{port}/readyz")
            assert response.status_code == 200
        finally:
            await child.close(timeout=5)
        assert child.process.returncode is not None

    asyncio.run(run())


def test_local_mcp_refs_are_explicit_engine_env_and_managed_names_are_removed() -> None:
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

    _channels, public = build_role_configs(cfg, split_mode=False)
    assert public["engineEnvNames"] == ["SAFE_OPERATOR", "MCP_OPERATOR", "MCP_REMOTE"]


def test_public_engine_rejects_harness_managed_env_names() -> None:
    from ach_agent.execution.wire import PublicEngineConfig

    with pytest.raises(ValueError, match="ACH_TOKEN"):
        PublicEngineConfig(engine_env_names=["ACH_TOKEN"])


def test_terminal_child_keeps_inherited_terminal_and_safe_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ach_agent.boot.local import LocalEngineProcess, RoleArtifacts

    artifacts = RoleArtifacts(tmp_path).write({}, {})
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
        assert kwargs["start_new_session"] is True
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


def test_public_context_is_linked_from_custom_work_dir(tmp_path: Path) -> None:
    from ach_agent.engine.context import link_public_context

    home = tmp_path / "home"
    work = tmp_path / "work"
    public = tmp_path / "public"
    link_public_context(home, public, work_dir=work)
    assert (work / ".ach-state").is_symlink()
    assert (work / ".ach-state").resolve() == public.resolve()

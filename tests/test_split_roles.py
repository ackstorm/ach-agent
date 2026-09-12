# SPDX-License-Identifier: Apache-2.0
"""Task 8B local role orchestration contract tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ach_agent.config.schema import AgentConfig


def _cfg() -> AgentConfig:
    return AgentConfig.model_validate(
        {
            "schemaVersion": "1",
            "agent": {"name": "role-test"},
            "model": {"name": "openai.gpt-5", "type": "openai"},
            "capability": {"ach": {"baseUrl": "https://ach.example.test"}},
            "channels": [],
        }
    )


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

    async def run() -> None:
        child = await LocalEngineProcess.start(artifacts, port=0)
        try:
            # A zero native process is a valid ready state for the mini-harness.
            assert child.process.returncode is None
        finally:
            await child.close(timeout=5)

    asyncio.run(run())


def test_public_engine_rejects_harness_managed_env_names() -> None:
    from ach_agent.execution.wire import PublicEngineConfig

    with pytest.raises(ValueError, match="ACH_TOKEN"):
        PublicEngineConfig(engine_env_names=["ACH_TOKEN"])


def test_public_context_is_linked_from_custom_work_dir(tmp_path: Path) -> None:
    from ach_agent.engine.context import link_public_context

    home = tmp_path / "home"
    work = tmp_path / "work"
    public = tmp_path / "public"
    link_public_context(home, public, work_dir=work)
    assert (work / ".ach-state").is_symlink()
    assert (work / ".ach-state").resolve() == public.resolve()

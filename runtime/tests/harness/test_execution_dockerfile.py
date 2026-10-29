"""The execution and control images must run as a fixed numeric non-root uid (runAsNonRoot needs it)."""
from __future__ import annotations

from pathlib import Path

import pytest

_DOCKERFILE = Path(__file__).resolve().parents[2] / "Dockerfile"


def _stage(text: str, name: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("FROM "):
            inside = line.rstrip().endswith(f" AS {name}")
        elif inside:
            lines.append(line.strip())
    return lines


@pytest.mark.skipif(not _DOCKERFILE.exists(), reason="Dockerfile not mounted")
@pytest.mark.parametrize("stage", ["execution", "control", "agent"])
def test_stage_sets_numeric_non_root_user(stage: str) -> None:
    users = [l.split()[1] for l in _stage(_DOCKERFILE.read_text(), stage) if l.startswith("USER ")]
    assert users, f"{stage} stage must set USER"
    uid, _, gid = users[-1].partition(":")
    assert uid.isdigit() and int(uid) != 0
    assert not gid or (gid.isdigit() and int(gid) != 0)


@pytest.mark.skipif(not _DOCKERFILE.exists(), reason="Dockerfile not mounted")
def test_execution_stage_installs_the_ach_mcp_cli() -> None:
    stage = "\n".join(_stage(_DOCKERFILE.read_text(), "execution"))
    assert "/usr/local/bin/ach-mcp" in stage
    assert "python -m ach_runtime.mcpcall" in stage


@pytest.mark.skipif(not _DOCKERFILE.exists(), reason="Dockerfile not mounted")
def test_agent_stage_owns_its_workspaces_directory_and_has_no_copy_all() -> None:
    text = _DOCKERFILE.read_text()
    lines = _stage(text, "agent")
    assert any(l.startswith("RUN ") and "/var/lib/ach-runtime/workspaces" in l and "chown 10001:10001" in l for l in lines)
    assert not any(l.startswith("COPY . ") for l in text.splitlines())

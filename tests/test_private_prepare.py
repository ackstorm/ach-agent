# SPDX-License-Identifier: Apache-2.0
"""Task 0A characterization of private preparation and workspace handoff.

These tests deliberately exercise the current script hook, rather than a proposed
private-checkout implementation.  The security tests are strict xfails until Task
0B removes the contaminated Git configuration path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ach_agent.boot.prepare import PrepareFailed, prepare_workspace, run_cleanup, run_prepare
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock


def _event(number: int = 1) -> MessageEvent:
    return MessageEvent(
        idempotency_key=f"evt-{number}",
        session_key="group/project:7",
        channel_name="gitlab-mr-review",
        delivery_context={"project_path": "group/project", "head_sha": "unused"},
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)
    return result.stdout.strip()


def _local_origin(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "fixture")
    (source / "notes.txt").write_text("origin\n")
    _git(source, "add", "notes.txt")
    _git(source, "commit", "-qm", "initial")
    return source, _git(source, "rev-parse", "HEAD")


def _clone_block(source: Path) -> PrepareBlock:
    return PrepareBlock.model_validate(
        {
            "script": """
set -eu
REPO="$ACH_WORKSPACE/repo"
if [ -d "$REPO/.git" ]; then
  git -C "$REPO" fetch -q origin
else
  git clone -q "$SOURCE" "$REPO"
fi
git -C "$REPO" checkout -q --force --detach origin/main
""",
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )


async def test_private_fixture_characterizes_reuse_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, initial_head = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    home = tmp_path / "home"
    workspace = prepare_workspace(str(home), str(tmp_path / "work"), "group/project:7")
    inode = workspace.stat().st_ino
    cfg = _clone_block(source)

    await run_prepare(cfg, _event(), workspace)
    repo = workspace / "repo"
    _git(repo, "config", "user.email", "agent@example.invalid")
    _git(repo, "config", "user.name", "agent")
    assert workspace.stat().st_ino == inode
    assert (workspace / ".ach-state").resolve() == (home / ".ach-state").resolve()
    assert _git(repo, "rev-parse", "HEAD") == initial_head
    assert (repo / "notes.txt").read_text() == "origin\n"

    # A populated checkout is reused. The current reference script's force checkout
    # discards tracked edits, while unrelated files and local commits remain observable.
    (repo / "notes.txt").write_text("dirty agent edit\n")
    await run_prepare(cfg, _event(2), workspace)
    assert (repo / "notes.txt").read_text() == "origin\n"
    assert workspace.stat().st_ino == inode

    (repo / "notes.txt").write_text("dirty agent edit\n")
    (repo / "untracked.txt").write_text("retain me\n")
    _git(repo, "add", "notes.txt")
    _git(repo, "commit", "-qm", "agent local commit")
    local_head = _git(repo, "rev-parse", "HEAD")
    await run_prepare(cfg, _event(3), workspace)
    assert workspace.stat().st_ino == inode
    assert _git(repo, "rev-parse", "HEAD") == initial_head
    assert (repo / "notes.txt").read_text() == "origin\n"
    assert (repo / "untracked.txt").read_text() == "retain me\n"
    assert _git(repo, "cat-file", "-t", local_head) == "commit"

    await run_cleanup(
        PrepareBlock.model_validate({"script": 'rm -rf -- "$ACH_WORKSPACE/repo"'}),
        _event(2),
        workspace,
    )
    assert workspace.stat().st_ino == inode
    assert not repo.exists()
    assert (workspace / ".ach-state").is_symlink()


async def test_private_scratch_prototype_preserves_target_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentialed scratch preparation, then local Git handoff to target."""
    source, initial_head = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    private_home = tmp_path / "private-home"
    scratch = tmp_path / "private-scratch"
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "scratch")
    inode = workspace.stat().st_ino
    cfg = PrepareBlock.model_validate(
        {
            "script": """
set -eu
export HOME="$PRIVATE_HOME"
mkdir -p "$HOME"
SCRATCH="$SCRATCH_PATH"
if [ -d "$SCRATCH/.git" ]; then
  git -C "$SCRATCH" fetch -q origin
else
  git clone -q "$SOURCE" "$SCRATCH"
fi
""",
            "env": {
                "SOURCE": str(source),
                "PRIVATE_HOME": str(private_home),
                "SCRATCH_PATH": str(scratch),
            },
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    await run_prepare(cfg, _event(), workspace)
    assert scratch.exists()
    sentinel = tmp_path / "handoff-token-marker"
    hook = tmp_path / "handoff-fsmonitor.sh"
    hook.write_text(f"#!/bin/sh\n[ \"${{TOKEN-}}\" = synthetic-token ] && touch {sentinel}\n")
    hook.chmod(0o700)
    (workspace / ".gitconfig").write_text(f"[core]\n\tfsmonitor = {hook}\n")

    # The handoff is a separate process with a deliberately minimal environment.
    # In particular it receives no TOKEN, HOME inherited from preparation, or Git config override.
    transfer = PrepareBlock.model_validate(
        {
            "script": """
set -eu
REPO="$ACH_WORKSPACE/repo"
if [ ! -d "$REPO/.git" ]; then git clone -q "$SCRATCH_PATH" "$REPO"; fi
git -C "$REPO" fetch -q "$SCRATCH_PATH" main:refs/ach/scratch-main
git -C "$REPO" checkout -q --force --detach refs/ach/scratch-main
git -C "$REPO" status --short
""",
            "env": {"SCRATCH_PATH": str(scratch)},
        }
    )
    await run_prepare(transfer, _event(2), workspace)
    assert not sentinel.exists()
    repo = workspace / "repo"
    _git(repo, "config", "user.email", "agent@example.invalid")
    _git(repo, "config", "user.name", "agent")
    (repo / "local.txt").write_text("local commit\n")
    _git(repo, "add", "local.txt")
    _git(repo, "commit", "-qm", "local state")
    local_head = _git(repo, "rev-parse", "HEAD")
    (repo / "untracked.txt").write_text("retain me\n")
    (repo / "notes.txt").write_text("dirty edit\n")
    await run_prepare(transfer, _event(3), workspace)
    assert workspace.stat().st_ino == inode
    assert _git(repo, "rev-parse", "HEAD") == initial_head
    assert (repo / "notes.txt").read_text() == "origin\n"
    assert (repo / "untracked.txt").read_text() == "retain me\n"
    assert _git(repo, "cat-file", "-t", local_head) == "commit"


async def test_prepare_failure_is_fail_closed_and_retains_workspace(tmp_path: Path) -> None:
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "failure")
    marker = workspace / "started"
    cfg = PrepareBlock.model_validate({"script": f"touch {marker}; exit 19"})
    with pytest.raises(PrepareFailed, match="exited 19"):
        await run_prepare(cfg, _event(), workspace)
    assert marker.exists()
    assert workspace.exists()


@pytest.mark.xfail(strict=True, reason="Task 0B: current HOME/.gitconfig contaminates Git")
async def test_credentialed_git_does_not_execute_planted_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    sentinel = tmp_path / "credential-marker"
    hook = tmp_path / "fsmonitor-marker.sh"
    hook.write_text(f"#!/bin/sh\n[ \"$TOKEN\" = synthetic-token ] && touch {sentinel}\n")
    hook.chmod(0o700)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "hostile")
    (workspace / ".gitconfig").write_text(
        "[core]\n"
        f"\tfsmonitor = {hook}\n"
        f"\thooksPath = {tmp_path / 'hooks'}\n"
    )
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"; '
                'git -C "$ACH_WORKSPACE/repo" status --short'
            ),
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    await run_prepare(cfg, _event(), workspace)
    assert not sentinel.exists()


@pytest.mark.xfail(
    strict=True, reason="Task 0B: current workspace checkout follows planted .git/config"
)
async def test_credentialed_git_does_not_execute_planted_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    sentinel = tmp_path / "repo-config-marker"
    hook = tmp_path / "fsmonitor-marker.sh"
    hook.write_text(f"#!/bin/sh\n[ \"$TOKEN\" = synthetic-token ] && touch {sentinel}\n")
    hook.chmod(0o700)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "repo-hostile")
    repo = workspace / "repo"
    _git(tmp_path, "clone", "-q", str(source), str(repo))
    (repo / ".git" / "config").write_text(
        "[core]\n"
        f"\tfsmonitor = {hook}\n"
        f"\thooksPath = {tmp_path / 'hooks'}\n"
    )
    cfg = PrepareBlock.model_validate(
        {
            "script": "set -eu; git -C \"$ACH_WORKSPACE/repo\" status --short",
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    await run_prepare(cfg, _event(), workspace)
    assert not sentinel.exists()


@pytest.mark.xfail(strict=True, reason="Task 0B: current hook can follow destination symlinks")
async def test_prepare_rejects_destination_symlink_escape(tmp_path: Path) -> None:
    sentinel = tmp_path / "outside"
    sentinel.mkdir()
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "symlink")
    (workspace / "repo").mkdir()
    (workspace / "repo" / "escape").symlink_to(sentinel, target_is_directory=True)
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'mkdir -p "$ACH_WORKSPACE/repo/escape"; '
                'touch "$ACH_WORKSPACE/repo/escape/marker"'
            )
        }
    )
    await run_prepare(cfg, _event(), workspace)
    assert not (sentinel / "marker").exists()

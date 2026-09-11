# SPDX-License-Identifier: Apache-2.0
"""Task 0A characterization of private preparation and workspace handoff.

These tests deliberately exercise the current script hook, rather than a proposed
private-checkout implementation.  The security tests are strict xfails until Task
0B removes the contaminated Git configuration path.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from ach_agent.boot.prepare import PrepareFailed, prepare_workspace, run_cleanup, run_prepare
from ach_agent.boot.private_prepare import (
    PrivateBundle,
    PrivateCleanupRegistry,
    PrivatePrepareFailed,
    _git_env,
    _public_origin,
    _publish_bundle,
    _scan_git_objects,
    dispose_private_bundle,
    private_prepare,
)
from ach_agent.boot.private_prepare import _git as private_git
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock
from ach_agent.execution.wire import WorkspaceStoppedEvent


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
    assert _git(repo, "rev-parse", "origin/main") == initial_head
    assert _git(repo, "merge-base", "origin/main", "HEAD") == initial_head
    assert _git(repo, "remote", "get-url", "origin") == str(source)
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


async def test_private_prepare_produces_shared_bundle_without_destination_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Private preparation publishes metadata and a bundle, leaving destination import to H2."""
    source, initial_head = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "producer")
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"; '
                'git -C "$ACH_WORKSPACE/repo" checkout -q --detach main'
            ),
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )

    bundle = await private_prepare(cfg, _event(), workspace, tmp_path / "scratch")

    assert bundle.head == initial_head
    assert bundle.origin == str(source)  # configured shared local origins remain compatible
    assert not Path(bundle.path).is_absolute()
    assert (workspace / bundle.path).is_file()
    assert not (workspace / "repo").exists()


async def test_private_prepare_scrubs_private_scratch_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "private-origin")
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"; '
                'git -C "$ACH_WORKSPACE/repo" remote set-url origin "$ACH_WORKSPACE/repo"'
            ),
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )

    bundle = await private_prepare(cfg, _event(), workspace, tmp_path / "scratch")

    assert bundle.origin is None
    assert str(tmp_path / "scratch") not in bundle.path


async def test_private_cleanup_registry_correlates_and_acknowledges_event(
    tmp_path: Path,
) -> None:
    event = _event()
    cfg = PrepareBlock.model_validate({"script": "true"})
    registry = PrivateCleanupRegistry(max_contexts=2, max_concurrent=1)
    await registry.register(
        "invocation",
        event,
        tmp_path / "workspace",
        tmp_path / "scratch",
        cfg,
    )
    mismatched = WorkspaceStoppedEvent(
        controller_id="controller",
        instance_id="instance",
        session_key=event.session_key,
        event_id="wrong",
        invocation_id="invocation",
        workspace=str(tmp_path / "workspace"),
    )
    acknowledgements: list[str] = []
    assert not await registry.handle_event(mismatched, lambda _: _record_ack(acknowledgements))

    stopped = WorkspaceStoppedEvent(
        controller_id="controller",
        instance_id="instance",
        session_key=event.session_key,
        event_id=event.idempotency_key,
        invocation_id="invocation",
        workspace=str(tmp_path / "workspace"),
    )
    assert await registry.handle_event(stopped, lambda value: _record_ack(acknowledgements, value))
    for _ in range(100):
        if acknowledgements:
            break
        await asyncio.sleep(0.01)
    assert acknowledgements == ["invocation"]
    await registry.close()


async def test_private_cleanup_registry_finishes_active_lane_before_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = _event()
    replacement = _event(2)
    cfg = PrepareBlock.model_validate({"script": "true"})
    started = asyncio.Event()
    release = asyncio.Event()

    async def controlled_cleanup(
        _cfg: PrepareBlock, cleanup_event: MessageEvent, _workspace: Path, _scratch: Path
    ) -> None:
        if cleanup_event.idempotency_key == event.idempotency_key:
            started.set()
            await release.wait()

    monkeypatch.setattr("ach_agent.boot.private_prepare.private_cleanup", controlled_cleanup)
    registry = PrivateCleanupRegistry(max_contexts=4, max_concurrent=2)
    await registry.register("old", event, tmp_path / "workspace", tmp_path / "scratch", cfg)
    old_stopped = WorkspaceStoppedEvent(
        controller_id="controller",
        instance_id="instance",
        session_key=event.session_key,
        event_id=event.idempotency_key,
        invocation_id="old",
        workspace=str(tmp_path / "workspace"),
    )
    acknowledgements: list[str] = []
    assert await registry.handle_event(
        old_stopped, lambda value: _record_ack(acknowledgements, value)
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await registry.register(
        "replacement", replacement, tmp_path / "workspace", tmp_path / "scratch", cfg
    )
    registry.commit("replacement")
    replacement_stopped = old_stopped.model_copy(
        update={
            "event_id": replacement.idempotency_key,
            "invocation_id": "replacement",
        }
    )
    assert await registry.handle_event(
        replacement_stopped, lambda value: _record_ack(acknowledgements, value)
    )
    await asyncio.sleep(0)
    assert acknowledgements == []
    release.set()
    for _ in range(100):
        if acknowledgements == ["old", "replacement"]:
            break
        await asyncio.sleep(0.01)
    assert acknowledgements == ["old", "replacement"]
    await registry.close()


async def _record_ack(
    acknowledgements: list[str], event: WorkspaceStoppedEvent | None = None
) -> None:
    if event is not None:
        acknowledgements.append(event.invocation_id)


def test_bundle_publish_rejects_existing_artifact_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_bundle = tmp_path / "private.bundle"
    private_bundle.write_bytes(b"bundle")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.bundle"
    outside.write_bytes(b"keep")
    monkeypatch.setattr("ach_agent.boot.private_prepare.secrets.token_hex", lambda _length: "fixed")
    (workspace / ".ach-private-fixed.bundle").symlink_to(outside)

    with pytest.raises(PrivatePrepareFailed, match="could not be published"):
        _publish_bundle(private_bundle, workspace)

    assert (workspace / ".ach-private-fixed.bundle").is_symlink()
    assert outside.read_bytes() == b"keep"


def test_dispose_private_bundle_uses_owned_relative_artifact_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / ".ach-private-owned.bundle"
    artifact.write_bytes(b"bundle")
    dispose_private_bundle(PrivateBundle(artifact.name, "a" * 40, None), workspace)
    assert not artifact.exists()

    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    with pytest.raises(PrivatePrepareFailed, match="not relative"):
        dispose_private_bundle(PrivateBundle(str(outside), "a" * 40, None), workspace)
    assert outside.read_bytes() == b"keep"


def test_private_origin_scrubs_file_uri_into_scratch(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    private_repo = scratch / "prepare-1" / "work" / "repo"
    private_repo.mkdir(parents=True)
    assert _public_origin(f"file://{private_repo}", scratch) is None
    assert _public_origin("file:///configured/shared/repo", scratch) == (
        "file:///configured/shared/repo"
    )


def test_bundle_publish_rejects_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    private_bundle = tmp_path / "private.bundle"
    private_bundle.write_bytes(b"bundle")
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    workspace = real_parent / "workspace"
    workspace.mkdir()
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(PrivatePrepareFailed, match="could not be opened safely"):
        _publish_bundle(private_bundle, linked_parent / "workspace")


async def test_private_scratch_prototype_preserves_target_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credentialed scratch preparation, then local Git handoff to target."""
    source, initial_head = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "scratch")
    inode = workspace.stat().st_ino
    cfg = PrepareBlock.model_validate(
        {
            "script": """
set -eu
            git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"
""",
            "env": {
                "SOURCE": str(source),
            },
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    await run_prepare(cfg, _event(), workspace)
    assert (workspace / "repo").exists()
    sentinel = tmp_path / "handoff-token-marker"
    hook = tmp_path / "handoff-fsmonitor.sh"
    hook.write_text(f'#!/bin/sh\n[ "${{TOKEN-}}" = synthetic-token ] && touch {sentinel}\n')
    hook.chmod(0o700)
    (workspace / ".gitconfig").write_text(f"[core]\n\tfsmonitor = {hook}\n")

    transfer = _clone_block(source)
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


async def test_credentialed_git_does_not_execute_planted_global_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    sentinel = tmp_path / "credential-marker"
    hook = tmp_path / "fsmonitor-marker.sh"
    hook.write_text(f'#!/bin/sh\n[ "$TOKEN" = synthetic-token ] && touch {sentinel}\n')
    hook.chmod(0o700)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "hostile")
    (workspace / ".gitconfig").write_text(
        f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {tmp_path / 'hooks'}\n"
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


async def test_credentialed_git_does_not_execute_planted_repo_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    sentinel = tmp_path / "repo-config-marker"
    hook = tmp_path / "fsmonitor-marker.sh"
    hook.write_text(f'#!/bin/sh\n[ "$TOKEN" = synthetic-token ] && touch {sentinel}\n')
    hook.chmod(0o700)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "repo-hostile")
    repo = workspace / "repo"
    _git(tmp_path, "clone", "-q", str(source), str(repo))
    (repo / ".git" / "config").write_text(
        f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {tmp_path / 'hooks'}\n"
    )
    cfg = PrepareBlock.model_validate(
        {
            "script": 'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"',
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    await run_prepare(cfg, _event(), workspace)
    assert not sentinel.exists()


async def test_prepare_rejects_destination_symlink_escape(tmp_path: Path) -> None:
    sentinel = tmp_path / "outside"
    sentinel.mkdir()
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "symlink")
    source, _ = _local_origin(tmp_path)
    (workspace / "repo").symlink_to(sentinel, target_is_directory=True)
    cfg = PrepareBlock.model_validate(
        {
            "script": 'git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"',
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    with pytest.raises(PrepareFailed, match="destination symlink"):
        await run_prepare(cfg, _event(), workspace)
    assert not (sentinel / "marker").exists()


@pytest.mark.parametrize("kind", ["gitdir", "nested"])
async def test_private_handoff_rejects_existing_destination_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    source, _ = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), f"dest-{kind}")
    repo = workspace / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    outside = tmp_path / "outside"
    outside.mkdir()
    if kind == "gitdir":
        original_git = repo / ".git"
        moved_git = outside / "git"
        original_git.rename(moved_git)
        original_git.symlink_to(moved_git, target_is_directory=True)
        sentinel = moved_git / "sentinel"
    else:
        nested = repo / "nested"
        nested.symlink_to(outside, target_is_directory=True)
        sentinel = outside / "sentinel"
    sentinel.write_text("keep")
    cfg = PrepareBlock.model_validate(
        {
            "script": 'git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"',
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    with pytest.raises(PrepareFailed, match="symlink|real destination"):
        await run_prepare(cfg, _event(), workspace)
    assert sentinel.read_text() == "keep"


async def test_private_handoff_rejects_credential_in_published_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "bundle-secret")
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"; '
                'printf "%s" "$TOKEN" > "$ACH_WORKSPACE/repo/secret.txt"; '
                'git -C "$ACH_WORKSPACE/repo" add secret.txt; '
                'git -C "$ACH_WORKSPACE/repo" -c user.name=x -c user.email=x@example.invalid '
                'commit -qm secret; rm "$ACH_WORKSPACE/repo/secret.txt"; '
                'git -C "$ACH_WORKSPACE/repo" -c user.name=x -c user.email=x@example.invalid '
                "commit -qam remove-secret"
            ),
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    with pytest.raises(PrepareFailed, match="configured credential"):
        await run_prepare(cfg, _event(), workspace)
    assert not (workspace / "repo").exists()


async def test_private_handoff_rejects_source_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n")
    (source / "linked.txt").symlink_to(outside)
    _git(source, "add", "linked.txt")
    _git(source, "commit", "-qm", "symlink")
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "source-symlink")
    cfg = PrepareBlock.model_validate(
        {
            "script": 'git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"',
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    with pytest.raises(PrepareFailed, match="symlink"):
        await run_prepare(cfg, _event(), workspace)


async def test_private_handoff_rejects_credentialed_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _ = _local_origin(tmp_path)
    monkeypatch.setenv("PRIVATE_PREPARE_TOKEN", "synthetic-token")
    workspace = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "origin-secret")
    cfg = PrepareBlock.model_validate(
        {
            "script": (
                'set -eu; git clone -q "$SOURCE" "$ACH_WORKSPACE/repo"; '
                'git -C "$ACH_WORKSPACE/repo" remote set-url origin '
                '"https://oauth2:$TOKEN@example.invalid/repo.git"'
            ),
            "env": {"SOURCE": str(source)},
            "secretEnv": {"TOKEN": {"env": "PRIVATE_PREPARE_TOKEN"}},
        }
    )
    with pytest.raises(PrepareFailed, match="origin contains"):
        await run_prepare(cfg, _event(), workspace)
    assert not (workspace / "repo").exists()


async def test_private_git_helper_cancellation_kills_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    parent_pid = tmp_path / "parent.pid"
    child_pid = tmp_path / "child.pid"
    fake_git = bindir / "git"
    fake_git.write_text(
        "#!/bin/sh\n"
        f"echo $$ > {parent_pid}\n"
        f"(sleep 30) & child=$!; echo $child > {child_pid}; wait $child\n"
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    task = asyncio.create_task(private_git(tmp_path, "status", env=_git_env()))
    async with asyncio.timeout(2):
        while not parent_pid.exists() or not child_pid.exists():
            await asyncio.sleep(0.01)
    parent = int(parent_pid.read_text())
    child = int(child_pid.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for pid in (parent, child):
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            stat_path = Path(f"/proc/{pid}/stat")
            if stat_path.exists() and stat_path.read_text().split()[2] == "Z":
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail(f"process {pid} survived cancellation")


async def test_private_git_error_is_redacted_and_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    token = "synthetic-private-token"
    fake_git = bindir / "git"
    fake_git.write_text(
        f'#!/bin/sh\ni=0; while [ "$i" -lt 20000 ]; do printf x >&2; i=$((i + 1)); done; '
        f'printf "%s" "{token}" >&2; exit 19\n'
    )
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    with pytest.raises(PrivatePrepareFailed) as error:
        await private_git(tmp_path, "status", env=_git_env(), secret_values=(token,))
    message = str(error.value)
    assert token not in message
    assert "[REDACTED]" in message
    assert len(message) < 4300


async def test_private_object_scan_rejects_malformed_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake_git = bindir / "git"
    fake_git.write_text("#!/bin/sh\nprintf 'malformed-header\\n'\n")
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    with pytest.raises(PrivatePrepareFailed, match="invalid metadata"):
        await _scan_git_objects(tmp_path, _git_env(), ("token",))


async def test_private_object_scan_timeout_kills_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pid_file = tmp_path / "scanner.pid"
    fake_git = bindir / "git"
    fake_git.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nsleep 30\n")
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    with pytest.raises(TimeoutError):
        await _scan_git_objects(tmp_path, _git_env(), ("token",), timeout=1)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

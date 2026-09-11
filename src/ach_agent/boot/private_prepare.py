# SPDX-License-Identifier: Apache-2.0
"""Credential-bearing channel preparation in a harness-private checkout.

The shell hook remains the operator's interface.  When it names a secret, its
cwd, HOME and checkout are fresh harness-owned paths; only the resulting local
Git repository is handed to the engine workspace.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import stat
import tempfile
from pathlib import Path

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock, resolve_secret


class PrivatePrepareFailed(RuntimeError):
    """The private hook or its safe local handoff failed."""


def _reject_tree(root: Path) -> None:
    """Reject links and non-regular files before a handoff can traverse them."""
    for path in (root, *root.rglob("*")):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PrivatePrepareFailed(f"private handoff refuses symlink: {path}")
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise PrivatePrepareFailed(f"private handoff refuses special file: {path}")


def _contains_secret(root: Path, secret_values: tuple[str, ...]) -> bool:
    if not secret_values:
        return False
    for path in root.rglob("*"):
        if path.parts and ".git" in path.parts:
            continue
        if path.is_file():
            needles = tuple(value.encode() for value in secret_values)
            with path.open("rb") as stream:
                tail = b""
                while chunk := stream.read(1024 * 1024):
                    data = tail + chunk
                    if any(needle in data for needle in needles):
                        return True
                    tail = data[-max((len(needle) for needle in needles), default=1) :]
    return False


def _check_destination(repo: Path) -> None:
    """Check each existing destination component without resolving symlinks."""
    current = repo
    while current != current.parent:
        if current.exists() or current.is_symlink():
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise PrivatePrepareFailed(
                    f"private handoff refuses destination symlink: {current}"
                )
            if not stat.S_ISDIR(info.st_mode):
                raise PrivatePrepareFailed(
                    f"private handoff destination is not a directory: {current}"
                )
        current = current.parent


def _git_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


async def _git(repo: Path, *args: str, env: dict[str, str], timeout: int = 120) -> str:
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
    if repo != Path("-"):
        command.extend(["-C", str(repo)])
    command.extend(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise PrivatePrepareFailed(f"private Git handoff could not start: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    if proc.returncode:
        detail = stderr.decode("utf-8", "replace").strip()
        raise PrivatePrepareFailed(f"private Git handoff failed: {detail}")
    return stdout.decode("utf-8", "replace").strip()


async def _run_private_hook(
    cfg: PrepareBlock, event: MessageEvent, cwd: Path, home: Path
) -> None:
    from ach_agent.boot.prepare import (
        _execute_hook,
        _HookSpawnFailed,
        _HookTimedOut,
        build_prepare_env,
    )

    env = build_prepare_env(cfg, event, cwd)
    env["HOME"] = str(home)
    env["ACH_WORKSPACE"] = str(cwd)
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    try:
        code, _stdout, stderr, _truncated = await _execute_hook(
            cfg.script, cfg.timeout_seconds, cwd=cwd, env=env
        )
    except (_HookSpawnFailed, _HookTimedOut) as exc:
        raise PrivatePrepareFailed(f"private preparation failed: {exc}") from exc
    if code != 0:
        from ach_agent.boot.prepare import _stderr_tail

        raise PrivatePrepareFailed(f"private preparation exited {code}: {_stderr_tail(stderr)}")


async def _handoff(
    source: Path, workspace: Path, home: Path, secret_values: tuple[str, ...]
) -> None:
    repo = workspace / "repo"
    _check_destination(repo)
    _reject_tree(source)
    if _contains_secret(source, secret_values):
        raise PrivatePrepareFailed("private handoff contains a configured credential")
    if not (source / ".git").is_dir():
        raise PrivatePrepareFailed(
            "private preparation must produce a Git checkout at $ACH_WORKSPACE/repo"
        )
    if repo.exists() and not repo.is_dir():
        raise PrivatePrepareFailed("workspace repo is not a directory")
    env = _git_env()
    env["HOME"] = str(home)
    fd, bundle_name = tempfile.mkstemp(prefix=".ach-private-", suffix=".bundle", dir=workspace)
    os.close(fd)
    bundle = Path(bundle_name)
    bundle.unlink()
    try:
        head = await _git(source, "rev-parse", "HEAD", env=env)
        objects = await _git(source, "rev-list", "--objects", "--all", env=env)
        for object_line in objects.splitlines():
            object_id = object_line.split(maxsplit=1)[0]
            content = await _git(source, "cat-file", "-p", object_id, env=env)
            if any(value and value in content for value in secret_values):
                raise PrivatePrepareFailed("private handoff contains a configured credential")
        await _git(source, "bundle", "create", str(bundle), "--all", env=env)
        if not repo.exists():
            repo.mkdir(parents=True, mode=0o700)
            await _git(repo, "init", "-q", env=env)
        await _git(repo, "fetch", "-q", "--no-tags", str(bundle), "+refs/*:refs/ach/private/*",
                   "+refs/remotes/origin/*:refs/remotes/origin/*", env=env)
        await _git(repo, "checkout", "-q", "--force", "--detach", head, env=env)
    finally:
        with contextlib.suppress(OSError):
            bundle.unlink()


async def private_prepare(
    cfg: PrepareBlock, event: MessageEvent, workspace: Path, scratch_root: Path
) -> None:
    """Run a credential-bearing hook privately and hand off its local Git result."""
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch_root.chmod(0o700)
    with tempfile.TemporaryDirectory(dir=scratch_root, prefix="prepare-") as root:
        private = Path(root)
        home = private / "home"
        checkout = private / "work"
        home.mkdir(mode=0o700)
        checkout.mkdir(mode=0o700)
        await _run_private_hook(cfg, event, checkout, home)
        secret_values = tuple(
            value
            for src in cfg.secret_env.values()
            if (value := resolve_secret(src)) is not None
        )
        await _handoff(checkout / "repo", workspace, home, secret_values)


async def private_cleanup(
    cfg: PrepareBlock, event: MessageEvent, workspace: Path, scratch_root: Path
) -> None:
    """Run credential-bearing cleanup in private state, never in engine workspace."""
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch_root.chmod(0o700)
    with tempfile.TemporaryDirectory(dir=scratch_root, prefix="cleanup-") as root:
        private = Path(root)
        home = private / "home"
        checkout = private / "work"
        home.mkdir(mode=0o700)
        checkout.mkdir(mode=0o700)
        await _run_private_hook(cfg, event, checkout, home)

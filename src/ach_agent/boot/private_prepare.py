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
from urllib.parse import urlsplit

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock, resolve_secret
from ach_agent.engine.sanitized_env import redact_text

_ERROR_TAIL_BYTES = 4096


async def _bounded_read(stream: asyncio.StreamReader) -> bytes:
    tail = bytearray()
    while chunk := await stream.read(8192):
        tail.extend(chunk)
        if len(tail) > _ERROR_TAIL_BYTES:
            del tail[:-_ERROR_TAIL_BYTES]
    return bytes(tail)


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
    if repo.exists():
        if not (repo / ".git").is_dir() or (repo / ".git").is_symlink():
            raise PrivatePrepareFailed("private handoff requires a real destination .git directory")
        _reject_tree(repo)


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


async def _git(
    repo: Path,
    *args: str,
    env: dict[str, str],
    timeout: int = 120,
    secret_values: tuple[str, ...] = (),
) -> str:
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
    assert proc.stdout is not None and proc.stderr is not None
    stdout_task = asyncio.create_task(proc.stdout.read())
    stderr_task = asyncio.create_task(_bounded_read(proc.stderr))
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
    except (TimeoutError, asyncio.CancelledError):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        raise
    if proc.returncode:
        detail = redact_text(stderr[-_ERROR_TAIL_BYTES:].decode("utf-8", "replace").strip())
        for value in secret_values:
            detail = detail.replace(value, "[REDACTED]")
        raise PrivatePrepareFailed(f"private Git handoff failed: {detail}")
    return stdout.decode("utf-8", "replace").strip()


async def _scan_git_objects(
    source: Path,
    env: dict[str, str],
    secret_values: tuple[str, ...],
    timeout: int = 120,
) -> None:
    """Scan every object with one bounded streaming Git process."""
    if not secret_values:
        return
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(source),
        "cat-file",
        "--batch-all-objects",
        "--batch",
    ]
    proc = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout is not None
    try:
        async with asyncio.timeout(timeout):
            while header := await proc.stdout.readline():
                parts = header.rstrip(b"\n").split()
                if len(parts) != 3 or parts[1] == b"missing":
                    raise PrivatePrepareFailed("private Git object scan returned invalid metadata")
                try:
                    size = int(parts[2])
                except ValueError as exc:
                    raise PrivatePrepareFailed(
                        "private Git object scan returned invalid metadata"
                    ) from exc
                remaining = size
                tail = b""
                needles = tuple(value.encode() for value in secret_values)
                while remaining:
                    chunk = await proc.stdout.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise PrivatePrepareFailed("private Git object scan ended early")
                    data = tail + chunk
                    if any(needle in data for needle in needles):
                        raise PrivatePrepareFailed(
                            "private handoff contains a configured credential"
                        )
                    tail = data[-max((len(needle) for needle in needles), default=1) :]
                    remaining -= len(chunk)
                await proc.stdout.readexactly(1)
        await proc.wait()
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        await proc.wait()
        raise
    if proc.returncode:
        raise PrivatePrepareFailed("private Git object scan failed")


def _safe_origin(origin: str, secret_values: tuple[str, ...]) -> str:
    if not origin or any(value and value in origin for value in secret_values):
        raise PrivatePrepareFailed("private handoff origin contains a configured credential")
    if any(ord(char) < 0x20 for char in origin):
        raise PrivatePrepareFailed("private handoff origin contains control characters")
    parsed = urlsplit(origin)
    if parsed.username or parsed.password:
        raise PrivatePrepareFailed("private handoff origin contains credentials")
    return origin


async def _run_private_hook(cfg: PrepareBlock, event: MessageEvent, cwd: Path, home: Path) -> None:
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
        head = await _git(source, "rev-parse", "HEAD", env=env, secret_values=secret_values)
        await _scan_git_objects(source, env, secret_values)
        origin = None
        try:
            origin = _safe_origin(
                await _git(
                    source,
                    "remote",
                    "get-url",
                    "origin",
                    env=env,
                    secret_values=secret_values,
                ),
                secret_values,
            )
        except PrivatePrepareFailed as exc:
            if "origin contains" in str(exc):
                raise
            # A script may intentionally create a repository without a remote.
            origin = None
        await _git(
            source,
            "bundle",
            "create",
            str(bundle),
            "--all",
            env=env,
            secret_values=secret_values,
        )
        if not repo.exists():
            repo.mkdir(parents=True, mode=0o700)
            await _git(repo, "init", "-q", env=env, secret_values=secret_values)
        if origin:
            try:
                await _git(
                    repo,
                    "remote",
                    "set-url",
                    "origin",
                    origin,
                    env=env,
                    secret_values=secret_values,
                )
            except PrivatePrepareFailed:
                await _git(
                    repo,
                    "remote",
                    "add",
                    "origin",
                    origin,
                    env=env,
                    secret_values=secret_values,
                )
        await _git(
            repo,
            "fetch",
            "-q",
            "--no-tags",
            str(bundle),
            "+refs/*:refs/ach/private/*",
            "+refs/remotes/origin/*:refs/remotes/origin/*",
            env=env,
            secret_values=secret_values,
        )
        await _git(
            repo,
            "checkout",
            "-q",
            "--force",
            "--detach",
            head,
            env=env,
            secret_values=secret_values,
        )
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
            value for src in cfg.secret_env.values() if (value := resolve_secret(src)) is not None
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

# SPDX-License-Identifier: Apache-2.0
"""Engine-owned public workspace hooks and credential-free Git handoff.

Only public hook configuration enters this module.  Credential-bearing preparation stays
in the harness; the handoff consumes a validated bundle artifact in the shared workspace.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import structlog

from ach_agent.engine.lifecycle import ManagedServer
from ach_agent.engine.process_supervisor import command as supervised_command
from ach_agent.engine.sanitized_env import redact_text

if TYPE_CHECKING:
    from ach_agent.execution.wire import WorkspaceHook

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_PRINTABLE = re.compile(r"[\x20-\x7e]{1,512}")
_REPO_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*(?:/[A-Za-z0-9._][A-Za-z0-9._-]*)*")
_REPO_PATH_KEYS = frozenset({"project_path", "repo"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_HOOK_OUTPUT_TAIL_BYTES = 4096
log = structlog.get_logger(__name__)


class WorkspaceHookFailed(RuntimeError):
    """A public workspace hook failed or exceeded its bounded deadline."""


class WorkspaceHookExitFailed(WorkspaceHookFailed):
    """A public hook exited nonzero; cleanup remains best effort."""


class WorkspaceHookTimedOut(WorkspaceHookFailed):
    """A public hook exceeded its deadline or could not be reaped."""


class WorkspaceHandoffFailed(RuntimeError):
    """An approved credential-free handoff could not be imported."""


def workspace_dir(work_dir: str, session_key: str) -> Path:
    slug = _SLUG_UNSAFE.sub("-", session_key)[:64].strip("-.") or "s"
    return Path(work_dir) / f"{slug}-{hashlib.sha256(session_key.encode()).hexdigest()[:8]}"


def _ensure_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise WorkspaceHookFailed(f"workspace path is not a directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def prepare_workspace(home: str, work_dir: str, session_key: str) -> Path:
    root = Path(work_dir)
    _ensure_directory(root)
    workspace = workspace_dir(work_dir, session_key)
    _ensure_directory(workspace)
    state = Path(home) / ".ach-state"
    _ensure_directory(state)
    link = workspace / ".ach-state"
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != state.resolve():
            raise WorkspaceHookFailed(f"workspace state path is not the public state link: {link}")
    else:
        link.symlink_to(state, target_is_directory=True)
    return workspace


def _event_value(key: str, value: Any) -> str | None:
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int | float):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None
    if not _PRINTABLE.fullmatch(text):
        return None
    if key in _REPO_PATH_KEYS and not (_REPO_PATH.fullmatch(text) and ".." not in text.split("/")):
        return None
    return text


def build_public_env(
    hook: WorkspaceHook,
    *,
    session_key: str,
    event_id: str,
    channel_name: str,
    delivery_context: dict[str, Any],
    workspace: Path,
) -> dict[str, str]:
    env = {
        name: os.environ[name]
        for name in ("PATH", "SHELL", "LANG", "LANGUAGE", "TZ")
        if name in os.environ
    }
    env.update(hook.env)
    for key, value in delivery_context.items():
        name = f"ACH_EVENT_{key.upper()}"
        text = _event_value(key, value)
        if text is not None and _ENV_NAME.fullmatch(name):
            env[name] = text
    env.update(
        {
            "ACH_WORKSPACE": str(workspace),
            "ACH_SESSION_KEY": session_key,
            "ACH_EVENT_ID": event_id,
            "ACH_CHANNEL": channel_name,
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(workspace),
        }
    )
    return env


async def _read_tail(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    tail = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        tail.extend(chunk)
        if len(tail) > _HOOK_OUTPUT_TAIL_BYTES:
            del tail[:-_HOOK_OUTPUT_TAIL_BYTES]
            truncated = True
    return bytes(tail), truncated


async def run_public_hook(
    hook: WorkspaceHook,
    *,
    cwd: Path,
    env: dict[str, str],
    remaining_seconds: float,
) -> None:
    timeout = min(float(hook.timeout_seconds), remaining_seconds)
    try:
        proc = await asyncio.create_subprocess_exec(
            *supervised_command(["/bin/sh", "-eu", "-s"]),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise WorkspaceHookFailed(f"public hook could not be started: {exc}") from exc
    assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
    stdin = proc.stdin
    supervisor = ManagedServer(port=0)
    supervisor.register_process(proc, protect_root=True)
    stdout_task = asyncio.create_task(_read_tail(proc.stdout))
    stderr_task = asyncio.create_task(_read_tail(proc.stderr))
    try:

        async def communicate() -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
            try:
                stdin.write(hook.script.encode())
                await stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                stdin.close()
            await proc.wait()
            return await stdout_task, await stderr_task

        try:
            stdout, stderr = await asyncio.wait_for(communicate(), timeout)
        except TimeoutError:
            await supervisor.stop()
            raise WorkspaceHookTimedOut(f"public hook timed out after {hook.timeout_seconds}s")
        except asyncio.CancelledError:
            await asyncio.shield(supervisor.stop())
            raise
        await supervisor.stop()
        log.debug(
            "workspace hook output",
            stdout=redact_text(stdout[0].decode("utf-8", "replace")),
            stderr=redact_text(stderr[0].decode("utf-8", "replace")),
            truncated=stdout[1] or stderr[1],
            returncode=proc.returncode,
        )
        if proc.returncode:
            detail = stderr[0].decode("utf-8", "replace").strip()
            raise WorkspaceHookExitFailed(f"public hook exited {proc.returncode}: {detail}")
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)


def _check_path_components(path: Path, root: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise WorkspaceHandoffFailed("handoff path escapes workspace") from exc
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise WorkspaceHandoffFailed(f"handoff refuses symlink: {current}")


def _git_env(home: Path) -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL") if key in os.environ}
    env.update(
        {
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


async def _git(
    repo: Path,
    args: tuple[str, ...],
    *,
    env: dict[str, str],
    deadline: float,
) -> str:
    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-C",
        str(repo),
        *args,
    ]
    proc = await asyncio.create_subprocess_exec(
        *supervised_command(command),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    stdout_task = asyncio.create_task(_read_tail(proc.stdout))
    stderr_task = asyncio.create_task(_read_tail(proc.stderr))
    supervisor = ManagedServer(port=0)
    supervisor.register_process(proc, protect_root=True)
    try:
        try:

            async def communicate() -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
                await proc.wait()
                return await stdout_task, await stderr_task

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError
            stdout, stderr = await asyncio.wait_for(communicate(), remaining)
            await supervisor.stop()
        except TimeoutError:
            await supervisor.stop()
            raise WorkspaceHandoffFailed("credential-free Git handoff deadline expired")
        except asyncio.CancelledError:
            await asyncio.shield(supervisor.stop())
            raise
    finally:
        for task in (stdout_task, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
    if proc.returncode:
        detail = stderr[0].decode(errors="replace").strip()
        raise WorkspaceHandoffFailed(f"credential-free Git handoff failed: {detail}")
    return stdout[0].decode(errors="replace").strip()


async def handoff_bundle(
    *,
    home: str,
    work_dir: str,
    session_key: str,
    bundle_path: str,
    head: str,
    origin: str | None,
    remaining_seconds: float,
) -> Path:
    workspace = prepare_workspace(home, work_dir, session_key)
    relative = PurePosixPath(bundle_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkspaceHandoffFailed("handoff bundle path escapes workspace")
    deadline = asyncio.get_running_loop().time() + remaining_seconds
    artifact = workspace.joinpath(*relative.parts)
    _check_path_components(artifact, workspace)
    if not artifact.is_file():
        raise WorkspaceHandoffFailed("handoff bundle is not a regular file")
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", head):
        raise WorkspaceHandoffFailed("handoff head is not a commit id")
    if origin is not None:
        parsed = urlsplit(origin)
        if parsed.username or parsed.password or any(ord(c) < 0x20 for c in origin):
            raise WorkspaceHandoffFailed("handoff origin contains credentials or controls")
    repo = workspace / "repo"
    _check_path_components(repo, workspace)
    if repo.exists() and (repo / ".git").is_symlink():
        raise WorkspaceHandoffFailed("handoff refuses destination .git symlink")
    if repo.exists() and not repo.is_dir():
        raise WorkspaceHandoffFailed("handoff destination repo is not a directory")
    if not repo.exists():
        repo.mkdir(mode=0o700)
        await _git(
            repo,
            ("init", "-q"),
            env=_git_env(Path(home)),
            deadline=deadline,
        )
    await _git(
        repo,
        (
            "fetch",
            "-q",
            "--no-tags",
            str(artifact),
            "+refs/*:refs/ach/private/*",
            "+refs/remotes/origin/*:refs/remotes/origin/*",
        ),
        env=_git_env(Path(home)),
        deadline=deadline,
    )
    if origin:
        try:
            await _git(
                repo,
                ("remote", "set-url", "origin", origin),
                env=_git_env(Path(home)),
                deadline=deadline,
            )
        except WorkspaceHandoffFailed:
            await _git(
                repo,
                ("remote", "add", "origin", origin),
                env=_git_env(Path(home)),
                deadline=deadline,
            )
    await _git(
        repo,
        ("checkout", "-q", "--force", "--detach", head),
        env=_git_env(Path(home)),
        deadline=deadline,
    )
    with contextlib.suppress(OSError):
        artifact.unlink()
    return workspace

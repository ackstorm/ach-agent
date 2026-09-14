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
import secrets
import signal
import stat
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock, resolve_secret
from ach_agent.engine.sanitized_env import redact_text
from ach_agent.execution.wire import WorkspaceStoppedEvent

_ERROR_TAIL_BYTES = 4096
log = structlog.get_logger(__name__)


async def _bounded_read(stream: asyncio.StreamReader) -> bytes:
    tail = bytearray()
    while chunk := await stream.read(8192):
        tail.extend(chunk)
        if len(tail) > _ERROR_TAIL_BYTES:
            del tail[:-_ERROR_TAIL_BYTES]
    return bytes(tail)


class PrivatePrepareFailed(RuntimeError):
    """The private hook or its safe local handoff failed."""


@dataclass(frozen=True, slots=True)
class PrivateBundle:
    """Credential-free metadata for a bundle published in the shared workspace.

    ``path`` is deliberately relative to the public workspace.  The private scratch
    path, HOME and resolved secret values never cross the harness/engine boundary.
    """

    path: str
    head: str
    origin: str | None


@dataclass(frozen=True, slots=True)
class _PrivateCleanupContext:
    invocation_id: str
    event: MessageEvent
    workspace: Path
    cfg: PrepareBlock


class PrivateCleanupRegistry:
    """Bounded harness-private cleanup contexts for correlated engine stop events.

    The runner registers context before public preparation starts. A controller event
    selects only its stored invocation/session/event correlation; no event supplies a
    private path or hook. Cleanup tasks are bounded by the stored-context limit, so one
    slow lane cannot block ACKs for unrelated lanes.
    """

    def __init__(self, *, max_contexts: int = 64) -> None:
        if max_contexts <= 0:
            raise ValueError("cleanup registry bounds must be positive")
        self._max_contexts = max_contexts
        self._contexts: dict[str, _PrivateCleanupContext] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._task_by_invocation: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    async def register(
        self,
        invocation_id: str,
        event: MessageEvent,
        workspace: Path,
        scratch_root: Path,
        cfg: PrepareBlock,
    ) -> None:
        """Store private inputs before sending the corresponding prepare request."""
        if self._closed:
            raise PrivatePrepareFailed("private cleanup registry is closed")
        if invocation_id in self._contexts or invocation_id in self._task_by_invocation:
            raise PrivatePrepareFailed("private cleanup context is already registered")
        if len(self._contexts) + len(self._tasks) >= self._max_contexts:
            raise PrivatePrepareFailed("private cleanup context limit reached")
        del scratch_root  # retained while callers migrate from the private registry name
        self._contexts[invocation_id] = _PrivateCleanupContext(invocation_id, event, workspace, cfg)

    def commit(self, invocation_id: str) -> None:
        """Commit a successful new prepare and retire superseded pending contexts."""
        context = self._contexts.get(invocation_id)
        if context is None:
            raise PrivatePrepareFailed("private cleanup context is not pending")
        for previous, candidate in tuple(self._contexts.items()):
            if (
                previous != invocation_id
                and candidate.event.session_key == context.event.session_key
            ):
                self._contexts.pop(previous, None)

    def retire(self, invocation_id: str) -> None:
        """Remove an unused context after prepare cancellation or failed admission."""
        self._contexts.pop(invocation_id, None)

    @staticmethod
    def _observe_task(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException as exc:
            log.warning("cleanup: private callback failed", error=str(exc))

    async def handle_event(
        self,
        event: WorkspaceStoppedEvent,
        acknowledge: Callable[[WorkspaceStoppedEvent], Awaitable[None]],
    ) -> bool:
        """Run one matching private cleanup and ACK it; return false for stale events."""
        if self._closed:
            return False
        context = self._contexts.get(event.invocation_id)
        if (
            context is None
            or context.event.idempotency_key != event.event_id
            or context.event.session_key != event.session_key
        ):
            return False
        self._contexts.pop(event.invocation_id, None)
        task = asyncio.create_task(self._cleanup_and_ack(context, event, acknowledge))
        self._tasks.add(task)
        self._task_by_invocation[event.invocation_id] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._task_by_invocation.pop(event.invocation_id, None))
        task.add_done_callback(self._observe_task)
        return True

    async def _cleanup_and_ack(
        self,
        context: _PrivateCleanupContext,
        event: WorkspaceStoppedEvent,
        acknowledge: Callable[[WorkspaceStoppedEvent], Awaitable[None]],
    ) -> None:
        try:
            from ach_agent.boot.prepare import run_cleanup

            await run_cleanup(context.cfg, context.event, context.workspace)
        except Exception as exc:  # noqa: BLE001
            # Cleanup is best-effort; always release the engine callback barrier.
            log.warning(
                "cleanup: hook failed",
                invocation_id=event.invocation_id,
                error=str(exc),
            )
            await acknowledge(event)
        else:
            await acknowledge(event)

    async def close(self) -> None:
        """Retire all pending contexts and cancel owned cleanup tasks."""
        self._closed = True
        self._contexts.clear()
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._task_by_invocation.clear()


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
            "GIT_NO_LAZY_FETCH": "1",
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


def _public_origin(origin: str | None, private_root: Path) -> str | None:
    """Keep configured origins while removing references into private scratch."""
    if origin is None:
        return None
    parsed = urlsplit(origin)
    candidate: Path | None = None
    if parsed.scheme == "file":
        candidate = Path(unquote(parsed.path))
    elif not parsed.scheme:
        candidate = Path(origin)
    if candidate is not None and candidate.is_absolute():
        with contextlib.suppress(OSError, ValueError):
            if candidate.resolve().is_relative_to(private_root.resolve()):
                return None
    if parsed.scheme and parsed.netloc:
        return origin
    # Git's SCP-like syntax is a public host reference even without a URL scheme.
    if ":" in origin and not origin.startswith(("/", "./", "../")):
        return origin
    # Local/file origins are supported in native local mode when they refer to a
    # configured shared path; only references into fresh private scratch are removed.
    return origin


def _open_directory_nofollow(path: Path) -> int:
    """Open every absolute path component without traversing an attacker symlink."""
    absolute = Path(os.path.abspath(path))
    if not absolute.parts or absolute.parts[0] != os.sep:
        raise PrivatePrepareFailed("private handoff workspace path must be absolute")
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for component in absolute.parts[1:]:
            next_fd = os.open(
                component,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=fd,
            )
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        raise PrivatePrepareFailed("private handoff workspace could not be opened safely") from exc


def _publish_bundle(private_bundle: Path, workspace: Path) -> str:
    """Copy a validated private bundle through a no-follow workspace directory fd."""
    workspace_fd = _open_directory_nofollow(workspace)
    name = f".ach-private-{secrets.token_hex(8)}.bundle"
    published_fd: int | None = None
    created = False
    try:
        try:
            published_fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=workspace_fd,
            )
            created = True
        except OSError as exc:
            raise PrivatePrepareFailed("private handoff artifact could not be published") from exc
        with private_bundle.open("rb") as source, os.fdopen(published_fd, "wb") as target:
            published_fd = None
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        return name
    except BaseException:
        if published_fd is not None:
            with contextlib.suppress(OSError):
                os.close(published_fd)
        if created:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=workspace_fd)
        raise
    finally:
        os.close(workspace_fd)


def _unlink_published_bundle(workspace: Path, artifact_name: str) -> None:
    """Remove only a regular artifact name from the held, validated workspace directory."""
    if not artifact_name or Path(artifact_name).name != artifact_name:
        raise PrivatePrepareFailed("private handoff artifact reference is not relative")
    workspace_fd = _open_directory_nofollow(workspace)
    try:
        try:
            mode = os.stat(artifact_name, dir_fd=workspace_fd, follow_symlinks=False).st_mode
        except OSError:
            return
        if not stat.S_ISREG(mode):
            return
        with contextlib.suppress(OSError):
            os.unlink(artifact_name, dir_fd=workspace_fd)
    finally:
        os.close(workspace_fd)


def dispose_private_bundle(bundle: PrivateBundle, workspace: Path) -> None:
    """Safely dispose the producer-owned artifact after engine handoff or cancellation."""
    _unlink_published_bundle(workspace, bundle.path)


@asynccontextmanager
async def _private_checkout(
    cfg: PrepareBlock, event: MessageEvent, scratch_root: Path
) -> AsyncIterator[tuple[Path, Path, Path]]:
    """Create one fresh private hook checkout for producer and compatibility consumers."""
    scratch_root.mkdir(parents=True, exist_ok=True)
    scratch_root.chmod(0o700)
    with tempfile.TemporaryDirectory(dir=scratch_root, prefix="prepare-") as root:
        private = Path(root)
        home = private / "home"
        checkout = private / "work"
        home.mkdir(mode=0o700)
        checkout.mkdir(mode=0o700)
        await _run_private_hook(cfg, event, checkout, home)
        yield private, checkout, home


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
        code, stdout, stderr, truncated = await _execute_hook(
            cfg.script, cfg.timeout_seconds, cwd=cwd, env=env
        )
    except (_HookSpawnFailed, _HookTimedOut) as exc:
        raise PrivatePrepareFailed(f"private preparation failed: {exc}") from exc
    if code != 0:
        from ach_agent.boot.prepare import _stderr_tail

        raise PrivatePrepareFailed(f"private preparation exited {code}: {_stderr_tail(stderr)}")
    log.debug(
        "private workspace hook output",
        session_key=event.session_key,
        stdout=redact_text(stdout.decode("utf-8", "replace")),
        stderr=redact_text(stderr.decode("utf-8", "replace")),
        truncated=truncated,
    )


async def _produce_bundle(
    source: Path,
    workspace: Path,
    private_root: Path,
    home: Path,
    secret_values: tuple[str, ...],
) -> PrivateBundle:
    """Validate a private checkout and publish only a credential-free Git bundle."""
    _reject_tree(source)
    if _contains_secret(source, secret_values):
        raise PrivatePrepareFailed("private handoff contains a configured credential")
    if not (source / ".git").is_dir():
        raise PrivatePrepareFailed(
            "private preparation must produce a Git checkout at $ACH_WORKSPACE/repo"
        )
    env = _git_env()
    env["HOME"] = str(home)
    fd, bundle_name = tempfile.mkstemp(prefix="bundle-", suffix=".bundle", dir=private_root)
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
        try:
            await _git(
                source,
                "bundle",
                "create",
                str(bundle),
                "--all",
                env=env,
                secret_values=secret_values,
            )
        except PrivatePrepareFailed as exc:
            detail = str(exc).lower()
            if any(
                marker in detail
                for marker in (
                    "promisor",
                    "missing object",
                    "could not fetch",
                    "unable to read",
                    "pack-objects died",
                )
            ):
                raise PrivatePrepareFailed(
                    "private preparation requires a fully materialized Git checkout; "
                    "fetch missing objects while credentials are available before the hook exits"
                ) from exc
            raise
        published_name = _publish_bundle(bundle, workspace)
        return PrivateBundle(published_name, head, _public_origin(origin, private_root))
    except BaseException:
        with contextlib.suppress(OSError):
            bundle.unlink()
        raise


async def _handoff_bundle(
    bundle: PrivateBundle,
    workspace: Path,
    home: Path,
    secret_values: tuple[str, ...],
) -> None:
    """Compatibility consumer for in-process tests; split execution uses engine Git."""
    if not bundle.path or Path(bundle.path).name != bundle.path:
        raise PrivatePrepareFailed("private handoff bundle path must be a relative artifact name")
    repo = workspace / "repo"
    _check_destination(repo)
    artifact = workspace / bundle.path
    workspace_fd = _open_directory_nofollow(workspace)
    try:
        try:
            artifact_mode = os.stat(bundle.path, dir_fd=workspace_fd, follow_symlinks=False).st_mode
        except OSError:
            artifact_mode = 0
    finally:
        os.close(workspace_fd)
    if not stat.S_ISREG(artifact_mode):
        raise PrivatePrepareFailed("private handoff bundle is not a regular file")
    env = _git_env()
    env["HOME"] = str(home)
    try:
        if not repo.exists():
            repo.mkdir(parents=True, mode=0o700)
            await _git(repo, "init", "-q", env=env, secret_values=secret_values)
        if bundle.origin:
            try:
                await _git(
                    repo,
                    "remote",
                    "set-url",
                    "origin",
                    bundle.origin,
                    env=env,
                    secret_values=secret_values,
                )
            except PrivatePrepareFailed:
                await _git(
                    repo,
                    "remote",
                    "add",
                    "origin",
                    bundle.origin,
                    env=env,
                    secret_values=secret_values,
                )
        await _git(
            repo,
            "fetch",
            "-q",
            "--no-tags",
            str(artifact),
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
            bundle.head,
            env=env,
            secret_values=secret_values,
        )
    finally:
        dispose_private_bundle(bundle, workspace)


async def _handoff(
    source: Path, workspace: Path, home: Path, secret_values: tuple[str, ...]
) -> None:
    """Legacy local composition retained only for current in-process tests."""
    with tempfile.TemporaryDirectory(prefix="private-producer-") as root:
        private_root = Path(root)
        bundle = await _produce_bundle(source, workspace, private_root, home, secret_values)
        await _handoff_bundle(bundle, workspace, home, secret_values)


async def private_prepare(
    cfg: PrepareBlock, event: MessageEvent, workspace: Path, scratch_root: Path
) -> PrivateBundle:
    """Run a credential-bearing hook and publish a shared-workspace bundle descriptor."""
    async with _private_checkout(cfg, event, scratch_root) as (private, checkout, home):
        secret_values = tuple(
            value for src in cfg.secret_env.values() if (value := resolve_secret(src)) is not None
        )
        return await _produce_bundle(checkout / "repo", workspace, private, home, secret_values)


async def private_prepare_compat(
    cfg: PrepareBlock, event: MessageEvent, workspace: Path, scratch_root: Path
) -> None:
    """Compose private producer + local consumer for legacy in-process callers only."""
    async with _private_checkout(cfg, event, scratch_root) as (private, checkout, home):
        secret_values = tuple(
            value for src in cfg.secret_env.values() if (value := resolve_secret(src)) is not None
        )
        bundle = await _produce_bundle(checkout / "repo", workspace, private, home, secret_values)
        origin = bundle.origin
        if origin is None:
            with contextlib.suppress(PrivatePrepareFailed):
                origin = _safe_origin(
                    await _git(
                        checkout / "repo",
                        "remote",
                        "get-url",
                        "origin",
                        env={**_git_env(), "HOME": str(home)},
                        secret_values=secret_values,
                    ),
                    secret_values,
                )
        await _handoff_bundle(
            PrivateBundle(bundle.path, bundle.head, origin), workspace, workspace, secret_values
        )


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

# SPDX-License-Identifier: MIT
"""Session-keyed private HOME/XDG registry + agent session hooks (contract §§4,6).

Each `session_ref` gets one private HOME/scratch directory, reserved exclusively via a
per-key `asyncio.Lock` (adapted from old-ach-agent's `engine/base/pool.py` keyed-reservation
idea, without the alive-process-reuse-ignoring-new-config branch the contract explicitly
retires). Hook success markers live inside that private directory — never a Workspace-wide
marker — matching old `execution/service.py:SESSION_STARTED_MARKER`.

`session_ref` is an opaque string (contract: minted by Task 3's harness.refs, never parsed
here) — review finding #13: an opaque string is not automatically a safe path component, so
every directory is named from its SHA-256 digest, never the raw ref.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from ach_runtime.config.schema import HookBlock
from ach_runtime.engine.opencode import _kill_process_group
from ach_runtime.storage.archive import _run_owned

SESSION_STARTED_MARKER = ".ach-session-started"
DEFAULT_HOOK_TIMEOUT_SECONDS = 120.0
MAX_HOOK_OUTPUT_BYTES = 64 * 1024
HOOK_KILL_CONFIRM_SECONDS = 5.0
# Root path ruling: a fixed image/CLI default, not a new CR field — Task 9 composes roots.
DEFAULT_SESSIONS_ROOT = Path("/var/lib/ach-runtime/sessions")


_FACADE_TOKEN = re.compile(r"/t/[^/\s\"']+")


def redact_facade_tokens(text: str) -> str:
    """Facade URLs carry the workspace's facade token in their path (`/t/<token>/`), which
    works as a bearer credential: never let it reach errors, logs or A2A replies."""
    return _FACADE_TOKEN.sub("/t/<redacted>", text)


class SessionHookFailed(RuntimeError):
    """`hooks.sessionStart`/`sessionRestore` exited non-zero, timed out, or could not start."""


def _safe_dir_name(session_ref: str) -> str:
    return hashlib.sha256(session_ref.encode("utf-8")).hexdigest()


@dataclass
class SessionState:
    session_ref: str
    home: Path
    scratch: Path
    native_session_id: str | None = None
    started: bool = False
    suspend_hook_failed: bool | None = None


class SessionRegistry:
    """Reserves one private HOME/scratch pair per `session_ref` under `root`."""

    def __init__(self, root: Path = DEFAULT_SESSIONS_ROOT) -> None:
        self._root = root
        self._sessions: dict[str, SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, session_ref: str) -> asyncio.Lock:
        lock = self._locks.get(session_ref)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_ref] = lock
        return lock

    def lock(self, session_ref: str) -> asyncio.Lock:
        """Exclusive reservation for this session — held through native settlement, not
        merely through admission (review finding #3)."""
        return self._lock_for(session_ref)

    def get_or_create(self, session_ref: str) -> SessionState:
        state = self._sessions.get(session_ref)
        if state is None:
            safe = _safe_dir_name(session_ref)
            home = self._root / safe / "home"
            scratch = self._root / safe / "scratch"
            home.mkdir(parents=True, exist_ok=True)
            scratch.mkdir(parents=True, exist_ok=True)
            state = SessionState(
                session_ref=session_ref,
                home=home,
                scratch=scratch,
                started=(home / SESSION_STARTED_MARKER).exists(),
            )
            self._sessions[session_ref] = state
        return state

    def get(self, session_ref: str) -> SessionState | None:
        return self._sessions.get(session_ref)

    def inventory(self) -> list[SessionState]:
        """Return known sessions and cold sessions described by their HOME manifest."""
        if self._root.is_dir():
            for home in self._root.glob("*/home"):
                manifest_path = home / ".ach-registry.json"
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    session_ref = manifest["sessionRef"]
                    native_session_id = manifest["nativeSessionId"]
                    if not isinstance(session_ref, str) or _safe_dir_name(session_ref) != home.parent.name:
                        continue
                    state = self._sessions.get(session_ref)
                    if state is None:
                        state = SessionState(
                            session_ref=session_ref,
                            home=home,
                            scratch=home.parent / "scratch",
                            native_session_id=native_session_id,
                        )
                        self._sessions[session_ref] = state
                    elif native_session_id:
                        state.native_session_id = native_session_id
                    state.started = (home / SESSION_STARTED_MARKER).is_file()
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        return list(self._sessions.values())

    def mark_started(self, state: SessionState) -> None:
        (state.home / SESSION_STARTED_MARKER).touch()
        state.started = True

    async def retire(self, session_ref: str) -> None:
        """Delete this session's private HOME/scratch root after a durable close receipt
        (I6). Refuses a symlinked or escaped root rather than deleting the wrong tree.
        Preserves `self._locks` (a retire caller is holding that exact lock) and the
        registry entry is removed only once deletion succeeds."""
        if session_ref not in self._sessions:
            return
        session_dir = self._root / _safe_dir_name(session_ref)
        if session_dir.is_symlink():
            raise RuntimeError(f"refusing to retire a symlinked session root: {session_dir}")
        if session_dir.exists():
            if session_dir.resolve().parent != self._root.resolve():
                raise RuntimeError(f"refusing to retire an escaped session root: {session_dir}")
            _, cancelled, error = await _run_owned(shutil.rmtree, session_dir)
            if cancelled:
                raise asyncio.CancelledError()
            if error is not None:
                raise error
        del self._sessions[session_ref]


async def _read_bounded(stream: asyncio.StreamReader, *, cap: int) -> bytes:
    """Drain a pipe fully (never block the child on a full PIPE buffer) while retaining
    only the first `cap` bytes — review finding #12: unbounded capture is a memory risk."""
    collected = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        if len(collected) < cap:
            collected.extend(chunk[: cap - len(collected)])
    return bytes(collected)


@dataclass(frozen=True)
class HookResult:
    returncode: int
    stdout: str
    stderr: str
    stdout_overflow: bool = False


async def _run_supervised(script: str, *, cwd: Path, env: dict[str, str], bound: float) -> HookResult:
    """Run one hook script under owned process-group supervision.

    A timeout (or cancellation) kills and joins the whole process group, not just the shell
    leader — a hook that backgrounds a child must not leave it mutating state after
    rejection (finding #12). Output is drained fully but retained only up to
    `MAX_HOOK_OUTPUT_BYTES`; `stdout_overflow` reports a truncated stdout.
    """
    proc = await asyncio.create_subprocess_shell(
        script,
        cwd=str(cwd),
        env={**env, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    async def drain_and_wait(out_r: asyncio.StreamReader, err_r: asyncio.StreamReader) -> tuple[bytes, bytes]:
        out = await asyncio.gather(
            _read_bounded(out_r, cap=MAX_HOOK_OUTPUT_BYTES + 1),
            _read_bounded(err_r, cap=MAX_HOOK_OUTPUT_BYTES),
        )
        # Inside the bound: a hook that closes its pipes and keeps running must still time out.
        await proc.wait()
        return out

    try:
        stdout, stderr = await asyncio.wait_for(drain_and_wait(proc.stdout, proc.stderr), timeout=bound)
    except (TimeoutError, asyncio.CancelledError):
        confirmed = await _kill_process_group(proc.pid, confirm_timeout=HOOK_KILL_CONFIRM_SECONDS)
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()
        if not confirmed:
            raise SessionHookFailed(
                f"hook group did not confirm exit after timeout ({bound}s)"
            ) from None
        raise SessionHookFailed(f"hook timed out after {bound}s") from None
    assert proc.returncode is not None
    return HookResult(
        proc.returncode,
        stdout[:MAX_HOOK_OUTPUT_BYTES].decode(errors="replace"),
        stderr.decode(errors="replace"),
        stdout_overflow=len(stdout) > MAX_HOOK_OUTPUT_BYTES,
    )


async def run_gate_hook(
    hook: HookBlock,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float | None = None,
) -> HookResult:
    """Run a hook whose exit code the caller interprets (sessionStart: 0 continue, 78 skip).

    Raises `SessionHookFailed` only on timeout, unconfirmed kill or oversized stdout: the
    stdout becomes prompt text, so it is never silently truncated.
    """
    bound = timeout_seconds if timeout_seconds is not None else float(hook.timeout_seconds)
    result = await _run_supervised(hook.script, cwd=cwd, env=env, bound=bound)
    if result.stdout_overflow:
        raise SessionHookFailed(f"hook stdout exceeds {MAX_HOOK_OUTPUT_BYTES} bytes")
    return result


async def run_hook(
    hook: HookBlock | None,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: float | None = None,
) -> None:
    """Run one agent lifecycle hook to completion; a non-zero exit or a timeout fails
    closed (`SessionHookFailed`)."""
    if hook is None:
        return
    bound = timeout_seconds if timeout_seconds is not None else float(hook.timeout_seconds)
    result = await _run_supervised(hook.script, cwd=cwd, env=env, bound=bound)
    if result.returncode != 0:
        raise SessionHookFailed(
            f"hook exited {result.returncode}: {redact_facade_tokens(result.stderr)}"
        )

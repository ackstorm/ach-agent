# SPDX-License-Identifier: MIT
"""Engine seam shared by every agent engine adapter: the `Engine` protocol
`ExecutionService` drives, the invocation/receipt/snapshot types it exchanges, and the
Linux /proc PID/start-time descendant tracking used to confirm a native process tree is gone.

A bare process-group signal does not prove a detached descendant is gone, and the contract's
10s bound requires a confirmed kill, not an assumed one.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from ach_runtime.config.public import PublicConfig
from ach_runtime.observability.runs import RunUsage

CANCEL_KILL_BOUND_SECONDS = 10.0


class NativeLaunchFailed(RuntimeError):
    """The native process could not become ready, or a process-group kill was unconfirmed.

    `ambiguous=True` means the native side's receipt of the prompt send is unknown (a
    transport error, or a 200 whose body doesn't decode as the expected receipt shape —
    either way we cannot rule out that the native side accepted it) — the service layer
    must record `unknown` and alarm, never blindly resend (contract §3). A clean non-2xx
    rejection is NOT ambiguous: the native side definitively did not accept it.
    """

    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


class RestoreMismatch(RuntimeError):
    """Snapshot manifest does not match the expected destination — zero native spawn."""


EngineOutcome = Literal["running", "completed", "failed", "cancelled", "unknown"]


class ArchiveTransfer(Protocol):
    """Task 6 seam — injected, never awaited on by Task 4's own logic/tests."""

    async def upload(self, path: Path, *, session_ref: str) -> str: ...

    async def download(self, ref: str, *, dest: Path) -> None: ...


@dataclass
class EngineInvocation:
    invocation_id: str
    session_ref: str
    workspace_dir: Path
    prompt: str
    public: PublicConfig
    engine_env: dict[str, str]
    managed_agent: str = "build"
    catalog: dict[str, Any] | None = None
    mcp: dict[str, Any] | None = None
    # Test-only seam: `public.infrastructure.facade_endpoint` is schema-validated HTTPS-only
    # (correctly, for production), so it cannot point at a plain-HTTP loopback fake model
    # backend. Overrides the fixture's provider baseURL when set; production code never
    # sets this.
    provider_base_url_override: str | None = None
    resolved_system_prompt: str | None = None
    deadline_at: datetime | None = None


@dataclass
class EngineReceipt:
    native_session_id: str
    input_id: str


@dataclass
class _HaltBudget:
    """One shared monotonic clock record for a single retained cleanup (review focus 5):
    the service stamps `cleanup_started_at`/`force_deadline` at true cleanup entry, and the
    first forced kill freezes `confirmation_deadline` once — a later escalation (nested
    cleanup, a second `_kill`) must never renew either deadline."""

    cleanup_started_at: float
    force_deadline: float
    confirmation_deadline: float | None = None

    def mark_forced(self, *, at: float | None = None) -> None:
        if self.confirmation_deadline is None:
            self.confirmation_deadline = (at if at is not None else time.monotonic()) + 5.0


@dataclass
class SnapshotManifest:
    snapshot_version: str
    engine_package: str
    engine_version: str
    agent: dict[str, str]
    workspace_ref: str
    session_ref: str
    native_session_id: str
    workspace_path: str
    registry: dict[str, Any] = field(default_factory=dict)
    archive_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshotVersion": self.snapshot_version,
            "engine": {"package": self.engine_package, "version": self.engine_version},
            "agent": self.agent,
            "workspaceRef": self.workspace_ref,
            "sessionRef": self.session_ref,
            "nativeSessionId": self.native_session_id,
            "workspacePath": self.workspace_path,
            "registry": self.registry,
            "archiveRef": self.archive_ref,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SnapshotManifest:
        engine = data.get("engine") or {}
        return cls(
            snapshot_version=data["snapshotVersion"],
            engine_package=engine.get("package", ""),
            engine_version=engine.get("version", ""),
            agent=data.get("agent") or {},
            workspace_ref=data.get("workspaceRef", ""),
            session_ref=data.get("sessionRef", ""),
            native_session_id=data.get("nativeSessionId", ""),
            workspace_path=data.get("workspacePath", ""),
            registry=data.get("registry") or {},
            archive_ref=data.get("archiveRef"),
        )


# ---------------------------------------------------------------------------
# Process ownership — ported from old-ach-agent engine/lifecycle.py verbatim
# (PID/start-time guarded procfs descendant tracking, H-03).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    start_time: int


def _linux_process_info(pid: int) -> tuple[_ProcessIdentity, int, str] | None:
    if os.name != "posix" or not Path("/proc").is_dir():
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
        tail = raw[raw.rfind(b")") + 2 :].split()
        if len(tail) < 20:
            return None
        state = tail[0].decode("ascii")
        ppid = int(tail[1])
        start_time = int(tail[19])
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return _ProcessIdentity(pid, start_time), ppid, state


def _linux_descendants(
    roots: dict[int, _ProcessIdentity],
) -> dict[int, tuple[_ProcessIdentity, str]]:
    if not roots or not Path("/proc").is_dir():
        return {}
    children: dict[int, list[tuple[int, _ProcessIdentity, str]]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        info = _linux_process_info(int(entry.name))
        if info is None:
            continue
        identity, ppid, state = info
        children.setdefault(ppid, []).append((identity.pid, identity, state))
    active_roots = {
        pid
        for pid, identity in roots.items()
        if (info := _linux_process_info(pid)) is not None and info[0] == identity
    }
    pending = list(active_roots)
    seen = set(active_roots)
    found: dict[int, tuple[_ProcessIdentity, str]] = {}
    while pending:
        parent = pending.pop()
        for pid, identity, state in children.get(parent, []):
            if pid in seen:
                continue
            seen.add(pid)
            found[pid] = (identity, state)
            pending.append(pid)
    return found


class OwnedProcessCleanupError(RuntimeError):
    """Owned native processes remained live after bounded cleanup."""


async def _kill_process_group(
    pid: int, *, confirm_timeout: float, force_immediately: bool = False
) -> bool:
    """Portable fallback (no /proc): SIGTERM then SIGKILL on the process group.
    `force_immediately` skips the SIGTERM grace phase entirely and confirms the SIGKILL
    alone within `confirm_timeout` — the mode `_NativeSession._kill()` always uses, since
    by the time it is reached any graceful attempt already happened at the HTTP level
    (interfaces: "Direct `_kill` calls force immediately"). The two-phase (graceful) form
    remains available for direct low-level callers that want an OS-level SIGTERM grace
    period first — but TERM and KILL confirmation share ONE entry-anchored total window
    (TERM gets at most its first half; KILL confirmation gets only the remainder to the
    same original deadline), never two full `confirm_timeout` windows stacked."""
    entry = time.monotonic()
    deadline = entry + confirm_timeout
    if not force_immediately:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        term_deadline = min(deadline, entry + confirm_timeout / 2)
        while time.monotonic() < term_deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return True
            await asyncio.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


class _OwnedProcessGroup:
    """Peer-safe PID/start-time tracked ownership of the launched leader + its descendants."""

    def __init__(self, leader_pid: int) -> None:
        self._leader_pid = leader_pid
        self._owned: dict[int, _ProcessIdentity] = {}
        info = _linux_process_info(leader_pid)
        if info is not None:
            identity, _ppid, _state = info
            self._owned[leader_pid] = identity

    def _refresh(self) -> None:
        if not self._owned:
            return
        for pid, (identity, _state) in _linux_descendants(self._owned).items():
            self._owned.setdefault(pid, identity)

    def _live(self) -> list[_ProcessIdentity]:
        live = []
        for identity in tuple(self._owned.values()):
            info = _linux_process_info(identity.pid)
            if info is not None and info[0] == identity and info[2] != "Z":
                live.append(identity)
        return live

    def _signal(self, sig: signal.Signals) -> None:
        for identity in tuple(self._owned.values()):
            info = _linux_process_info(identity.pid)
            if info is None or info[0] != identity or info[2] == "Z":
                continue
            with contextlib.suppress(ProcessLookupError):
                os.kill(identity.pid, sig)

    async def kill_and_confirm(
        self, *, confirm_timeout: float, force_immediately: bool = False
    ) -> bool:
        """`force_immediately` skips the SIGTERM grace phase and confirms the SIGKILL alone
        within `confirm_timeout` — `_NativeSession._kill()` always uses this mode, since any
        graceful attempt already happened at the HTTP level before `_kill()` is reached
        (interfaces: "Direct `_kill` calls force immediately"). The two-phase form (each
        phase keeping its own `confirm_timeout` window) remains for direct low-level
        callers that want an OS-level SIGTERM grace period first."""
        entry = time.monotonic()
        deadline = entry + confirm_timeout
        if not Path("/proc").is_dir():
            return await _kill_process_group(
                self._leader_pid, confirm_timeout=confirm_timeout, force_immediately=force_immediately
            )
        self._refresh()
        if not self._live():
            return True
        if not force_immediately:
            self._signal(signal.SIGTERM)
            term_deadline = min(deadline, entry + confirm_timeout / 2)
            while time.monotonic() < term_deadline:
                self._refresh()
                if not self._live():
                    return True
                await asyncio.sleep(0.05)
        self._signal(signal.SIGKILL)
        while time.monotonic() < deadline:
            self._refresh()
            if not self._live():
                return True
            await asyncio.sleep(0.05)
        self._refresh()
        return not self._live()


@runtime_checkable
class Engine(Protocol):
    """What `ExecutionService` needs from an agent engine."""

    async def start(self, invocation: EngineInvocation, *, home: Path, scratch: Path) -> EngineReceipt: ...
    async def status(self, invocation_id: str) -> EngineOutcome: ...
    def last_progress(self, invocation_id: str) -> float | None: ...
    def result(self, invocation_id: str) -> str | None: ...
    def usage(self, invocation_id: str) -> RunUsage | None: ...
    async def wait_settled(self, invocation_id: str) -> EngineOutcome: ...
    async def cancel(self, invocation_id: str, reason: str, *, _halt_budget: _HaltBudget | None = None) -> None: ...
    async def snapshot(self, session_ref: str, *, agent: dict[str, str], workspace_ref: str, registry: dict[str, Any]) -> SnapshotManifest: ...
    async def suspend(self, session_ref: str) -> bool: ...
    async def retire_session(self, session_ref: str) -> None: ...
    async def restore(self, session_ref: str, manifest: SnapshotManifest, *, expected: dict[str, str], public: PublicConfig, engine_env: dict[str, str], managed_agent: str = "build", home: Path | None = None, scratch: Path | None = None, restored_home: Path | None = None, in_place: bool = False, deadline_at: datetime | None = None) -> None: ...
    async def close(self) -> None: ...

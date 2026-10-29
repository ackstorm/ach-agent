# SPDX-License-Identifier: MIT
"""Pinned OpenCode v2 native adapter (contract §4) — the exact recipe proved in Task 1's
spike (spikes/opencode-v2/serve-fixture.mjs, remainder.mjs) and written down in
task-4-reuse-handoff.md "Exact native adapter recipe", steps 1-7.

Process supervision reuses old-ach-agent's `engine/lifecycle.py` Linux /proc PID/start-time
descendant tracking (`_ProcessIdentity`/`_linux_process_info`/`_linux_descendants`) verbatim
— review finding #7: a bare process-group signal does not prove a detached descendant is
gone, and the contract's 10s bound requires a confirmed kill, not an assumed one.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import os
import secrets
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from ach_runtime.config.public import PublicConfig
from ach_runtime.engine.config import build_env, managed_content, to_fixture
from ach_runtime.observability.runs import RunUsage

OPENCODE_BIN = os.environ.get("OPENCODE_BIN", "opencode2")
STARTUP_TIMEOUT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 30.0
# Cancellation's own HTTP calls (interrupt/inbox-delete) must be short enough that two of
# them plus a settle-wait still fit inside the contract's single 10s kill bound (finding #6)
# — they are not "just another API call" on the general 30s timeout.
CANCEL_REQUEST_TIMEOUT_SECONDS = 3.0
CANCEL_KILL_BOUND_SECONDS = 10.0
EVENTS_READY_TIMEOUT_SECONDS = 10.0
READINESS_POLL_INTERVAL_SECONDS = 0.1
READINESS_TIMEOUT_SECONDS = 20.0
# Native 2.0.20 (pin 84c9be93) registers MCP tools through a 100 ms debounced registry reload
# that starts only after `/api/mcp` already reports connected, and exposes no registry-ready
# signal. Hold `connected` for that debounce plus an empirical reload margin. This is a
# version-specific stabilization window, not proof of registry readiness; revisit on upgrade.
NATIVE_MCP_TOOL_SETTLE_SECONDS = 0.3
# Continuous descendant-tracking cadence while a session is alive (H-03 parity with
# old-ach-agent's ManagedServer._observe_owned_processes) — a detached child must be
# recorded by PID/start-time before it reparents, not only when a kill is requested.
_OWNERSHIP_POLL_SECONDS = 0.05
_STDERR_TAIL_MAX_BYTES = 8192
SNAPSHOT_VERSION = "session-v1"
ENGINE_PACKAGE = "@opencode/cli"
ENGINE_VERSION = "2.0.20"


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


@dataclass
class _InvocationState:
    outcome: EngineOutcome = "unknown"
    native_session_id: str | None = None
    input_id: str | None = None
    event_seq_baseline: int = 0
    pending_delivery: bool = False
    # Set only once this specific input's own `session.inbox.delivered` event is seen —
    # a bare session-level execution.succeeded/failed/interrupted must never terminalize
    # this state before then (finding: "accepts SID-only terminal after local arrival
    # baseline" — a different input's own terminal must not finish this one).
    delivered: bool = False
    # The native `durable.seq` at which THIS input's own delivery was observed — the
    # owned-busy-period correlation key (review focus 3): only a CLOSED busy interval that
    # contains this exact sequence may settle this state.
    delivered_seq: int | None = None
    # Set when the event stream itself was lost before this invocation settled (finding:
    # stream loss must raise an ambiguous failure for service-owned cleanup, not silently
    # publish a safe "unknown" settlement).
    observation_lost: bool = False
    error: str | None = None
    # Full text from the final assistant step inside this invocation's owned busy
    # interval. The synchronous public getter additionally requires confirmed settlement.
    result: str | None = None
    settlement_confirmed: bool = False
    usage: RunUsage | None = None
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    # Monotonic time of the last `session.*` event for this native session (a message part,
    # tool state, step...): the service's stall watchdog fails a run that stops moving.
    last_progress_at: float = field(default_factory=time.monotonic)


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


class _NativeSession:
    """One `opencode2 serve --stdio` process + HTTP client, owned by one `session_ref`."""

    def __init__(self, session_ref: str, home: Path, scratch: Path) -> None:
        self.session_ref = session_ref
        self.home = home
        self.scratch = scratch
        self.url: str | None = None
        self.sid: str | None = None
        self.config_version: str | None = None
        self._managed_fingerprint: str | None = None
        self._selected_provider_model: tuple[str, str] | None = None
        self._selected_agent: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._owned: _OwnedProcessGroup | None = None
        self._client: httpx.AsyncClient | None = None
        self._auth_header: str = ""
        self._event_task: asyncio.Task[None] | None = None
        self._events_ready = asyncio.Event()
        # Each entry pairs the raw event with whether it was ACCEPTED durable evidence at
        # the moment it arrived (review focus 3/R5) — `reconcile_from_baseline` replays
        # only accepted entries, so a receipt-race replay can never apply evidence that
        # live processing would have rejected as stale/duplicate/foreign-aggregate.
        self._event_log: list[tuple[dict[str, Any], bool]] = []
        self._invocations: dict[str, _InvocationState] = {}
        self._stderr_tail = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stdout_task: asyncio.Task[None] | None = None
        self._ownership_task: asyncio.Task[None] | None = None
        # Owned-busy-period tracking for native durable-sequence correlation (review focus
        # 3): one open-or-closed interval per `session.execution.started`->terminal cycle,
        # shared across every invocation on this (possibly reused) session.
        self._busy: list[dict[str, Any]] = []
        self._last_durable_seq: int = 0
        # Retained halt/escalation task (review focus 5) — armed once per cleanup, reused by
        # a nested cleanup trigger rather than re-armed.
        self._halt_task: asyncio.Task[None] | None = None
        # Set only after `stop_cleanly()` proves a clean EOF exit with every tracked
        # resource joined — a forced/crashed/unconfirmed stop always leaves this False.
        self.clean_exit: bool = False
        # True once a *confirmed* halt (a successful `_kill()`/`stop_cleanly()`) has
        # happened — distinct from `alive is False`, which can also mean "unexpectedly
        # gone, never proven" (review focus 3/5: a dead leader alone is not proof).
        self._confirmed_halted: bool = False
        # Independent force (review focus 4): armed on its own schedule by `_arm_force`,
        # never contingent on a concurrent graceful coroutine actually cooperating with
        # cancellation. `_force_trigger` lets a caller that already has a definitive
        # answer (clean success, or a fast definitive rejection) wake it early instead of
        # waiting out the rest of its independently-scheduled delay.
        self._force_task: asyncio.Task[bool] | None = None
        self._force_trigger: asyncio.Event | None = None
        # I7: genuine per-generation EOF proof — set ONLY on an actual read/stream EOF,
        # never by a drain swallowing its own cancellation and returning early.
        self._stdout_eof: bool = False
        self._stderr_eof: bool = False
        self._events_eof: bool = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def ensure_started(
        self, *, env: dict[str, str], config_path: Path, deadline: float | None = None
    ) -> None:
        if self.alive:
            return
        if deadline is None:
            deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        if deadline <= time.monotonic():
            raise NativeLaunchFailed("native startup deadline already expired before launch")
        # R7: per-process proof belongs to THIS generation only — a stale True from a
        # prior proved-clean/halted process (or a prior ready-SSE subscription) must never
        # leak into the new one being launched here.
        self.clean_exit = False
        self._confirmed_halted = False
        self._halt_task = None  # I3/R7: a new generation starts with no retained halt
        self._stdout_eof = False
        self._stderr_eof = False
        self._events_eof = False
        self._events_ready = asyncio.Event()
        env = {**env, "OPENCODE_CONFIG": str(config_path)}
        password = env["OPENCODE_SERVER_PASSWORD"]
        self._auth_header = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
        self.home.mkdir(parents=True, exist_ok=True)
        self.scratch.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            OPENCODE_BIN,
            "serve",
            "--stdio",
            "--hostname",
            "127.0.0.1",
            "--port",
            "0",
            cwd=str(self.home),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        self._proc = proc
        self._owned = _OwnedProcessGroup(proc.pid)
        self._ownership_task = asyncio.create_task(self._observe_owned())

        async def read_url() -> str:
            assert proc.stdout is not None
            buf = b""
            max_buf = 64 * 1024  # a URL line is a few hundred bytes; cap runaway startup noise
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    raise NativeLaunchFailed(
                        f"opencode2 serve --stdio exited before printing a URL "
                        f"(code={proc.returncode}): {self._tail()}"
                    )
                buf = (buf + chunk)[-max_buf:]
                for line in buf.split(b"\n"):
                    try:
                        payload = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if isinstance(payload, dict) and "url" in payload:
                        return str(payload["url"])

        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            self.url = await asyncio.wait_for(
                read_url(), timeout=max(0.0, deadline - time.monotonic())
            )
        except (TimeoutError, NativeLaunchFailed, asyncio.CancelledError):
            await self._kill(confirm_timeout=5.0)
            raise
        self._stdout_task = asyncio.create_task(self._drain_stdout())
        self._client = httpx.AsyncClient(
            base_url=self.url, headers={"authorization": self._auth_header}
        )
        self._event_task = asyncio.create_task(self._consume_events())
        try:
            await asyncio.wait_for(
                self._events_ready.wait(), timeout=max(0.0, deadline - time.monotonic())
            )
        except TimeoutError as exc:
            await self._kill(confirm_timeout=5.0)
            raise NativeLaunchFailed("SSE subscription never became ready") from exc
        except asyncio.CancelledError:
            await self._kill(confirm_timeout=5.0)
            raise

    async def _drain_stderr(self) -> None:
        """4096-byte chunk reads, never `readline()` (review focus 4: a pipe that never
        emits a newline — `test_large_no_newline_pipes_are_drained_and_joined` — must still
        be drained; `readline()` would block forever on one). Retains only the last
        `_STDERR_TAIL_MAX_BYTES` for the diagnostic tail."""
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    self._stderr_eof = True
                    return
                self._stderr_tail.extend(chunk)
                del self._stderr_tail[: max(0, len(self._stderr_tail) - _STDERR_TAIL_MAX_BYTES)]
        except asyncio.CancelledError:
            return

    async def _drain_stdout(self) -> None:
        """Keep reading stdout after the URL line so the native process never blocks on a
        full pipe buffer (finding: "stdout stops draining after URL discovery")."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                chunk = await self._proc.stdout.read(4096)
                if not chunk:
                    self._stdout_eof = True
                    return
        except asyncio.CancelledError:
            return

    async def _observe_owned(self) -> None:
        """Continuously refresh tracked descendants while this session is alive (finding:
        "descendant refresh occurs during kill only") — a detached child must be recorded
        by PID/start-time before it reparents, not discovered for the first time at kill."""
        try:
            while True:
                if self._owned is not None:
                    self._owned._refresh()
                await asyncio.sleep(_OWNERSHIP_POLL_SECONDS)
        except asyncio.CancelledError:
            return

    def _tail(self) -> str:
        return bytes(self._stderr_tail).decode(errors="replace")

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> tuple[int, Any]:
        assert self._client is not None
        response = await self._client.request(
            method, path, json=json_body, params=params, timeout=timeout
        )
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body

    async def _consume_events(self) -> None:
        assert self._client is not None
        try:
            async with self._client.stream("GET", "/api/event", timeout=None) as response:
                if response.status_code != 200:
                    # Never mark readiness on an error page (finding: SSE non-200 must not
                    # be treated as a ready subscription) — ensure_started's own bounded
                    # EVENTS_READY_TIMEOUT_SECONDS wait times out and fails the launch.
                    return
                self._events_ready.set()
                buf = ""
                async for chunk in response.aiter_text():
                    buf += chunk
                    while "\n\n" in buf:
                        frame, buf = buf.split("\n\n", 1)
                        data_line = next(
                            (l for l in frame.splitlines() if l.startswith("data:")), None
                        )
                        if data_line is None:
                            continue
                        try:
                            event = json.loads(data_line[len("data:") :].strip())
                        except ValueError:
                            continue
                        self._record_and_dispatch(event)
                # I7: the server closed the stream on its own — genuine EOF, never set on
                # a cancelled/errored subscription.
                self._events_eof = True
        except (httpx.HTTPError, asyncio.CancelledError):
            return
        finally:
            # Stream end/error is itself evidence: a status() caller must not keep reporting
            # "running" against a server that stopped talking to us (finding #5).
            for state in self._invocations.values():
                if state.input_id is not None and not state.settled.is_set():
                    state.observation_lost = True
                    state.outcome = "unknown"
                    state.settled.set()

    def _record_and_dispatch(self, event: dict[str, Any]) -> None:
        durable = event.get("durable")
        accepted = True
        if isinstance(durable, dict) and isinstance(durable.get("seq"), int):
            seq = durable["seq"]
            accepted = durable.get("aggregateID") == self.sid and seq > self._last_durable_seq
            if accepted:
                self._last_durable_seq = seq
        self._event_log.append((event, accepted))
        if not accepted:
            # Stale/duplicate/foreign-aggregate durable evidence (review focus 3/R5): never
            # invented execution identity — dropped before it can perturb busy-interval
            # tracking OR a same-shaped inbox-delivery match, live or on later replay.
            return
        if isinstance(durable, dict):
            self._apply_durable_event(event.get("type"), event.get("data") or {}, durable["seq"])
        data = event.get("data")
        if (
            str(event.get("type")).startswith("session.")
            and isinstance(data, dict)
            and data.get("sessionID") == self.sid
        ):
            now = time.monotonic()
            for state in self._invocations.values():
                state.last_progress_at = now
        for state in self._invocations.values():
            if state.input_id is None or state.settled.is_set():
                continue
            self._apply_event(state, event)
        self._resolve_pending()

    def _apply_durable_event(self, kind: str | None, data: dict[str, Any], seq: int) -> None:
        """Owned-busy-period tracking (review focus 3): one interval per `started`->terminal
        cycle on this aggregate (session), by native sequence — never by local arrival
        order. `start precedes drain`: a terminal with no currently-open interval is itself
        uncertainty (a lost/missing start), so it closes nothing and settles no one."""
        if kind == "session.execution.started":
            self._busy.append(
                {
                    "start": seq,
                    "end": None,
                    "outcome": None,
                    "error": None,
                    "used": False,
                    "assistant_id": None,
                    "texts": {},
                    "step_starts": set(),
                    "compaction_starts": [],
                    "compactions": [],
                    "delivered": set(),
                    "usage": {"input": None, "output": None, "reasoning": None,
                              "cache_read": None, "cache_write": None, "cost_usd": None,
                              "steps": 0},
                }
            )
        elif kind == "session.step.started":
            if data.get("sessionID") != self.sid:
                return
            interval = next((item for item in reversed(self._busy) if item["end"] is None), None)
            assistant_id = data.get("assistantMessageID")
            if interval is not None and isinstance(assistant_id, str):
                interval["assistant_id"] = assistant_id
                interval["texts"] = {}
                interval["step_starts"].add(assistant_id)
        elif kind == "session.inbox.delivered":
            if data.get("sessionID") != self.sid:
                return
            interval = next((item for item in reversed(self._busy) if item["end"] is None), None)
            input_id = data.get("inboxID")
            if interval is not None and isinstance(input_id, str):
                interval["delivered"].add(input_id)
        elif kind == "session.compaction.started":
            if data.get("sessionID") != self.sid:
                return
            interval = next((item for item in reversed(self._busy) if item["end"] is None), None)
            if interval is not None:
                input_id = data.get("inputID")
                interval["compaction_starts"].append(input_id if isinstance(input_id, str) else None)
        elif kind in ("session.step.ended", "session.step.failed"):
            if data.get("sessionID") != self.sid:
                return
            interval = next((item for item in reversed(self._busy) if item["end"] is None), None)
            assistant_id = data.get("assistantMessageID")
            if interval is not None and isinstance(assistant_id, str) and assistant_id in interval["step_starts"]:
                interval["step_starts"].remove(assistant_id)
                interval["usage"]["steps"] += 1
                self._accumulate_usage(interval["usage"], data)
        elif kind in ("session.compaction.ended", "session.compaction.failed"):
            if data.get("sessionID") != self.sid:
                return
            interval = next((item for item in reversed(self._busy) if item["end"] is None), None)
            input_id = data.get("inputID")
            if interval is not None:
                starts = interval["compaction_starts"]
                terminal_input = input_id if isinstance(input_id, str) else None
                if terminal_input is None:
                    match = 0 if starts else None
                else:
                    match = next((index for index, started_input in enumerate(starts)
                                  if started_input == terminal_input), None)
                    if match is None:
                        match = next((index for index, started_input in enumerate(starts)
                                      if started_input is None), None)
                if match is not None:
                    started_input = starts.pop(match)
                    owner_input = started_input or terminal_input
                    interval["compactions"].append({"input_id": owner_input, "data": dict(data)})
        elif kind == "session.text.ended":
            if data.get("sessionID") != self.sid:
                return
            interval = next((item for item in reversed(self._busy) if item["end"] is None), None)
            ordinal = data.get("ordinal")
            text = data.get("text")
            if (
                interval is not None
                and data.get("assistantMessageID") == interval["assistant_id"]
                and isinstance(ordinal, int)
                and isinstance(text, str)
            ):
                interval["texts"][ordinal] = text
        elif kind in (
            "session.execution.succeeded",
            "session.execution.failed",
            "session.execution.interrupted",
        ):
            for interval in reversed(self._busy):
                if interval["end"] is None:
                    interval["end"] = seq
                    if kind == "session.execution.succeeded":
                        interval["outcome"] = "completed"
                    elif kind == "session.execution.failed":
                        interval["outcome"] = "failed"
                        interval["error"] = json.dumps(data)[:2000]
                    else:
                        interval["outcome"] = "cancelled"
                    texts = interval["texts"]
                    if texts:
                        interval["result"] = "".join(texts[index] for index in sorted(texts))
                    break

    def _resolve_pending(self) -> None:
        for state in self._invocations.values():
            if not state.delivered or state.settled.is_set() or state.delivered_seq is None:
                continue
            for interval in self._busy:
                if interval["used"] or interval["end"] is None:
                    continue
                if interval["start"] <= state.delivered_seq <= interval["end"]:
                    interval["used"] = True
                    state.outcome = interval["outcome"] or "unknown"
                    state.error = interval["error"]
                    state.result = interval.get("result")
                    usage = self._usage_for_interval(state, interval)
                    state.usage = self._usage_record(usage)
                    state.settled.set()
                    break

    @staticmethod
    def _accumulate_usage(target: dict[str, Any], data: dict[str, Any]) -> None:
        tokens = data.get("tokens")
        cache = tokens.get("cache") if isinstance(tokens, dict) else None
        values = {
            "input": tokens.get("input") if isinstance(tokens, dict) else None,
            "output": tokens.get("output") if isinstance(tokens, dict) else None,
            "reasoning": tokens.get("reasoning") if isinstance(tokens, dict) else None,
            "cache_read": cache.get("read") if isinstance(cache, dict) else None,
            "cache_write": cache.get("write") if isinstance(cache, dict) else None,
            "cost_usd": data.get("cost"),
        }
        for key, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                total = (target[key] or 0) + value
                if math.isfinite(total):
                    target[key] = total

    def _usage_for_interval(
        self, state: _InvocationState, interval: dict[str, Any]
    ) -> dict[str, Any]:
        usage = dict(interval["usage"])
        for compaction in interval["compactions"]:
            input_id = compaction["input_id"]
            if input_id == state.input_id or (
                input_id is None
                and len(interval["delivered"]) == 1
                and state.input_id in interval["delivered"]
            ):
                self._accumulate_usage(usage, compaction["data"])
        return usage

    @staticmethod
    def _usage_record(values: dict[str, Any]) -> RunUsage | None:
        fields = ("input", "output", "reasoning", "cache_read", "cache_write", "cost_usd")
        if not any(values[key] is not None for key in fields) and not values["steps"]:
            return None
        return RunUsage(
            source="opencode", basis="engine_estimate", input=values["input"],
            output=values["output"], reasoning=values["reasoning"],
            cache_read=values["cache_read"], cache_write=values["cache_write"],
            cost_usd=values["cost_usd"], steps=values["steps"],
        )

    def _apply_event(self, state: _InvocationState, event: dict[str, Any]) -> None:
        kind = event.get("type")
        data = event.get("data") or {}
        if kind in ("session.inbox.enqueued", "session.inbox.delivered"):
            if data.get("sessionID") == self.sid and data.get("inboxID") == state.input_id:
                state.pending_delivery = kind == "session.inbox.enqueued"
                if kind == "session.inbox.delivered":
                    state.delivered = True
                    durable = event.get("durable")
                    if isinstance(durable, dict) and isinstance(durable.get("seq"), int):
                        state.delivered_seq = durable["seq"]
                if state.outcome == "unknown":
                    state.outcome = "running"

    def reconcile_from_baseline(self, state: _InvocationState) -> None:
        """Replay buffered events for an invocation whose `input_id` was assigned after
        some events had already arrived (finding #5: a fast terminal must not be dropped
        just because it raced the prompt-send HTTP response) — durable-sequence intervals
        are already fully built from the raw log, so this only needs to replay the
        delivery-matching projection and then re-attempt resolution."""
        for event, accepted in self._event_log[state.event_seq_baseline :]:
            if state.settled.is_set():
                return
            if not accepted:
                continue
            self._apply_event(state, event)
        self._resolve_pending()

    async def _kill(
        self, *, confirm_timeout: float, _halt_budget: _HaltBudget | None = None
    ) -> bool:
        """Repeated/nested calls (force_task's own fire, a direct caller, a launch
        failure path) converge on ONE retained halt/result task (I3/review focus 5) —
        never a second concurrent signal/resource-join race over the same session."""
        if self._halt_task is not None and not self._halt_task.done():
            return await self._halt_task
        self._halt_task = asyncio.ensure_future(
            self._perform_kill(confirm_timeout=confirm_timeout, _halt_budget=_halt_budget)
        )
        return await self._halt_task

    async def _perform_kill(
        self, *, confirm_timeout: float, _halt_budget: _HaltBudget | None = None
    ) -> bool:
        """Direct calls (no `_halt_budget`) force immediately with `confirm_timeout` as one
        total confirmation/join budget; engine cancellation passes the shared budget and
        time already spent escalating (interfaces: "Direct `_kill` calls force immediately
        ... engine cancellation supplies the shared budget instead"). When a budget IS
        given, `_kill` computes its own remaining-time-on-that-shared-deadline (R3) rather
        than trusting a caller-precomputed window, and freezes it on first force if that
        has not already happened."""
        self.clean_exit = False  # R7: beginning force invalidates this generation's proof
        if _halt_budget is None:
            # Direct callers (no shared budget) get one entry-anchored total capped at 5s,
            # never a caller-supplied oversized window.
            confirm_timeout = min(confirm_timeout, 5.0)
        if self._ownership_task is not None:
            self._ownership_task.cancel()
        if self._event_task is not None:
            self._event_task.cancel()
        if self._stdout_task is not None:
            self._stdout_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()

        # A leader that already exited must still be confirmed clear of its own tracked
        # descendants (finding: "_kill bypasses tracked children once the leader exits") —
        # never short-circuit on `self._proc.returncode is not None` before that check.
        async def _signal_and_confirm() -> bool:
            if self._proc is None:
                return True
            # I5: the stamp happens here — exactly when a live owned identity is about to
            # be signaled, synchronously before any awaited confirmation work — never
            # preemptively, and never when no live target remains to force.
            if self._owned is not None:
                self._owned._refresh()
            live = bool(self._owned._live()) if self._owned is not None else self._proc.returncode is None
            local_timeout = confirm_timeout
            if _halt_budget is not None:
                if live:
                    _halt_budget.mark_forced()
                if _halt_budget.confirmation_deadline is not None:
                    local_timeout = max(0.001, _halt_budget.confirmation_deadline - time.monotonic())
            if self._owned is not None:
                return await self._owned.kill_and_confirm(
                    confirm_timeout=local_timeout, force_immediately=True
                )
            return await _kill_process_group(
                self._proc.pid, confirm_timeout=local_timeout, force_immediately=True
            )

        # Signal immediately, concurrently with closing the HTTP client — `aclose()` must
        # never delay the kill signal (interfaces: "`_kill` signals before client close").
        kill_coro = asyncio.ensure_future(_signal_and_confirm())
        confirmed = await kill_coro

        # I3: every remaining resource join (close, process reap, readers/SSE/observer) is
        # bounded against the SAME single absolute deadline — a held/failed/unjoined
        # resource prevents a successful halt rather than being suppressed into a silent
        # True; an unresolved resource is retained, never cancelled, on expiry.
        if _halt_budget is not None:
            shared_deadline = _halt_budget.confirmation_deadline
            if shared_deadline is None:
                shared_deadline = _halt_budget.force_deadline
        else:
            shared_deadline = time.monotonic() + confirm_timeout

        def _remaining() -> float:
            return max(0.001, shared_deadline - time.monotonic())

        close_ok = True
        if self._client is not None:
            close_task = asyncio.ensure_future(self._client.aclose())
            done, _pending = await asyncio.wait({close_task}, timeout=_remaining())
            if close_task in done:
                close_ok = close_task.exception() is None
            else:
                close_ok = False  # retained, not cancelled — still pending/unjoined

        if self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=_remaining())
            except (TimeoutError, ProcessLookupError):
                if self._proc.returncode is None:
                    confirmed = False

        resource_tasks = [
            task
            for task in (self._ownership_task, self._event_task, self._stdout_task, self._stderr_task)
            if task is not None
        ]
        resources_ok = True
        if resource_tasks:
            done, pending = await asyncio.wait(resource_tasks, timeout=_remaining())
            if pending:
                resources_ok = False  # retained, never torn down on expiry
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    resources_ok = False

        confirmed = confirmed and close_ok and resources_ok
        self._confirmed_halted = confirmed
        return confirmed

    def _arm_force(self, *, budget: _HaltBudget) -> asyncio.Task[bool]:
        """Independent force (review focus 4): arms a background task that performs the
        actual owned SIGKILL on its own schedule — `max(budget.cleanup_started_at,
        budget.force_deadline - 0.05)` — regardless of whether any concurrent graceful or
        cancellation path ever cooperates or completes (not a `wait_for`-cancellation of
        that path). A caller that already has a definitive answer before the schedule
        fires may wake it early via `_force_trigger` instead of waiting out the rest of
        the delay. Repeated/nested calls for the same cleanup reuse the same armed task."""
        if self._force_task is not None and not self._force_task.done():
            return self._force_task
        trigger = asyncio.Event()
        self._force_trigger = trigger

        async def _fire() -> bool:
            fire_at = max(budget.cleanup_started_at, budget.force_deadline - 0.05)
            delay = max(0.0, fire_at - time.monotonic())
            if delay > 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(trigger.wait(), timeout=delay)
            return await self._kill(confirm_timeout=5.0, _halt_budget=budget)

        self._force_task = asyncio.ensure_future(_fire())
        return self._force_task

    async def stop_cleanly(self, *, timeout_seconds: float = 5.0) -> bool:
        """Requested-EOF clean stop (contract §6 `sessionSuspend` barrier): close stdin
        (the documented clean-stop signal) and require REAL proof of a tidy exit — exit0,
        no live tracked descendants, genuine stdout/stderr EOF, and every reader/observer/
        client resource actually joined — before trusting it. Native shutdown can truncate
        HTTP chunk framing on the SSE observer; that framing EOF is not a process durability
        condition after the requested exit and joined resources. A forced/crashed/unconfirmed
        stop always returns False, even when the forced halt itself succeeds; `clean_exit`
        is set only on proven-clean exit."""
        if not self.alive:
            return self.clean_exit
        deadline_seconds = min(timeout_seconds, 10.0)
        deadline = time.monotonic() + deadline_seconds
        assert self._proc is not None and self._proc.stdin is not None
        with contextlib.suppress(ProcessLookupError, BrokenPipeError):
            self._proc.stdin.close()
        confirmed = False
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=deadline_seconds)
            if self._proc.returncode == 0:
                if self._owned is not None:
                    self._owned._refresh()
                live = self._owned._live() if self._owned is not None else []
                if not live:
                    # The ownership observer has no natural end condition. The SSE
                    # subscription is also only an observer: an incomplete terminating
                    # chunk after native exit is not evidence against the process stop.
                    # Stop both explicitly, then bound one join of every owned reader.
                    if self._ownership_task is not None:
                        self._ownership_task.cancel()
                    if self._event_task is not None:
                        self._event_task.cancel()

                    resources = {
                        task
                        for task in (
                            self._ownership_task,
                            self._event_task,
                            self._stdout_task,
                            self._stderr_task,
                        )
                        if task is not None
                    }
                    done, pending = await asyncio.wait(
                        resources, timeout=max(0.0, deadline - time.monotonic())
                    )
                    resources_joined = not pending
                    for task in done:
                        if task in (self._ownership_task, self._event_task) and task.cancelled():
                            continue
                        if task.cancelled():
                            resources_joined = False
                        elif task.exception() is not None:
                            resources_joined = False
                    if self._stdout_task is not None and not self._stdout_eof:
                        resources_joined = False
                    if self._stderr_task is not None and not self._stderr_eof:
                        resources_joined = False
                    if resources_joined:
                        try:
                            if self._client is not None:
                                close_task = asyncio.create_task(self._client.aclose())
                                close_done, _close_pending = await asyncio.wait(
                                    {close_task}, timeout=max(0.0, deadline - time.monotonic())
                                )
                                if close_task not in close_done:
                                    resources_joined = False
                                    # Keep ownership of a close that outlives this bounded
                                    # proof attempt; a pending task must not be orphaned.
                                    self._client_close_task = close_task
                                elif close_task.cancelled():
                                    resources_joined = False
                                elif close_task.exception() is not None:
                                    resources_joined = False
                                elif not self._client.is_closed:
                                    resources_joined = False
                        except Exception:  # noqa: BLE001 - a failed/unbounded close is not clean proof
                            resources_joined = False
                        if self._owned is not None:
                            self._owned._refresh()
                            resources_joined = resources_joined and not self._owned._live()
                        confirmed = resources_joined
        except TimeoutError:
            confirmed = False
        if confirmed:
            self.clean_exit = True
            self._confirmed_halted = True
            return True
        self.clean_exit = False
        await self._kill(confirm_timeout=5.0)
        return False


def _expects_native_system(fixture: dict[str, Any]) -> bool:
    """Only `compose: replace` sets the native agent's `system`; `append` pushes the prompt
    through a session `context` hook and no prompt leaves OpenCode's default in place."""
    return bool(fixture.get("systemPrompt")) and fixture.get("promptCompose") == "replace"


async def _await_native_readiness(
    session: _NativeSession,
    *,
    wid: str,
    managed_agent: str,
    required_mcp: list[str],
    provider_id: str,
    model_id: str,
    max_steps: int,
    expect_system: bool = True,
    deadline: float | None = None,
) -> None:
    """Poll `/api/config`, `/api/model`, `/api/agent/:managedAgent` and (if any MCP entries
    were injected) `/api/mcp` for connected status — all scoped to this location (contract
    §4 step 3; finding #9: "connected readiness remains required when entries are
    injected"). A native provider failure *after* Started is not a readiness rejection; this
    runs strictly before the first prompt. `deadline` is one absolute monotonic bound
    shared with launch/URL/SSE (policy gap I5) rather than this phase's own fresh timer.
    """
    if deadline is None:
        deadline = time.monotonic() + READINESS_TIMEOUT_SECONDS

    async def poll(path: str, *, ok: Any) -> None:
        while True:
            status: int | None = None
            body: Any = "request timed out"
            with contextlib.suppress(TimeoutError):
                # Each request gets only the remaining budget, so a stalled endpoint cannot
                # outlive the shared startup deadline.
                status, body = await asyncio.wait_for(
                    session.request("GET", path, params={"location[directory]": wid}),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            if status == 200 and ok(body):
                return
            if time.monotonic() >= deadline:
                await session._kill(confirm_timeout=5.0)
                raise NativeLaunchFailed(f"native readiness timed out at {path}: {status} {body!r}")
            await asyncio.sleep(READINESS_POLL_INTERVAL_SECONDS)

    await poll("/api/config", ok=lambda body: isinstance(body, list))

    def _desired_model(body: Any) -> bool:
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            return False
        return any(
            isinstance(model, dict)
            and model.get("providerID") == provider_id
            and model.get("id") == model_id
            and model.get("enabled", True) is True
            for model in body["data"]
        )

    await poll("/api/model", ok=_desired_model)

    def _desired_agent(body: Any) -> bool:
        data = body.get("data") if isinstance(body, dict) else None
        return (
            isinstance(data, dict)
            and data.get("id", managed_agent) == managed_agent
            and data.get("steps") == max_steps
            # Only compose=replace puts a native `system` on the agent (see _expects_native_system).
            and (not expect_system or (isinstance(data.get("system"), str) and bool(data["system"])))
        )

    await poll(f"/api/agent/{managed_agent}", ok=_desired_agent)
    if required_mcp:

        def _mcp_connected(body: Any) -> bool:
            # Observed real shape: {"location": {...}, "data": [{"name", "status":
            # {"status": "connected"}}, ...]} — a list keyed by `name`, not a dict keyed
            # by server name (the handoff's "raw Config.Entry array" phrasing applies to
            # /api/config, not /api/mcp; verified against the pinned binary, not guessed).
            if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                return False
            by_name = {
                entry.get("name"): entry
                for entry in body["data"]
                if isinstance(entry, dict)
            }
            return all(
                (by_name.get(name, {}).get("status") or {}).get("status") == "connected"
                for name in required_mcp
            )

        connected_since: float | None = None

        def _mcp_settled(body: Any) -> bool:
            nonlocal connected_since
            if not _mcp_connected(body):
                connected_since = None
                return False
            now = time.monotonic()
            if connected_since is None:
                connected_since = now
            return now - connected_since >= NATIVE_MCP_TOOL_SETTLE_SECONDS

        await poll("/api/mcp", ok=_mcp_settled)


def _managed_fingerprint(content: dict[str, Any], env: dict[str, str]) -> str:
    """Hash the effective managed config and approved process environment, excluding auth."""
    effective_env = {key: value for key, value in env.items() if key != "OPENCODE_SERVER_PASSWORD"}
    payload = json.dumps(
        {"content": content, "env": effective_env}, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _parse_inbox_items(body: Any, *, session_sid: str) -> list[str] | None:
    """Strict native Inbox.Info validation (review focus 3/R5), shared identically by
    ordinary settlement and cancellation so neither path accepts a looser shape than the
    other: every item must be a dict with a nonempty string id, and any OTHER field it
    does carry (sessionID/type/delivery/payload) must match the expected shape — a
    malformed response returns None (never a silent "not pending" guess)."""
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        return None
    ids: list[str] = []
    for item in body["data"]:
        if not isinstance(item, dict):
            return None
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return None
        if "sessionID" in item and item["sessionID"] != session_sid:
            return None
        if "type" in item and item["type"] not in ("user", "assistant"):
            return None
        if "delivery" in item and item["delivery"] not in ("queue", "steer"):
            return None
        if "payload" in item and not isinstance(item["payload"], dict):
            return None
        ids.append(item_id)
    return ids


class OpenCodeEngine:
    """The `start/status/cancel/snapshot/restore` protocol fixed by contract §4."""

    def __init__(self, *, transfer: ArchiveTransfer | None = None) -> None:
        self._sessions: dict[str, _NativeSession] = {}
        self._transfer = transfer

    def _session_for(self, session_ref: str, home: Path, scratch: Path) -> _NativeSession:
        session = self._sessions.get(session_ref)
        if session is None:
            session = _NativeSession(session_ref, home, scratch)
            self._sessions[session_ref] = session
        return session

    async def close(self) -> None:
        """Close only native handles this engine actually created."""
        sessions = tuple(self._sessions.values())
        if not sessions:
            return
        results = await asyncio.gather(
            *(
                session.stop_cleanly(timeout_seconds=5.0)
                if session.alive
                else asyncio.sleep(0, result=session._confirmed_halted)
                for session in sessions
            ),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, BaseException) or result is not True]
        if failures:
            detail = "; ".join(str(item) for item in failures)
            raise RuntimeError(f"native session cleanup was not confirmed: {detail}")

    @staticmethod
    def _startup_deadline(
        startup_timeout_seconds: float, deadline_at: datetime | None
    ) -> float:
        """One absolute monotonic native-startup deadline (I5), shared across launch/URL/
        SSE/readiness rather than each phase's own fixed/refreshed timer: the configured
        `engine.startup_timeout_seconds`, clipped by whatever remains on the original
        invocation/restore deadline. No deadline (restore without a ticket) keeps the
        configured bound as-is."""
        bound = startup_timeout_seconds
        if deadline_at is not None:
            remaining = (deadline_at - datetime.now(timezone.utc)).total_seconds()
            bound = max(0.0, min(startup_timeout_seconds, remaining))
        return time.monotonic() + bound

    async def start(
        self, invocation: EngineInvocation, *, home: Path, scratch: Path, password: str | None = None
    ) -> EngineReceipt:
        session = self._session_for(invocation.session_ref, home, scratch)
        deadline = self._startup_deadline(
            invocation.public.engine.startup_timeout_seconds, invocation.deadline_at
        )
        home.mkdir(parents=True, exist_ok=True)
        scratch.mkdir(parents=True, exist_ok=True)
        fixture = to_fixture(
            invocation.public,
            managed_agent=invocation.managed_agent,
            catalog=invocation.catalog,
            resolved_system_prompt=invocation.resolved_system_prompt,
        )
        if invocation.mcp:
            fixture["mcp"] = invocation.mcp
        if invocation.provider_base_url_override:
            fixture["providers"][invocation.public.model.type]["baseURL"] = (
                invocation.provider_base_url_override
            )
        content = managed_content(invocation.public, fixture)
        config_path = home / "opencode.config.json"
        env = build_env(
            home=home,
            scratch=scratch,
            approved_env=invocation.engine_env,
            password=password or secrets.token_hex(16),
        )
        fingerprint = _managed_fingerprint(content, env)
        needs_refresh = session._managed_fingerprint != fingerprint
        is_new_process = not session.alive
        # R2: register ownership before any launch await — `cancel()` must be able to
        # find (and force-halt) this invocation even while still inside `ensure_started`
        # or readiness polling, not only once a prompt has actually been sent.
        state = _InvocationState(event_seq_baseline=len(session._event_log))
        session._invocations[invocation.invocation_id] = state
        wid = str(invocation.workspace_dir)

        async def setup_request(method: str, path: str, **kwargs: Any) -> tuple[int, Any]:
            # Session create/select/verify share the startup deadline with launch/readiness.
            try:
                return await asyncio.wait_for(
                    session.request(method, path, **kwargs),
                    timeout=max(0.0, deadline - time.monotonic()),
                )
            except TimeoutError:
                raise NativeLaunchFailed(f"native startup deadline expired at {method} {path}") from None

        provider_id = invocation.public.model.type
        model_id = invocation.public.model.name
        try:
            if session.alive and needs_refresh:
                session._managed_fingerprint = None
                session.config_version = None
                if not await session.stop_cleanly(timeout_seconds=5.0):
                    raise NativeLaunchFailed("managed refresh requires a confirmed clean native stop")
                is_new_process = True
            if not session.alive:
                config_path.write_text(json.dumps(content), encoding="utf-8")
                await session.ensure_started(env=env, config_path=config_path, deadline=deadline)
                is_new_process = True

            if session.sid is None:
                status, body = await setup_request(
                    "POST",
                    "/api/session",
                    json_body={
                        "title": f"ach-{invocation.invocation_id}",
                        "agent": invocation.managed_agent,
                        "model": {"providerID": provider_id, "id": model_id},
                        "location": {"directory": wid},
                    },
                )
                if status != 200 or not isinstance(body, dict) or not isinstance(body.get("data"), dict):
                    raise NativeLaunchFailed(f"session create failed: {status} {body!r}")
                session.sid = str(body["data"].get("id", ""))
                if not session.sid:
                    raise NativeLaunchFailed(f"session create omitted native id: {body!r}")

            required_mcp = sorted(
                ((invocation.catalog or {}).get("mcp") or invocation.mcp or {}).keys()
            )
            if is_new_process:
                await _await_native_readiness(
                    session,
                    wid=wid,
                    managed_agent=invocation.managed_agent,
                    required_mcp=required_mcp,
                    provider_id=provider_id,
                    model_id=model_id,
                    max_steps=invocation.public.limits.max_steps,
                    expect_system=_expects_native_system(fixture),
                    deadline=deadline,
                )

            status, body = await setup_request("GET", f"/api/session/{session.sid}")
            data = body.get("data") if isinstance(body, dict) else None
            if (
                status != 200
                or not isinstance(data, dict)
                or data.get("id") != session.sid
                or not isinstance(data.get("location"), dict)
                or data["location"].get("directory") != wid
            ):
                raise NativeLaunchFailed(
                    f"native session identity/location verification failed: {status} {body!r}"
                )
            model_data = data.get("model")
            current_model = (
                (model_data.get("providerID"), model_data.get("id"))
                if isinstance(model_data, dict)
                else None
            )
            current_agent = data.get("agent")
            desired_model = (provider_id, model_id)
            if current_model != desired_model:
                status, body = await setup_request(
                    "POST",
                    f"/api/session/{session.sid}/model",
                    json_body={"model": {"providerID": provider_id, "id": model_id}},
                )
                if status != 204:
                    raise NativeLaunchFailed(f"native model selection failed: {status} {body!r}")
            if current_agent != invocation.managed_agent:
                status, body = await setup_request(
                    "POST",
                    f"/api/session/{session.sid}/agent",
                    json_body={"agent": invocation.managed_agent},
                )
                if status != 204:
                    raise NativeLaunchFailed(f"native agent selection failed: {status} {body!r}")
            if current_model != desired_model or current_agent != invocation.managed_agent:
                status, body = await setup_request("GET", f"/api/session/{session.sid}")
                data = body.get("data") if isinstance(body, dict) else None
                verified_model = data.get("model") if isinstance(data, dict) else None
                if (
                    status != 200
                    or not isinstance(data, dict)
                    or data.get("id") != session.sid
                    or not isinstance(data.get("location"), dict)
                    or data["location"].get("directory") != wid
                    or not isinstance(verified_model, dict)
                    or (verified_model.get("providerID"), verified_model.get("id")) != desired_model
                    or data.get("agent") != invocation.managed_agent
                ):
                    raise NativeLaunchFailed(f"native selection verification failed: {status} {body!r}")
            session._managed_fingerprint = fingerprint
            session._selected_provider_model = desired_model
            session._selected_agent = invocation.managed_agent
            session.config_version = invocation.public.config_version
        except BaseException:
            session._managed_fingerprint = None
            session.config_version = None
            raise

        try:
            status, body = await session.request(
                "POST",
                f"/api/session/{session.sid}/prompt",
                json_body={"text": invocation.prompt, "delivery": "queue", "resume": True},
            )
        except httpx.HTTPError as exc:
            state.outcome = "unknown"
            raise NativeLaunchFailed(
                f"prompt send receipt unknown: {exc}", ambiguous=True
            ) from exc

        data = body.get("data") if isinstance(body, dict) else None
        receipt_ok = (
            status == 200
            and isinstance(data, dict)
            and data.get("sessionID") == session.sid
            and data.get("delivery") == "queue"
            and data.get("type") == "user"
            and isinstance(data.get("id"), str)
            and data.get("id") != ""
            # The receipt must echo back exactly what we submitted (finding: "Start checks
            # type/SID/delivery/ID but omits submitted payload") — a 200 whose payload text
            # disagrees with what was sent cannot be trusted as our own accepted prompt.
            and isinstance(data.get("payload"), dict)
            and data["payload"].get("text") == invocation.prompt
        )
        if not receipt_ok:
            if status == 200:
                # A 200 we can't fully validate is ambiguous, not a clean rejection: the
                # native side may well have accepted it under a shape we didn't expect.
                state.outcome = "unknown"
                raise NativeLaunchFailed(
                    f"200 response did not decode as the expected receipt: {body!r}",
                    ambiguous=True,
                )
            state.outcome = "failed"
            state.settled.set()
            raise NativeLaunchFailed(f"prompt send rejected: {status} {body!r}")

        assert isinstance(data, dict)
        input_id = str(data["id"])
        state.input_id = input_id
        if state.outcome == "unknown":
            state.outcome = "running"
        session.reconcile_from_baseline(state)
        return EngineReceipt(native_session_id=session.sid, input_id=input_id)

    async def status(self, invocation_id: str) -> EngineOutcome:
        for session in self._sessions.values():
            state = session._invocations.get(invocation_id)
            if state is not None:
                return state.outcome
        return "unknown"

    def last_progress(self, invocation_id: str) -> float | None:
        """Monotonic time of this invocation's last native progress event."""
        for session in self._sessions.values():
            state = session._invocations.get(invocation_id)
            if state is not None:
                return state.last_progress_at
        return None

    def result(self, invocation_id: str) -> str | None:
        """Return retained text only after this invocation's settlement is confirmed."""
        for session in self._sessions.values():
            state = session._invocations.get(invocation_id)
            if state is not None:
                if not state.settlement_confirmed or state.outcome != "completed":
                    return None
                return state.result
        return None

    def usage(self, invocation_id: str) -> RunUsage | None:
        """Return only finite, owned usage after native settlement or a confirmed halt."""
        for session in self._sessions.values():
            state = session._invocations.get(invocation_id)
            if state is None:
                continue
            confirmed = state.settlement_confirmed or (
                session._confirmed_halted and state.outcome == "cancelled"
            )
            if not confirmed or state.outcome not in {"completed", "failed", "cancelled"}:
                return None
            if state.usage is not None:
                return state.usage
            if state.delivered_seq is None:
                return None
            for interval in session._busy:
                if interval["start"] <= state.delivered_seq and (
                    interval["end"] is None or state.delivered_seq <= interval["end"]
                ):
                    return session._usage_record(session._usage_for_interval(state, interval))
        return None

    async def wait_settled(self, invocation_id: str) -> EngineOutcome:
        """Block until this invocation reaches a terminal outcome — the service layer's
        session-exclusivity hold (finding #3) waits on this rather than polling. A
        stream-derived candidate alone is not yet a safe settlement (review focus 3):
        `_confirm_settlement` additionally requires native wait204 and inbox absence."""
        for session in self._sessions.values():
            state = session._invocations.get(invocation_id)
            if state is not None:
                await state.settled.wait()
                if state.observation_lost:
                    # Never a safe settlement: the service layer must run its own
                    # halt-confirming cleanup, not trust an unobserved "unknown".
                    raise NativeLaunchFailed(
                        f"event stream lost before invocation {invocation_id} settled",
                        ambiguous=True,
                    )
                await self._confirm_settlement(session, state, invocation_id)
                state.settlement_confirmed = True
                return state.outcome
        return "unknown"

    async def _confirm_settlement(
        self, session: _NativeSession, state: _InvocationState, invocation_id: str
    ) -> None:
        """A candidate terminal is not a safe settlement until native independently proves
        this exact input is no longer queued (review focus 3: "wait204 alone cannot finish
        queued input" — and symmetrically, the event-stream candidate alone is not proof
        either). A dead leader is not inbox proof unless it was a *confirmed* halt."""
        if not session.alive or session.sid is None:
            if session._confirmed_halted:
                return  # already independently proven by `_kill()`/`stop_cleanly()`
            raise NativeLaunchFailed(
                f"cannot confirm settlement for {invocation_id}: native session not alive",
                ambiguous=True,
            )
        if state.input_id is None:
            raise NativeLaunchFailed(
                f"cannot confirm settlement for {invocation_id}: input id unknown",
                ambiguous=True,
            )
        try:
            wstatus, _wbody = await session.request(
                "POST",
                f"/api/experimental/session/{session.sid}/wait",
                timeout=CANCEL_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as exc:
            raise NativeLaunchFailed(f"settlement wait failed: {exc}", ambiguous=True) from exc
        if wstatus != 204:
            raise NativeLaunchFailed(
                f"settlement wait did not confirm (status {wstatus})", ambiguous=True
            )
        try:
            istatus, ibody = await session.request(
                "GET", f"/api/session/{session.sid}/inbox", timeout=CANCEL_REQUEST_TIMEOUT_SECONDS
            )
        except httpx.HTTPError as exc:
            raise NativeLaunchFailed(f"settlement inbox check failed: {exc}", ambiguous=True) from exc
        if istatus != 200:
            raise NativeLaunchFailed(
                f"settlement inbox check malformed: {istatus} {ibody!r}", ambiguous=True
            )
        ids = _parse_inbox_items(ibody, session_sid=session.sid)
        if ids is None:
            raise NativeLaunchFailed(
                f"settlement inbox check malformed: {istatus} {ibody!r}", ambiguous=True
            )
        if state.input_id in ids:
            raise NativeLaunchFailed(
                f"input {state.input_id} still pending at settlement", ambiguous=True
            )

    async def cancel(
        self, invocation_id: str, reason: str, *, _halt_budget: _HaltBudget | None = None
    ) -> None:
        for session in self._sessions.values():
            state = session._invocations.get(invocation_id)
            if state is None:
                continue
            if state.error is None:
                state.error = reason
            # A caller supplying `_halt_budget` owns `cleanup_started_at`/`force_deadline`
            # (the service's true cleanup-entry timestamp); a direct caller gets the same
            # shape created fresh here (interfaces: "direct callers create the same budget
            # at entry").
            started_at = time.monotonic()
            budget = _halt_budget or _HaltBudget(
                cleanup_started_at=started_at, force_deadline=started_at + CANCEL_KILL_BOUND_SECONDS
            )
            kill_deadline = budget.force_deadline

            async def _graceful() -> bool:
                """Every acknowledgement is REQUIRED (R4): an interrupt failure, a
                non-204 DELETE, or a non-204 wait each stop graceful cancellation right
                there and fall through to the retained whole-session force halt below —
                none of them may be silently skipped and treated as if it succeeded."""
                if state.input_id is None:
                    # Registered (R2) but never got far enough to have an owned inbox
                    # item — nothing to interrupt/delete via HTTP; only a confirmed halt
                    # of the whole session can prove this invocation safe.
                    return session._confirmed_halted
                if session.sid is not None and session.alive:
                    try:
                        istatus, ibody = await session.request(
                            "POST",
                            f"/api/session/{session.sid}/interrupt?resume=false",
                            timeout=CANCEL_REQUEST_TIMEOUT_SECONDS,
                        )
                    except httpx.HTTPError as exc:
                        state.error = f"interrupt request failed: {exc}"
                        return False
                    if istatus != 200 or not (
                        isinstance(ibody, dict) and ibody.get("interrupted") is True
                    ):
                        state.error = f"interrupt not acknowledged: {istatus} {ibody!r}"
                        return False
                    if state.input_id is not None:
                        try:
                            dstatus, dbody = await session.request(
                                "DELETE",
                                f"/api/session/{session.sid}/inbox/{state.input_id}",
                                timeout=CANCEL_REQUEST_TIMEOUT_SECONDS,
                            )
                        except httpx.HTTPError as exc:
                            state.error = f"inbox delete failed: {exc}"
                            return False
                        if dstatus != 204:
                            state.error = f"inbox delete not acknowledged: {dstatus} {dbody!r}"
                            return False
                    try:
                        wstatus, wbody = await session.request(
                            "POST",
                            f"/api/experimental/session/{session.sid}/wait",
                            timeout=CANCEL_REQUEST_TIMEOUT_SECONDS,
                        )
                    except httpx.HTTPError as exc:
                        state.error = f"cancel wait failed: {exc}"
                        return False
                    if wstatus != 204:
                        state.error = f"cancel wait not acknowledged: {wstatus} {wbody!r}"
                        return False
                await state.settled.wait()
                return await self._confirm_no_pending_input(session, state)

            # The force signal is armed independently of this coroutine's own ability to
            # cancel `_graceful` (review focus 4: not a `wait_for`-cancellation of a
            # possibly cancellation-resistant HTTP phase) — it fires on its own schedule
            # even if `_graceful` never cooperates. Bounded observation uses `asyncio.wait`
            # (never a `wait_for` that depends on cancelling a resistant coroutine).
            force_task = session._arm_force(budget=budget)
            graceful_task = asyncio.ensure_future(_graceful())
            graceful_task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            remaining = max(0.0, kill_deadline - time.monotonic())
            done, _pending = await asyncio.wait({graceful_task}, timeout=remaining)
            graceful_finished = graceful_task in done
            settled_cleanly = (
                graceful_finished
                and not graceful_task.cancelled()
                and graceful_task.exception() is None
                and graceful_task.result()
            )
            if not settled_cleanly:
                if graceful_finished and session._force_trigger is not None:
                    # A fast, definitive rejection: force now rather than waiting out the
                    # rest of the independently-armed schedule.
                    session._force_trigger.set()
                confirmed = await force_task
                if not confirmed:
                    raise NativeLaunchFailed(
                        f"owned native process group for {invocation_id} did not confirm exit"
                    )
                state.outcome = "cancelled"
                state.settled.set()
                # Contract §4: a cancelled process must be relaunched/restored before the
                # next event, but terminal records remain queryable — `session.alive` is
                # already False after a confirmed kill, which naturally forces
                # `ensure_started()` to relaunch on the next Start for this session_ref;
                # deleting the session object here previously made `status()` return
                # "unknown" for a cancellation that had just been confirmed.
            else:
                if session._halt_task is None:
                    # I4: the independent force never committed to an actual halt — safe
                    # to disarm the still-pending timer on this HTTP/terminal evidence.
                    if not force_task.done():
                        force_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await force_task
                else:
                    # I4: a halt is already in flight or complete — its retained result is
                    # the truth; never cancel an executing halt worker on HTTP/terminal
                    # evidence alone. A confirmed halt means this generation actually was
                    # force-stopped despite the racing graceful acknowledgement.
                    confirmed = await session._halt_task
                    if confirmed:
                        state.outcome = "cancelled"
                        state.settled.set()
                        return
                if state.outcome not in ("completed", "failed"):
                    state.outcome = "cancelled"
                state.settlement_confirmed = True
            return

    async def _confirm_no_pending_input(
        self, session: _NativeSession, state: _InvocationState
    ) -> bool:
        if state.input_id is None:
            # Ambiguous: we never learned which inbox item was ours, so we cannot prove it
            # carries no pending owned work — escalate to a full owned-session halt
            # (finding: "missing input ID is incorrectly considered safe").
            return False
        if not session.alive or session.sid is None:
            # A dead/never-established session is not itself proof — only a CONFIRMED
            # halt (`_kill()`/`stop_cleanly()` already having proven it) is (R4/R5: "a
            # dead leader is not inbox proof unless it was a confirmed halt").
            return session._confirmed_halted
        try:
            status, body = await session.request(
                "GET",
                f"/api/session/{session.sid}/inbox",
                timeout=CANCEL_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            return False
        if status != 200:
            return False
        ids = _parse_inbox_items(body, session_sid=session.sid)
        if ids is None:
            # Malformed item (review focus 3/R5): never silently treated as a safe
            # non-match — the real endpoint returns the released `{data: [...]}`
            # envelope, not a bare top-level list.
            return False
        return state.input_id not in ids

    async def snapshot(
        self, session_ref: str, *, agent: dict[str, str], workspace_ref: str, registry: dict[str, Any]
    ) -> SnapshotManifest:
        session = self._sessions.get(session_ref)
        if session is None or session.sid is None:
            raise NativeLaunchFailed(f"no native session to snapshot for {session_ref}")
        if session.alive:
            raise NativeLaunchFailed(
                "snapshot requires confirmed clean settlement; call suspend()/stdin-EOF first"
            )
        manifest = SnapshotManifest(
            snapshot_version=SNAPSHOT_VERSION,
            engine_package=ENGINE_PACKAGE,
            engine_version=ENGINE_VERSION,
            agent=agent,
            workspace_ref=workspace_ref,
            session_ref=session_ref,
            native_session_id=session.sid,
            workspace_path=str(registry.get("workspace_path", "")),
            registry=registry,
        )
        # Registry lives alongside HOME, inside it — never in scratch, which contract §4
        # explicitly excludes from the snapshot (it is pinned TMPDIR, outside the Workspace
        # snapshot, and outside this one too).
        registry_path = session.home / ".ach-registry.json"
        registry_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
        if self._transfer is not None:
            manifest.archive_ref = await self._transfer.upload(session.home, session_ref=session_ref)
        return manifest

    async def suspend(self, session_ref: str) -> bool:
        """Confirmed-clean-exit teardown before a snapshot (contract §6 `sessionSuspend`
        barrier) — `stop_cleanly()` owns the actual proof; a missing session stays a no-op
        True, and an already-dead session returns its retained proof only."""
        session = self._sessions.get(session_ref)
        if session is None:
            return True
        return await session.stop_cleanly(timeout_seconds=5.0)

    async def retire_session(self, session_ref: str) -> None:
        """Drop native SID/config/process identity after a durable close receipt (I6).

        The `_NativeSession` entry itself is kept (never popped) — `status`/`result`/
        `usage` key off it by `invocation_id`, and those terminal records must stay
        queryable after retire. Any usage still reconstructed lazily from `_busy` is
        materialized into the invocation state first, since `_busy`/`_event_log` are
        dropped here along with the rest of the live process identity.
        """
        session = self._sessions.get(session_ref)
        if session is None:
            return
        if session.alive or not session._confirmed_halted:
            raise NativeLaunchFailed(
                f"retire requires a confirmed native halt for {session_ref}"
            )
        for state in session._invocations.values():
            if state.usage is None and state.delivered_seq is not None:
                for interval in session._busy:
                    if interval["start"] <= state.delivered_seq and (
                        interval["end"] is None or state.delivered_seq <= interval["end"]
                    ):
                        state.usage = session._usage_record(session._usage_for_interval(state, interval))
                        break
            if not state.settled.is_set():
                # Halt confirmed with no terminal evidence: settle as unknown so this old
                # invocation's delivered_seq can never claim the next SID's busy intervals.
                state.outcome = state.outcome or "unknown"
                state.settled.set()
        session.sid = None
        session.url = None
        session.config_version = None
        session._managed_fingerprint = None
        session._selected_provider_model = None
        session._selected_agent = None
        session._event_log = []
        session._busy = []
        # Native durable seq is per aggregate (SID); the next SID starts its own sequence.
        session._last_durable_seq = 0

    async def restore(
        self,
        session_ref: str,
        manifest: SnapshotManifest,
        *,
        expected: dict[str, str],
        public: PublicConfig,
        engine_env: dict[str, str],
        managed_agent: str = "build",
        home: Path | None = None,
        scratch: Path | None = None,
        restored_home: Path | None = None,
        deadline_at: datetime | None = None,
    ) -> None:
        """Validate, download, relaunch and verify — never adopts archive metadata as
        destination authority (finding #11): every identity field is checked against an
        explicit `expected` destination, the archived payload is required, and restoration
        is not complete until the relaunched process answers `GET /api/session/:sid` for the
        exact restored sid. No synthetic prompt is sent.
        """
        required = {"engine_package", "engine_version", "workspace_ref", "session_ref", "workspace_path"}
        missing_expected = required - expected.keys()
        if missing_expected:
            raise RestoreMismatch(
                f"restore requires an explicit expected destination for {sorted(missing_expected)}"
            )
        if manifest.snapshot_version != SNAPSHOT_VERSION:
            raise RestoreMismatch(f"unsupported snapshotVersion {manifest.snapshot_version!r}")
        if not manifest.native_session_id:
            raise RestoreMismatch("manifest.nativeSessionId must be a nonempty string")
        if not manifest.workspace_path or not Path(manifest.workspace_path).is_absolute():
            raise RestoreMismatch("manifest.workspacePath must be a nonempty absolute path")
        for key, value in expected.items():
            if getattr(manifest, key, None) != value:
                raise RestoreMismatch(f"manifest.{key} does not match expected destination")
        session = self._sessions.get(session_ref)
        if session is None:
            if home is None or scratch is None:
                raise RestoreMismatch(f"no reserved session state to restore into for {session_ref}")
            session = self._session_for(session_ref, home, scratch)
        elif home is not None and session.home != home:
            raise RestoreMismatch("reserved HOME path does not match the SessionRegistry")
        if session.alive:
            # A live process would keep its old HOME open and answer the sid probe itself;
            # the caller must stop/retire it first.
            raise RestoreMismatch(f"restore requires a stopped native process for {session_ref}")
        if restored_home is None and manifest.archive_ref is None:
            raise RestoreMismatch("restore requires an archived payload; no bare metadata adoption")
        if restored_home is not None and not restored_home.is_dir():
            raise RestoreMismatch("staged HOME payload does not exist")
        if restored_home is None and self._transfer is not None:
            await self._transfer.download(manifest.archive_ref, dest=session.home)

        previous_home: Path | None = None
        if restored_home is not None:
            previous_home = session.home.with_name(f".{session.home.name}.previous-{uuid.uuid4().hex}")
            if session.home.exists():
                session.home.rename(previous_home)
            try:
                restored_home.rename(session.home)
            except BaseException:
                if previous_home.exists():
                    previous_home.rename(session.home)
                raise

        fixture = to_fixture(public, managed_agent=managed_agent, lookup_only_prompt=True)
        content = managed_content(public, fixture)
        config_path = session.home / "opencode.config.json"
        config_path.write_text(json.dumps(content), encoding="utf-8")
        env = build_env(
            home=session.home,
            scratch=session.scratch,
            approved_env=engine_env,
            password=secrets.token_hex(16),
        )
        deadline = self._startup_deadline(public.engine.startup_timeout_seconds, deadline_at)
        try:
            await session.ensure_started(env=env, config_path=config_path, deadline=deadline)
            status, body = await asyncio.wait_for(
                session.request("GET", f"/api/session/{manifest.native_session_id}"),
                timeout=max(0.0, deadline - time.monotonic()),
            )
            if status != 200:
                raise RestoreMismatch(
                    f"restored sid {manifest.native_session_id} did not resolve after relaunch: "
                    f"{status} {body!r}"
                )
            session.sid = manifest.native_session_id
            session.config_version = public.config_version
            session._managed_fingerprint = _managed_fingerprint(content, env)
        except BaseException:
            if session.alive:
                await session._kill(confirm_timeout=5.0)
            session.sid = None
            if previous_home is not None:
                shutil.rmtree(session.home, ignore_errors=True)
                if previous_home.exists():
                    previous_home.rename(session.home)
            raise
        else:
            if previous_home is not None:
                shutil.rmtree(previous_home, ignore_errors=True)

# SPDX-License-Identifier: MIT
"""ExecutionService over a fake engine transport — proves registry/dedup/deadline/cancel/
session-exclusivity semantics (task-4-reuse-handoff.md "New focused coverage", plus
task-4-execution-review.md round 1 findings #1/#2/#3/#8/#14) without a native process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ach_runtime.execution.service as service_module
from ach_runtime.config.schema import HookBlock
from ach_runtime.engine.opencode import EngineReceipt, SnapshotManifest
from ach_runtime.config.schema import PromptBlock, SystemAch, SystemFile, SystemText
from ach_runtime.execution.service import (
    CapacityExceeded,
    ConflictingStart,
    ExecutionService,
    _build_fixture_invocation,
)
from ach_runtime.execution.sessions import SessionHookFailed, SessionRegistry
from ach_runtime.execution.wire import StartRequest


class _Ambiguous(RuntimeError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.ambiguous = True


@dataclass
class FakeEngine:
    """Auto-settles to "completed" right after `start()` unless the invocation id is
    listed in `hang` (stays "running" until `cancel()` fires its settlement event) —
    matching the common case so most tests don't need to manage background settle tasks."""

    start_calls: list[str] = field(default_factory=list)
    invocations: list[object] = field(default_factory=list)
    cancel_calls: list[tuple[str, str]] = field(default_factory=list)
    fail_ambiguous: bool = False
    start_delay: float = 0.0
    hang: frozenset[str] = frozenset()
    statuses: dict[str, str] = field(default_factory=dict)
    # Deterministic barriers for the cancellation regressions below — all default to None,
    # preserving the auto-settling behavior every other test in this file relies on.
    start_entered: asyncio.Event | None = None
    start_permit: asyncio.Event | None = None
    cancel_entered: asyncio.Event | None = None
    cancel_permit: asyncio.Event | None = None
    settle_permit: dict[str, asyncio.Event] = field(default_factory=dict)
    # Simulates `engine.cancel()` failing to confirm halt (cleanup uncertainty).
    cancel_raises: Exception | None = None
    close_calls: int = 0
    # Simulates `engine.wait_settled()` observing a lost stream for specific invocations
    # (raises instead of returning a safe outcome).
    settle_raises: dict[str, Exception] = field(default_factory=dict)
    _settled: dict[str, asyncio.Event] = field(default_factory=dict)

    def _event(self, invocation_id: str) -> asyncio.Event:
        event = self._settled.get(invocation_id)
        if event is None:
            event = asyncio.Event()
            self._settled[invocation_id] = event
        return event

    async def start(self, invocation, *, home, scratch, password=None):
        self.invocations.append(invocation)
        if self.start_delay:
            await asyncio.sleep(self.start_delay)
        # Acceptance is recorded the instant native would have taken the request — before
        # the receipt (the HTTP response) is ever withheld by `start_permit` — so a test can
        # assert "native accepted this" independently of "the service has the receipt back".
        self.start_calls.append(invocation.invocation_id)
        if self.fail_ambiguous:
            raise _Ambiguous("transport lost the receipt")
        self.statuses[invocation.invocation_id] = "running"
        if self.start_entered is not None:
            self.start_entered.set()
        if self.start_permit is not None:
            await self.start_permit.wait()
        if invocation.invocation_id not in self.hang:
            self.statuses[invocation.invocation_id] = "completed"
            self._event(invocation.invocation_id).set()
        return EngineReceipt(
            native_session_id=f"sid-{invocation.invocation_id}",
            input_id=f"input-{invocation.invocation_id}",
        )

    async def status(self, invocation_id):
        return self.statuses.get(invocation_id, "running")

    async def wait_settled(self, invocation_id):
        await self._event(invocation_id).wait()
        permit = self.settle_permit.get(invocation_id)
        if permit is not None:
            await permit.wait()
        if invocation_id in self.settle_raises:
            raise self.settle_raises[invocation_id]
        return self.statuses.get(invocation_id, "unknown")

    async def cancel(self, invocation_id, reason, *, _halt_budget=None):
        if self.cancel_entered is not None:
            self.cancel_entered.set()
        if self.cancel_permit is not None:
            await self.cancel_permit.wait()
        self.cancel_calls.append((invocation_id, reason))
        if self.cancel_raises is not None:
            raise self.cancel_raises
        self.statuses[invocation_id] = "cancelled"
        self._event(invocation_id).set()

    async def suspend(self, session_ref):
        return True

    async def snapshot(self, session_ref, *, agent, workspace_ref, registry):
        return SnapshotManifest(
            snapshot_version="session-v1",
            engine_package="@opencode/cli",
            engine_version="2.0.20",
            agent=agent,
            workspace_ref=workspace_ref,
            session_ref=session_ref,
            native_session_id="fake-sid",
            workspace_path=registry.get("workspace_path", ""),
            registry=registry,
        )

    async def restore(
        self, session_ref, manifest, *, expected, public, engine_env, managed_agent="build",
        home=None, scratch=None, restored_home=None, deadline_at: datetime | None = None,
    ):
        return None

    async def close(self):
        self.close_calls += 1


@dataclass
class _Invocation:
    invocation_id: str


@dataclass
class _ObservedLock:
    """A drop-in `asyncio.Lock` substitute, pre-seeded into a `SessionRegistry`, so these
    tests can synchronize on real acquire/release events instead of sleeps: signals a
    numbered `asyncio.Event` immediately before an attempt actually blocks, and counts
    completed acquisitions/releases while keeping normal mutual-exclusion behavior."""

    inner: asyncio.Lock = field(default_factory=asyncio.Lock)
    acquired_count: int = 0
    release_count: int = 0
    blocked_events: list[asyncio.Event] = field(default_factory=list)
    # A stable, pre-existing Event a test can await right after scheduling the blocked
    # caller — unlike `blocked_events[-1]`, it exists before that caller ever runs, so
    # awaiting it (instead of indexing a maybe-still-empty list) can't race the scheduler.
    attempt_started: asyncio.Event = field(default_factory=asyncio.Event)

    def locked(self) -> bool:
        return self.inner.locked()

    async def acquire(self) -> bool:
        if self.inner.locked():
            event = asyncio.Event()
            self.blocked_events.append(event)
            event.set()
            self.attempt_started.set()
        result = await self.inner.acquire()
        self.acquired_count += 1
        return result

    def release(self) -> None:
        self.release_count += 1
        self.inner.release()


def _request(
    config_version: str,
    deadline_seconds: float = 60.0,
    *,
    invocation_id: str = "inv-1",
    session_ref: str = "se" * 16,
    **overrides: object,
) -> StartRequest:
    fields: dict[str, object] = dict(
        workspaceKey="wk", sessionKey="sk",
        invocationId=invocation_id,
        channel="webhook",
        workspaceRef="ws" * 16,
        sessionRef=session_ref,
        configVersion=config_version,
        prompt="do the thing",
        deadlineAt=datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds),
    )
    fields.update(overrides)
    return StartRequest(**fields)


def _service(tmp_path: Path, public_config_fixture, engine: FakeEngine | None = None):
    engine = engine or FakeEngine()
    service = ExecutionService(
        engine=engine,
        sessions=SessionRegistry(tmp_path),
        workspace_dir=tmp_path / "workspace",
        build_invocation=lambda request, public, workspace_dir: _Invocation(request.invocation_id),
    )
    service.configure(public_config_fixture)
    return service, engine


async def _freeze_service_utc(monkeypatch, inv, instant: datetime) -> list[datetime]:
    """Stop the real sleeping watchdog before freezing only service-module UTC reads."""
    if inv.deadline_task is not None:
        inv.deadline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(inv.deadline_task, timeout=5)

    clock = [instant]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            current = clock[0]
            return current.astimezone(tz) if tz is not None else current.replace(tzinfo=None)

    monkeypatch.setattr(service_module, "datetime", FrozenDateTime)
    return clock


async def _observe_task(task: "asyncio.Task[object]", *, deadline: float) -> bool:
    """Step1 Gate helper (bounded observation): waits at most until `deadline` without
    cancelling `task` on expiry — an unresolved task stays retained, never torn down."""
    done, _pending = await asyncio.wait({task}, timeout=max(0.0, deadline - time.monotonic()))
    return task in done


async def test_identical_concurrent_start_sends_one_prompt(tmp_path, public_config_fixture):
    service, engine = _service(tmp_path, public_config_fixture)
    request = _request(public_config_fixture.config_version)
    results = await asyncio.gather(service.start(request), service.start(request))
    assert results[0] == results[1]
    assert engine.start_calls == ["inv-1"]


async def test_retry_with_identical_payload_returns_cached_started(tmp_path, public_config_fixture):
    service, engine = _service(tmp_path, public_config_fixture)
    request = _request(public_config_fixture.config_version)
    first = await service.start(request)
    second = await service.start(request)
    assert first == second
    assert engine.start_calls == ["inv-1"]


async def test_conflicting_same_id_rejected(tmp_path, public_config_fixture):
    service, _ = _service(tmp_path, public_config_fixture)
    await service.start(_request(public_config_fixture.config_version))
    with pytest.raises(ConflictingStart):
        await service.start(
            _request(public_config_fixture.config_version, prompt="a different prompt")
        )


async def test_ambiguous_native_send_never_replays(tmp_path, public_config_fixture):
    engine = FakeEngine(fail_ambiguous=True)
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    with pytest.raises(RuntimeError):
        await service.start(request)
    assert engine.start_calls == ["inv-1"]
    with pytest.raises(RuntimeError):
        await service.start(request)
    assert engine.start_calls == ["inv-1"]
    inv = service._invocations[request.invocation_id]
    if inv.cancel_task is not None:
        await asyncio.wait_for(inv.cancel_task, timeout=5)
    status = await service.status(request.invocation_id)
    assert status.state == "unknown"
    assert status.result["reporting"] == {"duration_ms": None, "usage": None}


def _with_session_start(public, script: str, timeout: int = 5):
    return public.model_copy(
        update={
            "hooks": public.hooks.model_copy(
                update={"session_start": HookBlock(script=script, timeoutSeconds=timeout)}
            )
        }
    )


async def test_hook_failure_sends_zero_prompts(tmp_path, public_config_fixture):
    """A failing sessionStart is a recorded run: Started is published, then `failed`."""
    service, engine = _service(tmp_path, public_config_fixture)
    service.configure(_with_session_start(public_config_fixture, "echo boom >&2; exit 3"))
    started = await service.start(_request(public_config_fixture.config_version))
    assert started.invocation_id == "inv-1"
    assert engine.start_calls == []
    status = await service.status("inv-1")
    assert status.state == "failed"
    assert "sessionStart exited 3" in status.error and "boom" in status.error
    assert status.result["reporting"]["duration_ms"] >= 0
    assert status.result["reporting"]["usage"] is None


def _probe_tools(monkeypatch, counts: list[int]) -> list[str]:
    """Each `tools/list` probe returns the next count; returns the probed URLs."""
    probed: list[str] = []

    async def count(url, headers):
        probed.append(url)
        return counts[min(len(probed), len(counts)) - 1]

    monkeypatch.setattr(service_module, "_count_mcp_tools", count)
    monkeypatch.setattr(service_module, "MCP_TOOLS_BACKOFF_SECONDS", (0.0, 0.0))
    return probed


_MCP_CATALOG = {"mcp": {"srv": {"url": "http://facade/mcp/srv", "headers": {}}}}


async def test_mcp_server_listing_no_tools_is_retried_before_the_send(
    tmp_path, public_config_fixture, monkeypatch
):
    probed = _probe_tools(monkeypatch, [0, 0, 3])
    service, engine = _service(tmp_path, public_config_fixture)
    service.configure(public_config_fixture, catalog=_MCP_CATALOG)
    await service.start(_request(public_config_fixture.config_version))
    assert probed == ["http://facade/mcp/srv"] * 3
    assert engine.start_calls == ["inv-1"]


async def test_mcp_server_still_listing_no_tools_fails_the_run_without_the_engine(
    tmp_path, public_config_fixture, monkeypatch
):
    probed = _probe_tools(monkeypatch, [0])
    service, engine = _service(tmp_path, public_config_fixture)
    service.configure(public_config_fixture, catalog=_MCP_CATALOG)
    await service.start(_request(public_config_fixture.config_version))
    assert len(probed) == 3
    assert engine.start_calls == []
    status = await service.status("inv-1")
    assert status.state == "failed"
    assert status.error == "mcp tools unavailable: srv (0 tools)"


async def test_session_start_timeout_is_a_recorded_failed_run(tmp_path, public_config_fixture):
    service, engine = _service(tmp_path, public_config_fixture)
    service.configure(_with_session_start(public_config_fixture, "sleep 30", timeout=1))
    await service.start(_request(public_config_fixture.config_version))
    status = await service.status("inv-1")
    assert status.state == "failed"
    assert "sessionStart" in status.error and "timed out" in status.error
    assert engine.start_calls == []


async def test_session_start_exit_78_skips_the_engine(tmp_path, public_config_fixture):
    service, engine = _service(tmp_path, public_config_fixture)
    service.configure(_with_session_start(public_config_fixture, "exit 78"))
    started = await service.start(_request(public_config_fixture.config_version))
    assert started.run_id
    status = await service.status("inv-1")
    assert status.state == "skipped"
    assert status.error is None
    assert status.result["reporting"]["duration_ms"] >= 0
    assert engine.start_calls == []
    # The session lock is released and the session is not marked started: the gate runs
    # again for the next invocation of the same session.
    await service.start(_request(public_config_fixture.config_version, invocation_id="inv-2"))
    assert (await service.status("inv-2")).state == "skipped"
    assert not service._sessions.get_or_create("se" * 16).started


async def test_session_start_stdout_is_appended_to_the_prompt(tmp_path, public_config_fixture):
    prompts: list[str] = []

    def build(request, public, workspace_dir):
        prompts.append(request.prompt)
        return _Invocation(request.invocation_id)

    engine = FakeEngine()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path),
        workspace_dir=tmp_path / "workspace", build_invocation=build,
    )
    service.configure(_with_session_start(public_config_fixture, "echo 'NEW: Pod/a/b'"))
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    assert prompts == ["do the thing\n\nNEW: Pod/a/b\n"]
    assert engine.start_calls == ["inv-1"]
    # The admitted request is untouched: a retry with the same payload is still idempotent.
    assert await service.start(request) is not None
    assert engine.start_calls == ["inv-1"]


async def test_session_start_empty_stdout_keeps_the_prompt(tmp_path, public_config_fixture):
    prompts: list[str] = []

    def build(request, public, workspace_dir):
        prompts.append(request.prompt)
        return _Invocation(request.invocation_id)

    service = ExecutionService(
        engine=FakeEngine(), sessions=SessionRegistry(tmp_path),
        workspace_dir=tmp_path / "workspace", build_invocation=build,
    )
    service.configure(_with_session_start(public_config_fixture, "true"))
    await service.start(_request(public_config_fixture.config_version))
    assert prompts == ["do the thing"]


async def test_session_start_env_and_cwd(tmp_path, public_config_fixture):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service, _ = _service(tmp_path, public_config_fixture)
    script = 'env > "$ACH_WORKSPACE/env.txt"; pwd > "$ACH_WORKSPACE/cwd.txt"'
    service.configure(
        _with_session_start(public_config_fixture, script),
        catalog={"mcp": {"mcp-k8sgpt-ro": {"url": "http://facade/mcp/x", "headers": {}}}},
    )
    await service.start(_request(public_config_fixture.config_version))
    env = dict(
        line.split("=", 1) for line in (workspace / "env.txt").read_text().splitlines() if "=" in line
    )
    assert env["ACH_WORKSPACE"] == str(workspace)
    assert env["ACH_CHANNEL"] == "webhook"
    assert env["ACH_INVOCATION_ID"] == "inv-1"
    assert env["ACH_SESSION_REF"] == "se" * 16
    assert env["ACH_WORKSPACE_KEY"] == "wk"
    assert env["ACH_SESSION_KEY"] == "sk"
    assert json.loads(env["ACH_MCP_URLS"]) == {"mcp-k8sgpt-ro": "http://facade/mcp/x"}
    session_home = service._sessions.get_or_create("se" * 16).home
    assert (workspace / "cwd.txt").read_text().strip() == str(session_home)


async def test_deadline_already_expired_before_preparation_never_sends(
    tmp_path, public_config_fixture
) -> None:
    """Finding #1: an already-expired request must not reach native send at all."""
    service, engine = _service(tmp_path, public_config_fixture)
    request = _request(public_config_fixture.config_version, deadline_seconds=-5.0)
    with pytest.raises(TimeoutError):
        await service.start(request)
    assert engine.start_calls == []
    status = await service.status(request.invocation_id)
    assert status.state == "failed"
    assert status.result["reporting"]["duration_ms"] >= 0
    assert status.result["reporting"]["usage"] is None


async def test_deadline_expiring_during_send_fails_not_cancels(
    tmp_path, public_config_fixture
) -> None:
    """Finding #1: a deadline that fires mid-send maps to failed/timeout, not cancelled —
    and the watchdog that enforces it must not self-cancel (the original bug)."""
    engine = FakeEngine(start_delay=0.3)
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version, deadline_seconds=0.05)
    with pytest.raises(TimeoutError):
        await service.start(request)
    inv = service._invocations[request.invocation_id]
    if inv.cancel_task is not None:
        await asyncio.wait_for(inv.cancel_task, timeout=5)
    status = await service.status(request.invocation_id)
    assert status.state == "failed"
    assert engine.start_calls == []  # never completed the delayed send


async def test_deadline_after_started_maps_to_failed_and_triggers_engine_cancel(
    tmp_path, public_config_fixture
) -> None:
    """Finding #1: once Started, a deadline firing is a timeout -> failed, and the
    watchdog's own `_timeout_cancel` must complete (it must not cancel itself)."""
    engine = FakeEngine(hang=frozenset({"inv-1"}))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version, deadline_seconds=0.05)
    await service.start(request)
    await asyncio.sleep(0.3)
    status = await service.status(request.invocation_id)
    assert status.state == "failed"
    assert status.error == "deadline expired"
    assert engine.cancel_calls == [(request.invocation_id, "deadline expired")]


@dataclass
class _ProgressEngine(FakeEngine):
    """A hanging engine whose last native progress is whatever the test sets."""

    progress_at: float = field(default_factory=time.monotonic)

    def last_progress(self, invocation_id):
        return self.progress_at


def _with_stall_seconds(public, seconds: int):
    return public.model_copy(
        update={"limits": public.limits.model_copy(update={"max_stall_seconds": seconds})}
    )


async def test_running_invocation_without_engine_progress_fails_as_stalled(
    tmp_path, public_config_fixture
) -> None:
    engine = _ProgressEngine(hang=frozenset({"inv-1"}))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    service.configure(_with_stall_seconds(public_config_fixture, 1))
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    await asyncio.sleep(1.5)
    status = await service.status(request.invocation_id)
    assert status.state == "failed"
    assert status.error == "stalled: no engine progress for 1s"
    assert engine.cancel_calls == [(request.invocation_id, "stalled: no engine progress for 1s")]


async def test_running_invocation_with_engine_progress_is_not_stalled(
    tmp_path, public_config_fixture
) -> None:
    engine = _ProgressEngine(hang=frozenset({"inv-1"}))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    service.configure(_with_stall_seconds(public_config_fixture, 1))
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    for _ in range(6):
        await asyncio.sleep(0.25)
        engine.progress_at = time.monotonic()
    assert (await service.status(request.invocation_id)).state == "running"
    assert engine.cancel_calls == []


async def test_queued_cancel_preserves_running_reservation_and_never_sends(
    tmp_path, public_config_fixture
) -> None:
    """Cancelling a queued second invocation must never release the first invocation's
    reservation, and must never let the second owner resume and send once dequeued."""
    session_ref = "shared-session"
    engine = FakeEngine(hang=frozenset({"first"}))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks[session_ref] = lock

    first = _request(
        public_config_fixture.config_version, invocation_id="first", session_ref=session_ref
    )
    await service.start(first)
    assert engine.start_calls == ["first"]

    second = _request(
        public_config_fixture.config_version, invocation_id="second", session_ref=session_ref
    )
    second_caller = asyncio.ensure_future(service.start(second))
    await asyncio.wait_for(lock.attempt_started.wait(), timeout=5)
    assert lock.blocked_events

    await asyncio.wait_for(service.cancel("second", "caller gave up"), timeout=5)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(second_caller, timeout=5)

    assert engine.start_calls == ["first"]
    assert (await service.status("first")).state == "running"
    assert (await service.status("second")).state == "cancelled"
    assert lock.locked()
    assert lock.release_count == 0
    assert (await service.status("second")).deadline_at == second.deadline_at

    # Retry with the identical payload: dedup returns the already-cancelled shared Future
    # directly, never re-entering the lock at all.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(service.start(second), timeout=5)
    assert engine.start_calls == ["first"]
    runs = {run.invocation_id: run for run in await service.list_runs()}
    assert runs["first"].channel == first.channel
    assert runs["first"].deadline_at == first.deadline_at
    assert runs["second"].channel == second.channel
    assert runs["second"].deadline_at == second.deadline_at

    first_inv = service._invocations["first"]
    engine.statuses["first"] = "completed"
    engine._event("first").set()
    await asyncio.wait_for(first_inv.settle_task, timeout=5)

    third = _request(
        public_config_fixture.config_version, invocation_id="third", session_ref=session_ref
    )
    engine.hang = frozenset({"third"})
    await service.start(third)
    assert engine.start_calls == ["first", "third"]
    assert lock.acquired_count == 2
    assert lock.release_count == 1
    assert lock.locked()

    second_inv = service._invocations["second"]
    await asyncio.wait_for(service.cancel("second", "caller gave up again"), timeout=5)
    service._release_session_lock(second_inv)
    assert lock.acquired_count == 2
    assert lock.release_count == 1
    assert lock.locked()
    assert (await service.status("third")).state == "running"

    await asyncio.wait_for(service.cancel("third", "test cleanup"), timeout=5)
    third_inv = service._invocations["third"]
    await asyncio.wait_for(third_inv.settle_task, timeout=5)


@pytest.mark.parametrize("barrier", ["hook", "send"])
async def test_cancel_stops_preparing_owner_before_prompt_submission(
    tmp_path, public_config_fixture, barrier, monkeypatch
) -> None:
    """Cancelling while the owner is still preparing (hook) or has not yet recorded its
    send (engine.start) must stop it before any prompt submission, must not release the
    reservation until cancel's own cleanup finishes, and a successor must not be able to
    acquire/send while that cleanup is still pending."""
    entered = asyncio.Event()
    permit = asyncio.Event()
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(cancel_entered=cancel_entered, cancel_permit=cancel_permit)
    if barrier == "send":
        engine.start_entered = entered
        engine.start_permit = permit
    service, engine = _service(tmp_path, public_config_fixture, engine)

    if barrier == "hook":
        public = public_config_fixture.model_copy(
            update={
                "hooks": public_config_fixture.hooks.model_copy(
                    update={"session_start": HookBlock(script="true", timeoutSeconds=5)}
                )
            }
        )
        service.configure(public)

        async def _blocking_hook(hook, *, cwd, env, timeout_seconds=None):
            entered.set()
            try:
                await permit.wait()
            except asyncio.CancelledError:
                # Mirrors the existing `sessions.run_hook` behavior (read-only, not
                # edited here): a cancellation mid-hook is converted into
                # `SessionHookFailed`, never re-raised as `CancelledError` (I2).
                raise SessionHookFailed("session hook cancelled") from None

        monkeypatch.setattr("ach_runtime.execution.service.run_gate_hook", _blocking_hook)

    session_ref = "preparing-owner"
    lock = _ObservedLock()
    service._sessions._locks[session_ref] = lock
    # Non-expiring deadline: this test is only about stopping the owner before it submits,
    # not about reclaiming an already-accepted native prompt via a race with the deadline.
    request = _request(
        public_config_fixture.config_version, session_ref=session_ref, deadline_seconds=60.0
    )
    caller = asyncio.ensure_future(service.start(request))
    await asyncio.wait_for(entered.wait(), timeout=5)

    cancel_caller = asyncio.ensure_future(
        service.cancel(request.invocation_id, "stop preparing")
    )
    try:
        await asyncio.wait_for(cancel_entered.wait(), timeout=5)

        # The preparing owner still owns its reservation while cancel's own engine cleanup
        # is blocked (I1): nothing has released it yet.
        assert lock.locked()
        assert lock.release_count == 0

        successor = _request(
            public_config_fixture.config_version,
            session_ref=session_ref,
            invocation_id="successor",
        )
        successor_caller = asyncio.ensure_future(service.start(successor))
        await asyncio.wait_for(lock.attempt_started.wait(), timeout=5)
        assert "successor" not in engine.start_calls  # successor is blocked, cannot send yet

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, timeout=5)
        permit.set()

        cancel_permit.set()
        await asyncio.wait_for(cancel_caller, timeout=5)

        status = await service.status(request.invocation_id)
        assert status.state == "cancelled"
        assert status.error == "stop preparing"  # I2: hook's SessionHookFailed never wins

        started = await asyncio.wait_for(successor_caller, timeout=5)
        assert started.invocation_id == "successor"
        # For the "send" barrier, the cancelled owner's own send may have already been
        # accepted by native before cancellation landed (it is recorded the instant native
        # would take it, not only once the full round trip completes); for "hook", the
        # owner never reaches native at all. Either way, the successor never sends until
        # the cancelled owner's reservation is actually released.
        expected_calls = [request.invocation_id, "successor"] if barrier == "send" else ["successor"]
        assert engine.start_calls == expected_calls
        successor_inv = service._invocations["successor"]
        assert successor_inv.settle_task is not None
        await asyncio.wait_for(successor_inv.settle_task, timeout=5)
        assert lock.acquired_count == 2  # preparing owner, then the successor
        assert lock.release_count == 2  # both reservations released; successor never hangs
    finally:
        permit.set()
        cancel_permit.set()
        for pending in (cancel_caller, caller):
            if not pending.done():
                pending.cancel()
        successor_inv = service._invocations.get("successor")
        if successor_inv is not None and successor_inv.settle_task is not None:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(successor_inv.settle_task, timeout=5)


async def test_late_settlement_and_repeated_release_preserve_successor_reservation(
    tmp_path, public_config_fixture
) -> None:
    """A stale settlement callback (and a repeated cancel) arriving after a successor has
    already acquired the session reservation must never release that successor's lock."""
    session_ref = "shared-session-late-settle"
    settle_permit = asyncio.Event()
    engine = FakeEngine(hang=frozenset({"first", "third"}))
    engine.settle_permit["first"] = settle_permit
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks[session_ref] = lock

    first = _request(
        public_config_fixture.config_version, invocation_id="first", session_ref=session_ref
    )
    await service.start(first)
    first_inv = service._invocations["first"]

    await service.cancel("first", "operator cancel")
    assert lock.release_count == 1
    assert not lock.locked()

    third = _request(
        public_config_fixture.config_version, invocation_id="third", session_ref=session_ref
    )
    await service.start(third)
    assert engine.start_calls == ["first", "third"]
    assert lock.locked()

    # Open the late settlement gate and repeat cancel/release for `first` now that `third`
    # holds the lock — none of this may touch third's reservation.
    settle_permit.set()
    await asyncio.wait_for(first_inv.settle_task, timeout=5)
    service._release_session_lock(first_inv)
    service._release_session_lock(first_inv)
    await service.cancel("first", "operator cancel again")

    assert lock.release_count == 1
    assert lock.locked()
    assert (await service.status("third")).state == "running"
    assert len(engine.cancel_calls) == 1

    await service.cancel("third", "test cleanup")
    third_inv = service._invocations["third"]
    await asyncio.wait_for(third_inv.settle_task, timeout=5)


@pytest.mark.parametrize("cancelled_waiter", ["first", "retry"])
async def test_cancel_after_caller_disconnect_still_completes(
    tmp_path, public_config_fixture, cancelled_waiter
) -> None:
    """Finding #2: a caller's own cancellation (e.g. HTTP disconnect) must not cancel the
    shared owner task — the surviving waiter (whichever one it is) still gets the real
    receipt, and an identical retry after settlement returns that same receipt."""
    entered = asyncio.Event()
    permit = asyncio.Event()
    engine = FakeEngine(start_entered=entered, start_permit=permit)
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)

    first_waiter = asyncio.ensure_future(service.start(request))
    await asyncio.wait_for(entered.wait(), timeout=5)

    # A bare `ensure_future` here would let `retry.cancel()` fire before the scheduler
    # ever steps the new task — it would never execute far enough to enter the shared
    # `started` wait, so cancelling it would prove nothing (I3). Setting an Event doesn't
    # yield control, so `_retry_start` keeps running synchronously straight into
    # `service.start`'s dedup branch and suspends on `asyncio.shield(existing.started)`
    # before this coroutine (awaiting the Event) ever resumes.
    retry_entered = asyncio.Event()

    async def _retry_start():
        retry_entered.set()
        return await service.start(request)

    retry_waiter = asyncio.ensure_future(_retry_start())
    await asyncio.wait_for(retry_entered.wait(), timeout=5)

    cancelled = first_waiter if cancelled_waiter == "first" else retry_waiter
    survivor = retry_waiter if cancelled_waiter == "first" else first_waiter

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    inv = service._invocations[request.invocation_id]
    assert inv.owner_task is not None and not inv.owner_task.done()
    assert not inv.started.done()

    permit.set()
    started = await asyncio.wait_for(survivor, timeout=5)
    assert started.invocation_id == request.invocation_id

    retry = await asyncio.wait_for(service.start(request), timeout=5)
    assert retry == started
    assert engine.start_calls == ["inv-1"]
    status = await service.status(request.invocation_id)
    assert status.deadline_at == request.deadline_at

    await asyncio.wait_for(inv.owner_task, timeout=5)
    if inv.settle_task is not None:
        await asyncio.wait_for(inv.settle_task, timeout=5)
    if inv.deadline_task is not None:
        inv.deadline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(inv.deadline_task, timeout=5)


@pytest.mark.parametrize("first_trigger", ["public", "watchdog"])
@pytest.mark.parametrize("expired_by", [timedelta(0), timedelta(microseconds=1)])
async def test_expired_public_cancel_matches_watchdog_outcome(
    tmp_path, public_config_fixture, monkeypatch, first_trigger, expired_by
) -> None:
    entered = asyncio.Event()
    permit = asyncio.Event()
    engine = FakeEngine(
        hang=frozenset({"inv-1"}), cancel_entered=entered, cancel_permit=permit
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    lock = _ObservedLock()
    service._sessions._locks[request.session_ref] = lock
    started = await service.start(request)
    inv = service._invocations[request.invocation_id]
    await _freeze_service_utc(monkeypatch, inv, request.deadline_at + expired_by)

    first_caller = None
    second_caller = None
    cleanup_task = None
    try:
        first_coro = (
            service.cancel(request.invocation_id, "invocation deadline exceeded")
            if first_trigger == "public"
            else service._timeout_cancel(inv)
        )
        first_caller = asyncio.create_task(first_coro)
        await asyncio.wait_for(entered.wait(), timeout=5)
        cleanup_task = inv.cancel_task

        await service._timeout_cancel(inv)
        second_entered = asyncio.Event()

        async def _second_public_cancel():
            second_entered.set()
            return await service.cancel(request.invocation_id, "operator request")

        second_caller = asyncio.create_task(_second_public_cancel())
        await asyncio.wait_for(second_entered.wait(), timeout=5)
        assert inv.cancel_task is cleanup_task
        assert cleanup_task is not None and not cleanup_task.done()
        assert not second_caller.done()
        assert lock.locked() and lock.release_count == 0
        assert inv.request.deadline_at == request.deadline_at

        permit.set()
        await asyncio.wait_for(first_caller, timeout=5)
        await asyncio.wait_for(second_caller, timeout=5)
        await asyncio.wait_for(cleanup_task, timeout=5)
        await asyncio.wait_for(inv.settle_task, timeout=5)

        status = await service.status(request.invocation_id)
        assert status.state == "failed"
        assert status.error == "deadline expired"
        assert status.deadline_at == request.deadline_at
        assert engine.cancel_calls == [(request.invocation_id, "deadline expired")]
        assert inv.cancel_task is cleanup_task
        assert lock.release_count == 1 and not lock.locked()
        assert await service.start(request) == started
        assert engine.start_calls == [request.invocation_id]
    finally:
        permit.set()
        pending = [task for task in (first_caller, second_caller, cleanup_task, inv.settle_task) if task]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)


async def test_public_cancel_just_before_deadline_keeps_first_claim_after_expiry(
    tmp_path, public_config_fixture, monkeypatch
) -> None:
    entered = asyncio.Event()
    permit = asyncio.Event()
    engine = FakeEngine(
        hang=frozenset({"inv-1"}), cancel_entered=entered, cancel_permit=permit
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    lock = _ObservedLock()
    service._sessions._locks[request.session_ref] = lock
    await service.start(request)
    inv = service._invocations[request.invocation_id]
    clock = await _freeze_service_utc(
        monkeypatch, inv, request.deadline_at - timedelta(microseconds=1)
    )
    first_caller = None
    second_caller = None
    cleanup_task = None
    try:
        first_caller = asyncio.create_task(service.cancel(request.invocation_id, "operator request"))
        await asyncio.wait_for(entered.wait(), timeout=5)
        cleanup_task = inv.cancel_task
        clock[0] = request.deadline_at + timedelta(microseconds=1)

        second_entered = asyncio.Event()

        async def _second_public_cancel():
            second_entered.set()
            return await service.cancel(request.invocation_id, "second request")

        second_caller = asyncio.create_task(_second_public_cancel())
        await asyncio.wait_for(second_entered.wait(), timeout=5)
        assert inv.cancel_task is cleanup_task
        assert cleanup_task is not None and not cleanup_task.done()
        assert not second_caller.done()
        assert lock.locked() and lock.release_count == 0

        await service._timeout_cancel(inv)
        assert inv.cancel_task is cleanup_task
        permit.set()
        await asyncio.wait_for(first_caller, timeout=5)
        await asyncio.wait_for(second_caller, timeout=5)
        await asyncio.wait_for(cleanup_task, timeout=5)
        await asyncio.wait_for(inv.settle_task, timeout=5)

        status = await service.status(request.invocation_id)
        assert status.state == "cancelled"
        assert status.error == "operator request"
        assert status.deadline_at == request.deadline_at
        assert engine.cancel_calls == [(request.invocation_id, "operator request")]
        assert inv.cancel_task is cleanup_task
        assert lock.release_count == 1
    finally:
        permit.set()
        pending = [task for task in (first_caller, second_caller, cleanup_task, inv.settle_task) if task]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)


async def test_two_expired_public_cancel_callers_both_join_cleanup(
    tmp_path, public_config_fixture, monkeypatch
) -> None:
    entered = asyncio.Event()
    permit = asyncio.Event()
    engine = FakeEngine(
        hang=frozenset({"inv-1"}), cancel_entered=entered, cancel_permit=permit
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    lock = _ObservedLock()
    service._sessions._locks[request.session_ref] = lock
    started = await service.start(request)
    inv = service._invocations[request.invocation_id]
    await _freeze_service_utc(monkeypatch, inv, request.deadline_at)

    first_caller = None
    second_caller = None
    cleanup_task = None
    try:
        # Both public calls enter the ready queue before either shielded timeout coroutine
        # can run and claim cleanup.
        first_caller = asyncio.create_task(
            service.cancel(request.invocation_id, "operator request one")
        )
        second_caller = asyncio.create_task(
            service.cancel(request.invocation_id, "operator request two")
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        cleanup_task = inv.cancel_task

        assert cleanup_task is not None and not cleanup_task.done()
        assert not first_caller.done()
        assert not second_caller.done()
        assert lock.locked() and lock.release_count == 0
        assert inv.request.deadline_at == request.deadline_at

        permit.set()
        await asyncio.wait_for(first_caller, timeout=5)
        await asyncio.wait_for(second_caller, timeout=5)
        await asyncio.wait_for(cleanup_task, timeout=5)
        await asyncio.wait_for(inv.settle_task, timeout=5)

        status = await service.status(request.invocation_id)
        assert status.state == "failed"
        assert status.error == "deadline expired"
        assert status.deadline_at == request.deadline_at
        assert engine.cancel_calls == [(request.invocation_id, "deadline expired")]
        assert inv.cancel_task is cleanup_task
        assert lock.release_count == 1 and not lock.locked()
        assert await service.start(request) == started
        assert engine.start_calls == [request.invocation_id]
    finally:
        permit.set()
        pending = [task for task in (first_caller, second_caller, cleanup_task, inv.settle_task) if task]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)


async def test_public_cancel_after_expiry_preserves_completed_terminal(
    tmp_path, public_config_fixture, monkeypatch
) -> None:
    service, engine = _service(tmp_path, public_config_fixture)
    request = _request(public_config_fixture.config_version)
    started = await service.start(request)
    inv = service._invocations[request.invocation_id]
    await asyncio.wait_for(inv.settle_task, timeout=5)
    before = await service.status(request.invocation_id)
    await _freeze_service_utc(monkeypatch, inv, request.deadline_at)

    await service.cancel(request.invocation_id, "invocation deadline exceeded")

    assert await service.status(request.invocation_id) == before
    assert inv.cancel_task is None
    assert engine.cancel_calls == []
    assert await service.start(request) == started


@pytest.mark.parametrize("expired", [False, True])
async def test_cancel_waiter_disconnect_preserves_retained_cleanup(
    tmp_path, public_config_fixture, monkeypatch, expired
) -> None:
    """A caller disconnecting from `cancel()` must not interrupt the shared cleanup it
    started — a second waiter observes the same retained cancel task, and that cleanup
    runs exactly once."""
    entered = asyncio.Event()
    permit = asyncio.Event()
    engine = FakeEngine(hang=frozenset({"inv-1"}), cancel_entered=entered, cancel_permit=permit)
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    lock = _ObservedLock()
    service._sessions._locks[request.session_ref] = lock
    await service.start(request)
    assert lock.locked()

    inv = service._invocations[request.invocation_id]
    if expired:
        await _freeze_service_utc(monkeypatch, inv, request.deadline_at)

    first_cancel_waiter = None
    second_cancel_waiter = None
    cancel_task = None
    try:
        first_cancel_waiter = asyncio.ensure_future(
            service.cancel(request.invocation_id, "operator request")
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        first_cancel_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_cancel_waiter

        cancel_task = inv.cancel_task
        assert cancel_task is not None and not cancel_task.done()

        # Same Event-doesn't-yield reasoning as the retry barrier above (I3): by the time this
        # coroutine resumes from `second_cancel_entered.wait()`, `_second_cancel` has already
        # run synchronously into `service.cancel`'s retained-task branch and suspended on
        # `asyncio.shield(cancel_task)` — proving the second caller actually joins the pending
        # cleanup rather than taking the terminal shortcut (I4).
        second_cancel_entered = asyncio.Event()

        async def _second_cancel():
            second_cancel_entered.set()
            return await service.cancel(request.invocation_id, "operator request again")

        second_cancel_waiter = asyncio.ensure_future(_second_cancel())
        await asyncio.wait_for(second_cancel_entered.wait(), timeout=5)
        assert not second_cancel_waiter.done()
        assert not cancel_task.done()

        permit.set()
        await asyncio.wait_for(second_cancel_waiter, timeout=5)
        await asyncio.wait_for(cancel_task, timeout=5)

        assert inv.cancel_task is cancel_task
        assert len(engine.cancel_calls) == 1
        status = await service.status(request.invocation_id)
        assert status.state == ("failed" if expired else "cancelled")
        assert status.error == ("deadline expired" if expired else "operator request")
        assert engine.cancel_calls == [
            (request.invocation_id, "deadline expired" if expired else "operator request")
        ]
        assert lock.release_count == 1

        await asyncio.wait_for(inv.settle_task, timeout=5)
        assert len(engine.cancel_calls) == 1
        assert lock.release_count == 1
    finally:
        permit.set()
        pending = [
            task
            for task in (first_cancel_waiter, second_cancel_waiter, cancel_task, inv.settle_task)
            if task
        ]
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)


async def test_session_exclusivity_blocks_concurrent_invocation_on_same_session(
    tmp_path, public_config_fixture
) -> None:
    """Finding #3: a second invocation for the same session cannot be admitted while the
    first is still running (held until settlement, not merely until admission)."""
    engine = FakeEngine(hang=frozenset({"inv-1"}))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    session_ref = "shared-session"
    first = _request(public_config_fixture.config_version, invocation_id="inv-1", session_ref=session_ref)
    await service.start(first)

    second = _request(
        public_config_fixture.config_version,
        invocation_id="inv-2",
        session_ref=session_ref,
        deadline_seconds=0.1,
    )
    with pytest.raises(TimeoutError):
        await service.start(second)
    assert engine.start_calls == ["inv-1"]  # inv-2 never reached native send

    await service.cancel("inv-1", "test cleanup")


async def test_session_admits_next_invocation_after_settlement(
    tmp_path, public_config_fixture
) -> None:
    """Finding #3, positive case: once the first invocation settles, the next one on the
    same session is admitted normally."""
    engine = FakeEngine()  # default: settles immediately
    service, engine = _service(tmp_path, public_config_fixture, engine)
    session_ref = "shared-session"
    first = _request(public_config_fixture.config_version, invocation_id="inv-1", session_ref=session_ref)
    await service.start(first)
    await asyncio.sleep(0)  # let the settle-and-release task run

    second = _request(public_config_fixture.config_version, invocation_id="inv-2", session_ref=session_ref)
    await service.start(second)
    assert engine.start_calls == ["inv-1", "inv-2"]


async def test_config_drift_during_preparation_does_not_leak_into_running_request(
    tmp_path, public_config_fixture
) -> None:
    """Finding #8: a request recorded under configVersion A must run with the PublicConfig
    captured at admission, even if `configure()` is called again before send completes."""
    engine = FakeEngine(start_delay=0.1)
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    start_future = asyncio.ensure_future(service.start(request))
    await asyncio.sleep(0.01)

    drifted = public_config_fixture.model_copy(
        update={"configVersion": "b" * 64, "limits": public_config_fixture.limits.model_copy(
            update={"max_steps": public_config_fixture.limits.max_steps + 1}
        )}
    )
    service.configure(drifted)

    await start_future
    status = await service.status(request.invocation_id)
    assert status.config_version == public_config_fixture.config_version


async def test_capacity_exceeded_before_retention_window_elapses(
    tmp_path, public_config_fixture
) -> None:
    """Finding #14: terminal records must not be evicted before the configured
    idempotency window elapses — a full registry rejects new admission instead."""
    engine = FakeEngine()
    service, engine = _service(tmp_path, public_config_fixture, engine)
    service._invocations.clear()
    from ach_runtime.execution.service import MAX_RETAINED_RUNS, _Invocation

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    future.set_result(object())
    for i in range(MAX_RETAINED_RUNS):
        service._invocations[f"old-{i}"] = _Invocation(
            request=_request(public_config_fixture.config_version, invocation_id=f"old-{i}"),
            public=public_config_fixture,
            run_id="r",
            started=future,
            terminal=True,
            state="completed",
            terminal_at=__import__("time").monotonic(),
        )
    with pytest.raises(CapacityExceeded):
        await service.start(_request(public_config_fixture.config_version, invocation_id="new-1"))


async def test_list_runs_includes_channel_and_deadline(tmp_path, public_config_fixture):
    service, _ = _service(tmp_path, public_config_fixture)
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    runs = await service.list_runs()
    assert len(runs) == 1
    assert runs[0].channel == "webhook"
    assert runs[0].deadline_at == request.deadline_at


async def test_cancel_is_terminal_and_idempotent(tmp_path, public_config_fixture):
    engine = FakeEngine(hang=frozenset({"inv-1"}))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    await service.cancel(request.invocation_id, "operator request")
    await service.cancel(request.invocation_id, "operator request again")
    assert len(engine.cancel_calls) == 1
    status = await service.status(request.invocation_id)
    assert status.state == "cancelled"


async def test_snapshot_requires_terminal_session(tmp_path, public_config_fixture) -> None:
    engine = FakeEngine(hang=frozenset({"inv-1"}))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    with pytest.raises(RuntimeError):
        await service.snapshot(request.session_ref)
    await service.cancel(request.invocation_id, "cleanup")


async def test_snapshot_after_settlement_includes_registry(tmp_path, public_config_fixture) -> None:
    engine = FakeEngine()
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    await asyncio.sleep(0)
    manifest = await service.snapshot(request.session_ref)
    assert manifest.registry["runs"][0]["invocationId"] == "inv-1"
    assert manifest.workspace_ref == request.workspace_ref


async def test_real_snapshot_run_roundtrip(tmp_path, public_config_fixture) -> None:
    engine = FakeEngine()
    service, _ = _service(tmp_path / "first", public_config_fixture, engine)
    request = _request(public_config_fixture.config_version, prompt="a nonempty archived prompt")
    started = await service.start(request)
    await asyncio.sleep(0)
    manifest = await service.snapshot(request.session_ref)

    record = manifest.registry["runs"][0]
    assert record["request"] == request.model_dump(by_alias=True, mode="json")
    assert record["started"] == started.model_dump(by_alias=True, mode="json")
    assert record["runId"] == started.run_id
    assert record["state"] == "completed"

    restored, restored_engine = _service(tmp_path / "restored", public_config_fixture)
    await restored.restore(request.session_ref, request.workspace_ref, manifest)
    assert await restored.status(request.invocation_id) == await service.status(request.invocation_id)
    assert await restored.start(request) == started
    assert restored_engine.start_calls == []


async def test_negative_snapshot_run_roundtrip_never_fabricates_started(
    tmp_path, public_config_fixture
) -> None:
    # A pre-Started failure (the invocation cannot be built). A failing sessionStart no
    # longer is one: it publishes Started and ends `failed`, so control records it.
    def _broken_build(request, public, workspace_dir):
        raise ValueError("cannot build invocation")

    service = ExecutionService(
        engine=FakeEngine(), sessions=SessionRegistry(tmp_path / "first"),
        workspace_dir=tmp_path / "first" / "workspace", build_invocation=_broken_build,
    )
    service.configure(public_config_fixture)
    request = _request(public_config_fixture.config_version, prompt="never sent")
    with pytest.raises(ValueError):
        await service.start(request)
    original_status = await service.status(request.invocation_id)
    manifest = await service.snapshot(request.session_ref)
    saved = manifest.registry["runs"][0]
    assert saved["state"] == "failed"
    assert saved["started"] is None

    restored, restored_engine = _service(tmp_path / "restored", public_config_fixture)
    await restored.restore(request.session_ref, request.workspace_ref, manifest)
    assert await restored.status(request.invocation_id) == original_status
    with pytest.raises(RuntimeError):
        await restored.start(request)
    assert restored_engine.start_calls == []


async def test_timeout_after_acceptance_keeps_reservation_until_halt(
    tmp_path, public_config_fixture
) -> None:
    """Finding: a deadline firing mid-send (receipt possibly already accepted by native)
    must not release the reservation before `engine.cancel()` confirms halt. The old code
    released this exact timeout immediately via `_fail_before_started`, with no native
    contact at all — a successor must see the reservation held, and never send, until that
    confirmation completes; exactly one cleanup call, original deadline, no replay."""
    start_entered = asyncio.Event()
    start_permit = asyncio.Event()  # deliberately never set: the deadline, not a release,
    # is what ends the send — the receipt is withheld until the owner's own `wait_for`
    # cancels this await out from under it.
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(
        start_entered=start_entered,
        start_permit=start_permit,
        cancel_entered=cancel_entered,
        cancel_permit=cancel_permit,
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    session_ref = "timeout-session"
    lock = _ObservedLock()
    service._sessions._locks[session_ref] = lock
    request = _request(
        public_config_fixture.config_version, session_ref=session_ref, deadline_seconds=0.05
    )
    start_caller = asyncio.ensure_future(service.start(request))

    await asyncio.wait_for(start_entered.wait(), timeout=5)
    assert engine.start_calls == [request.invocation_id]  # acceptance recorded before receipt

    await asyncio.wait_for(cancel_entered.wait(), timeout=5)
    assert lock.locked()
    assert lock.release_count == 0

    successor = _request(
        public_config_fixture.config_version,
        session_ref=session_ref,
        invocation_id="successor",
    )
    successor_caller = asyncio.ensure_future(service.start(successor))
    await asyncio.wait_for(lock.attempt_started.wait(), timeout=5)
    assert engine.start_calls == [request.invocation_id]  # successor blocked, never sent

    cancel_permit.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(start_caller, timeout=5)
    assert engine.start_calls.count(request.invocation_id) == 1  # no replay of the timed-out send
    inv = service._invocations[request.invocation_id]
    if inv.cancel_task is not None:
        await asyncio.wait_for(inv.cancel_task, timeout=5)
    status = await service.status(request.invocation_id)
    assert status.state == "failed"
    assert len(engine.cancel_calls) == 1  # one cleanup/release, no replay

    # `start_permit` is shared on this `FakeEngine` instance (it only needs to withhold the
    # ORIGINAL send's receipt until the deadline cuts it off, already confirmed above) —
    # release it now so the successor's own send isn't blocked by the same gate.
    start_permit.set()
    started = await asyncio.wait_for(successor_caller, timeout=5)
    assert started.invocation_id == "successor"
    successor_inv = service._invocations["successor"]
    await asyncio.wait_for(successor_inv.settle_task, timeout=5)
    if successor_inv.deadline_task is not None:
        successor_inv.deadline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await successor_inv.deadline_task


async def test_terminal_during_cleanup_keeps_reservation(
    tmp_path, public_config_fixture
) -> None:
    """Review focus 1: a real settlement arriving while a retained cleanup (triggered by
    explicit cancel here) is still blocked must not independently release the reservation —
    only the retained cleanup's own proof of halt may release it, and exactly once."""
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(
        hang=frozenset({"inv-1"}), cancel_entered=cancel_entered, cancel_permit=cancel_permit
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks["se" * 16] = lock
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    inv = service._invocations[request.invocation_id]
    assert lock.locked()

    cancel_caller = asyncio.ensure_future(service.cancel(request.invocation_id, "operator"))
    await asyncio.wait_for(cancel_entered.wait(), timeout=5)

    # The native side actually finishes right now, while cleanup is still blocked —
    # `_hold_until_settled`'s own settlement must see cleanup already owns finalization.
    engine.statuses[request.invocation_id] = "completed"
    engine._event(request.invocation_id).set()
    await asyncio.sleep(0)
    assert lock.locked()
    assert lock.release_count == 0

    # Queue a successor for the same session while cleanup is still blocked: it must stay
    # blocked on the reservation through both the candidate terminal above and the engine
    # halt below, never only one of the two.
    successor = _request(public_config_fixture.config_version, invocation_id="successor")
    engine.hang = frozenset({"successor"})  # must not auto-release and race our own check
    successor_caller = asyncio.ensure_future(service.start(successor))
    await asyncio.wait_for(lock.attempt_started.wait(), timeout=5)
    assert "successor" not in engine.start_calls
    assert lock.release_count == 0

    cancel_permit.set()
    await asyncio.wait_for(cancel_caller, timeout=5)
    await asyncio.wait_for(inv.settle_task, timeout=5)

    status = await service.status(request.invocation_id)
    assert status.state == "cancelled"  # the retained cleanup's state wins, not "completed"
    assert lock.release_count == 1
    assert lock.locked()  # the queued successor already re-acquired it

    # A stale repeated release/cancel on the already-finished invocation must never touch
    # the successor's freshly-acquired reservation.
    service._release_session_lock(inv)
    await service.cancel(request.invocation_id, "repeat after release")
    assert lock.release_count == 1

    started = await asyncio.wait_for(successor_caller, timeout=5)
    assert started.invocation_id == "successor"
    successor_inv = service._invocations["successor"]
    assert successor_inv.settle_task is not None
    engine.statuses["successor"] = "completed"
    engine._event("successor").set()
    await asyncio.wait_for(successor_inv.settle_task, timeout=5)


async def test_all_cleanup_triggers_join_retained_owner(
    tmp_path, public_config_fixture
) -> None:
    """Review focus 2: overlapping triggers (settlement loss, the original deadline, and
    two public cancels) must all join the same retained cleanup task — exactly one
    `engine.cancel()` call, and cancelling a queued waiter must not disturb the owner."""
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(
        hang=frozenset({"inv-1"}),
        settle_raises={"inv-1": RuntimeError("event stream lost")},
        cancel_entered=cancel_entered,
        cancel_permit=cancel_permit,
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks["se" * 16] = lock
    request = _request(public_config_fixture.config_version, deadline_seconds=60.0)
    await service.start(request)
    inv = service._invocations[request.invocation_id]

    engine._event(request.invocation_id).set()  # triggers the settlement-loss cleanup
    await asyncio.wait_for(cancel_entered.wait(), timeout=5)
    cleanup_task = inv.cancel_task
    assert cleanup_task is not None

    # A deadline firing concurrently (simulated directly) and two explicit cancels must all
    # observe the same retained task rather than racing a second cleanup.
    if inv.deadline_task is not None:
        inv.deadline_task.cancel()
    await service._timeout_cancel(inv)
    assert inv.cancel_task is cleanup_task

    first_cancel = asyncio.ensure_future(service.cancel(request.invocation_id, "operator a"))
    second_cancel = asyncio.ensure_future(service.cancel(request.invocation_id, "operator b"))
    await asyncio.sleep(0)
    assert inv.cancel_task is cleanup_task

    # A disconnecting waiter must not disturb the retained cleanup it was only observing
    # (review focus 2: cancelling a queued waiter must not disturb the owner).
    first_cancel.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await first_cancel
    assert not cleanup_task.cancelled()
    assert inv.cancel_task is cleanup_task

    cancel_permit.set()
    await asyncio.wait_for(second_cancel, timeout=5)
    await asyncio.wait_for(cleanup_task, timeout=5)

    assert len(engine.cancel_calls) == 1
    status = await service.status(request.invocation_id)
    assert status.state == "unknown"  # the first (settlement-loss) trigger's state wins
    assert lock.release_count == 1


async def test_close_joins_retained_cleanup_after_invocation_is_terminal(
    tmp_path, public_config_fixture
) -> None:
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(
        hang=frozenset({"inv-1"}),
        cancel_entered=cancel_entered,
        cancel_permit=cancel_permit,
    )
    service, _ = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    await service.start(request)

    cancel_task = asyncio.create_task(service.cancel(request.invocation_id, "operator"))
    await asyncio.wait_for(cancel_entered.wait(), timeout=5)
    inv = service._invocations[request.invocation_id]
    assert inv.terminal
    assert inv.cancel_task is not None and not inv.cancel_task.done()

    close_task = asyncio.create_task(service.close())
    done, _ = await asyncio.wait({close_task}, timeout=0.05)
    assert not done, "close returned while retained cleanup was still pending"
    assert engine.close_calls == 0

    cancel_permit.set()
    await asyncio.wait_for(cancel_task, timeout=5)
    await asyncio.wait_for(close_task, timeout=5)
    assert engine.close_calls == 1


async def test_close_reports_retained_unknown_cleanup(
    tmp_path, public_config_fixture
) -> None:
    engine = FakeEngine(hang=frozenset({"inv-1"}), cancel_raises=RuntimeError("halt unknown"))
    service, _ = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    await service.cancel(request.invocation_id, "operator")
    assert (await service.status(request.invocation_id)).state == "unknown"

    inv = service._invocations[request.invocation_id]
    with pytest.raises(RuntimeError, match="uncertain"):
        await service.close()
    assert engine.close_calls == 0
    assert inv.reservation is not None and inv.reservation.locked()
    engine.statuses[request.invocation_id] = "completed"
    engine._event(request.invocation_id).set()
    assert inv.settle_task is not None
    await asyncio.wait_for(inv.settle_task, timeout=5)


async def test_admitted_invocation_keeps_captured_public_and_catalog(
    tmp_path, public_config_fixture
) -> None:
    entered = asyncio.Event()
    permit = asyncio.Event()
    engine = FakeEngine(start_entered=entered, start_permit=permit)
    service = ExecutionService(
        engine=engine,
        sessions=SessionRegistry(tmp_path),
        workspace_dir=tmp_path / "workspace",
    )
    public_a = public_config_fixture
    catalog_a = {"providers": {"captured": {"value": "A"}}, "mcp": {"tool": {"value": "A"}}}
    service.configure(public_a, catalog=catalog_a)
    request_a = _request(public_a.config_version, invocation_id="captured-a")
    start_a = asyncio.create_task(service.start(request_a))
    await asyncio.wait_for(entered.wait(), timeout=5)

    catalog_a["providers"]["captured"]["value"] = "mutated"
    public_b = public_a.model_copy(update={"config_version": "b" * 64}, deep=True)
    catalog_b = {"providers": {"captured": {"value": "B"}}, "mcp": {"tool": {"value": "B"}}}
    service.configure(public_b, catalog=catalog_b)
    permit.set()
    await asyncio.wait_for(start_a, timeout=5)

    request_b = _request(public_b.config_version, invocation_id="captured-b")
    await asyncio.wait_for(service.start(request_b), timeout=5)
    captured_a, captured_b = engine.invocations
    assert captured_a.public.config_version == public_a.config_version
    assert captured_a.catalog["providers"]["captured"]["value"] == "A"
    assert captured_a.catalog["mcp"]["tool"]["value"] == "A"
    assert captured_b.public.config_version == "b" * 64
    assert captured_b.catalog["providers"]["captured"]["value"] == "B"
    assert captured_b.catalog["mcp"]["tool"]["value"] == "B"


async def test_close_with_no_created_sessions_closes_engine_once(
    tmp_path, public_config_fixture
) -> None:
    service, engine = _service(tmp_path, public_config_fixture)
    await service.close()
    assert engine.start_calls == []
    assert engine.cancel_calls == []
    assert engine.close_calls == 1


async def test_close_after_two_completed_session_handles_does_not_replay_cancels(
    tmp_path, public_config_fixture
) -> None:
    service, engine = _service(tmp_path, public_config_fixture)
    first = _request(public_config_fixture.config_version, invocation_id="first-session")
    second = _request(
        public_config_fixture.config_version,
        invocation_id="second-session",
        session_ref="s2" * 16,
    )
    await service.start(first)
    await service.start(second)

    assert engine.start_calls == ["first-session", "second-session"]
    assert engine.cancel_calls == []
    await service.close()
    assert engine.cancel_calls == []
    assert engine.close_calls == 1


async def test_deadline_before_started_survives_owner_cancellation_stall(
    tmp_path, public_config_fixture
) -> None:
    """Review focus 2: the original deadline watchdog claims cleanup even when a separate,
    already-accepted send-cancellation stalls — the original deadline is unchanged, and
    cleanup reaches the engine before the Start-owner join resolves."""
    start_entered = asyncio.Event()
    start_permit = asyncio.Event()  # never set: the deadline alone ends the send
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(
        start_entered=start_entered,
        start_permit=start_permit,
        cancel_entered=cancel_entered,
        cancel_permit=cancel_permit,
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version, deadline_seconds=0.05)
    original_deadline = request.deadline_at
    start_caller = asyncio.ensure_future(service.start(request))
    await asyncio.wait_for(start_entered.wait(), timeout=5)

    # Cleanup (the possible-send path) must reach the engine even though nothing ever
    # releases the owner's own in-flight receipt wait on its own.
    await asyncio.wait_for(cancel_entered.wait(), timeout=5)
    cancel_permit.set()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(start_caller, timeout=5)

    inv = service._invocations[request.invocation_id]
    if inv.cancel_task is not None:
        await asyncio.wait_for(inv.cancel_task, timeout=5)
    status = await service.status(request.invocation_id)
    assert status.state == "failed"
    assert status.deadline_at == original_deadline  # never renewed
    assert len(engine.cancel_calls) == 1


@pytest.mark.parametrize("barrier", ["hook", "send"])
async def test_original_watchdog_claims_cleanup_despite_cancellation_resistant_owner(
    tmp_path, public_config_fixture, barrier, monkeypatch
) -> None:
    """R1: the original-deadline watchdog is created at admission, independent of the
    owner — a hook/send barrier that catches its own cancellation and keeps running past
    it must not stop the deadline from claiming cleanup: `Started` settles with the
    deadline's TimeoutError and the reservation stays held with zero successor sends
    while that owner remains stuck, before cleanup itself completes."""
    entered = asyncio.Event()
    resistant_release = asyncio.Event()
    cancel_entered = asyncio.Event()
    engine = FakeEngine(cancel_entered=cancel_entered)

    if barrier == "send":

        async def _resistant_start(invocation, *, home, scratch, password=None):
            engine.start_calls.append(invocation.invocation_id)
            if invocation.invocation_id != "inv-1":
                engine.statuses[invocation.invocation_id] = "completed"
                engine._event(invocation.invocation_id).set()
                return EngineReceipt(
                    native_session_id=f"sid-{invocation.invocation_id}",
                    input_id=f"input-{invocation.invocation_id}",
                )
            entered.set()
            with contextlib.suppress(asyncio.CancelledError):
                await resistant_release.wait()
            await resistant_release.wait()
            return EngineReceipt(native_session_id="sid", input_id="input")

        engine.start = _resistant_start  # type: ignore[method-assign]

    service, engine = _service(tmp_path, public_config_fixture, engine)
    session_ref = "watchdog-session"
    lock = _ObservedLock()
    service._sessions._locks[session_ref] = lock

    if barrier == "hook":
        public = public_config_fixture.model_copy(
            update={
                "hooks": public_config_fixture.hooks.model_copy(
                    update={"session_start": HookBlock(script="true", timeoutSeconds=5)}
                )
            }
        )
        service.configure(public)

        resisted_once = False

        async def _resistant_hook(hook, *, cwd, env, timeout_seconds=None):
            nonlocal resisted_once
            if resisted_once:
                return
            resisted_once = True
            entered.set()
            with contextlib.suppress(asyncio.CancelledError):
                await resistant_release.wait()
            await resistant_release.wait()

        monkeypatch.setattr("ach_runtime.execution.service.run_gate_hook", _resistant_hook)

    request = _request(
        public_config_fixture.config_version, session_ref=session_ref, deadline_seconds=0.05
    )
    original_deadline = request.deadline_at
    start_caller = asyncio.ensure_future(service.start(request))
    await asyncio.wait_for(entered.wait(), timeout=5)

    inv = service._invocations[request.invocation_id]
    assert inv.deadline_task is not None  # watchdog exists at admission, before Started

    await asyncio.wait_for(cancel_entered.wait(), timeout=5)  # engine cleanup entered

    successor = _request(
        public_config_fixture.config_version, session_ref=session_ref, invocation_id="successor"
    )
    successor_caller = asyncio.ensure_future(service.start(successor))
    await asyncio.wait_for(lock.attempt_started.wait(), timeout=5)
    assert "successor" not in engine.start_calls
    assert lock.release_count == 0

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(start_caller, timeout=5)
    assert (await service.status(request.invocation_id)).deadline_at == original_deadline

    resistant_release.set()

    inv_cleanup = inv.cancel_task
    assert inv_cleanup is not None
    await asyncio.wait_for(inv_cleanup, timeout=5)
    status = await service.status(request.invocation_id)
    assert status.state == "failed"
    assert len(engine.cancel_calls) == 1

    started = await asyncio.wait_for(successor_caller, timeout=5)
    assert started.invocation_id == "successor"
    successor_inv = service._invocations["successor"]
    await asyncio.wait_for(successor_inv.settle_task, timeout=5)
    if successor_inv.deadline_task is not None:
        successor_inv.deadline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await successor_inv.deadline_task


async def test_release_waits_for_owner_join_not_just_engine_cancel(
    tmp_path, public_config_fixture, monkeypatch
) -> None:
    """R2: `engine.cancel()` returning quickly must not permit release/successor
    admission before the owner-stop join also completes — both must finish before
    `_confirm_halt_and_release` finalizes."""
    entered = asyncio.Event()
    release_owner = asyncio.Event()
    cancel_entered = asyncio.Event()
    engine = FakeEngine(cancel_entered=cancel_entered)  # cancel() returns immediately
    service, engine = _service(tmp_path, public_config_fixture, engine)
    session_ref = "owner-join-session"
    lock = _ObservedLock()
    service._sessions._locks[session_ref] = lock
    # The successor's own native send must not auto-settle — otherwise, once the first
    # invocation's lock is released, the successor could race through its entire
    # lifecycle (and release the shared lock a second time) before this test's own
    # assertions run.
    engine.hang = frozenset({"successor"})

    public = public_config_fixture.model_copy(
        update={
            "hooks": public_config_fixture.hooks.model_copy(
                update={"session_start": HookBlock(script="true", timeoutSeconds=5)}
            )
        }
    )
    service.configure(public)

    resisted_once = False

    async def _resistant_hook(hook, *, cwd, env, timeout_seconds=None):
        nonlocal resisted_once
        if resisted_once:
            return
        resisted_once = True
        entered.set()
        with contextlib.suppress(asyncio.CancelledError):
            await release_owner.wait()
        await release_owner.wait()

    monkeypatch.setattr("ach_runtime.execution.service.run_gate_hook", _resistant_hook)

    request = _request(
        public_config_fixture.config_version, session_ref=session_ref, deadline_seconds=60.0
    )
    caller = asyncio.ensure_future(service.start(request))
    await asyncio.wait_for(entered.wait(), timeout=5)

    cancel_caller = asyncio.ensure_future(service.cancel(request.invocation_id, "operator"))
    await asyncio.wait_for(cancel_entered.wait(), timeout=5)

    successor = _request(
        public_config_fixture.config_version, session_ref=session_ref, invocation_id="successor"
    )
    successor_caller = asyncio.ensure_future(service.start(successor))
    await asyncio.wait_for(lock.attempt_started.wait(), timeout=5)

    # engine.cancel() has already returned — it never blocked on anything here — yet the
    # reservation must still be held: the owner itself has not actually stopped yet.
    assert len(engine.cancel_calls) == 1
    assert lock.locked()
    assert lock.release_count == 0
    assert "successor" not in engine.start_calls

    release_owner.set()
    await asyncio.wait_for(cancel_caller, timeout=5)
    with contextlib.suppress(asyncio.CancelledError):
        await caller

    status = await service.status(request.invocation_id)
    assert status.state == "cancelled"
    assert lock.release_count == 1

    started = await asyncio.wait_for(successor_caller, timeout=5)
    assert started.invocation_id == "successor"
    successor_inv = service._invocations["successor"]
    engine.statuses["successor"] = "completed"
    engine._event("successor").set()
    await asyncio.wait_for(successor_inv.settle_task, timeout=5)
    if successor_inv.deadline_task is not None:
        successor_inv.deadline_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await successor_inv.deadline_task


async def test_unfinished_cleanup_and_reservation_are_never_evicted(
    tmp_path, public_config_fixture
) -> None:
    """R8: a terminal-but-unproved invocation (retained reservation, or cleanup still in
    flight) must never be evicted by capacity pressure, even once its idempotency window
    has elapsed — only a fully finalized entry may age out."""
    engine = FakeEngine()
    service, engine = _service(tmp_path, public_config_fixture, engine)
    service._invocations.clear()
    from ach_runtime.execution.service import MAX_RETAINED_RUNS, _Invocation

    future: asyncio.Future = asyncio.get_event_loop().create_future()
    future.set_result(object())
    old_age = __import__("time").monotonic() - 10_000.0

    stuck_lock = asyncio.Lock()
    await stuck_lock.acquire()
    unproved_id = "unproved-reservation"
    service._invocations[unproved_id] = _Invocation(
        request=_request(public_config_fixture.config_version, invocation_id=unproved_id),
        public=public_config_fixture,
        run_id="r",
        started=future,
        terminal=True,
        state="unknown",
        terminal_at=old_age,
        reservation=stuck_lock,
    )

    never_finishing: asyncio.Future = asyncio.get_event_loop().create_future()
    pending_cleanup_id = "pending-cleanup"
    service._invocations[pending_cleanup_id] = _Invocation(
        request=_request(public_config_fixture.config_version, invocation_id=pending_cleanup_id),
        public=public_config_fixture,
        run_id="r",
        started=future,
        terminal=True,
        state="unknown",
        terminal_at=old_age,
        cancel_task=asyncio.ensure_future(never_finishing),
    )

    for i in range(MAX_RETAINED_RUNS - 2):
        service._invocations[f"old-{i}"] = _Invocation(
            request=_request(public_config_fixture.config_version, invocation_id=f"old-{i}"),
            public=public_config_fixture,
            run_id="r",
            started=future,
            terminal=True,
            state="completed",
            terminal_at=old_age,
        )

    await service.start(_request(public_config_fixture.config_version, invocation_id="new-1"))

    assert unproved_id in service._invocations
    assert pending_cleanup_id in service._invocations
    assert len(service._invocations) == MAX_RETAINED_RUNS
    remaining_old = [k for k in service._invocations if k.startswith("old-")]
    assert len(remaining_old) == MAX_RETAINED_RUNS - 3

    never_finishing.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await service._invocations[pending_cleanup_id].cancel_task


async def test_builder_failure_settles_started_and_releases_once(
    tmp_path, public_config_fixture
) -> None:
    """A `build_invocation` callable that raises must settle the pending `Started` as a
    failure, send nothing to native, and release the reservation exactly once — a definitive
    pre-send failure, same as a hook rejection."""
    engine = FakeEngine()

    def _raising_builder(request, public, workspace_dir):
        raise RuntimeError("cannot build invocation")

    service = ExecutionService(
        engine=engine,
        sessions=SessionRegistry(tmp_path),
        workspace_dir=tmp_path / "workspace",
        build_invocation=_raising_builder,
    )
    service.configure(public_config_fixture)
    lock = _ObservedLock()
    service._sessions._locks["se" * 16] = lock

    request = _request(public_config_fixture.config_version)
    with pytest.raises(RuntimeError):
        await asyncio.wait_for(service.start(request), timeout=5)

    assert engine.start_calls == []
    assert lock.release_count == 1
    status = await service.status(request.invocation_id)
    assert status.state == "failed"


def test_builder_resolves_system_file_and_ach(tmp_path, public_config_fixture) -> None:
    workspace = tmp_path / "workspace"
    state = workspace / ".ach-state"
    prompts = state / "prompts"
    prompts.mkdir(parents=True)
    (state / "system.md").write_text("persona — café ☕", encoding="utf-8")
    (state / "empty.md").write_text("", encoding="utf-8")
    (prompts / "solo").mkdir()
    (prompts / "solo" / "nested.md").write_text("sole prompt", encoding="utf-8")
    (prompts / "named").mkdir()
    (prompts / "named" / "first.md").write_text("first", encoding="utf-8")
    (prompts / "named" / "second.md").write_text("second", encoding="utf-8")

    request = _request(public_config_fixture.config_version)

    def build(system):
        public = public_config_fixture.model_copy(
            update={"prompt": None if system is ... else PromptBlock(system=system)}
        )
        return _build_fixture_invocation(request, public, workspace)

    file_invocation = build(SystemFile(type="file", file="system.md"))
    assert file_invocation.prompt == "do the thing"
    assert file_invocation.resolved_system_prompt == "persona — café ☕"
    assert build(SystemFile(type="file", file="empty.md")).resolved_system_prompt == ""
    assert build(SystemAch(type="ach", ach="solo")).resolved_system_prompt == "sole prompt"
    assert build(SystemAch(type="ach", ach="named", file="second.md")).resolved_system_prompt == "second"
    assert build(SystemText(type="text", text="inline")).resolved_system_prompt is None
    assert build(...).resolved_system_prompt is None

    with pytest.raises(ValueError):
        build(SystemFile(type="file", file="missing.md"))
    with pytest.raises(ValueError):
        build(SystemAch(type="ach", ach="named"))


async def test_exact_receipt_retained_before_started(
    tmp_path, public_config_fixture
) -> None:
    """The exact receipt native returned (its SID/input ID) is retained on the invocation
    before `Started` is ever published — not merely published and discarded."""
    engine = FakeEngine()
    service, engine = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    await service.start(request)

    inv = service._invocations[request.invocation_id]
    assert inv.receipt is not None
    assert inv.receipt.native_session_id == f"sid-{request.invocation_id}"
    assert inv.receipt.input_id == f"input-{request.invocation_id}"


async def test_cleanup_failure_keeps_unknown_reserved(tmp_path, public_config_fixture) -> None:
    """Finding: "Retain unknown/alarm and reservation if halt cannot be proved" — when
    `engine.cancel()` itself fails to confirm halt, the reservation must stay held and the
    terminal state must be "unknown", never a silently-released "cancelled"/"failed"."""
    engine = FakeEngine(hang=frozenset({"inv-1"}), cancel_raises=RuntimeError("kill failed"))
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks["se" * 16] = lock
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    assert lock.locked()

    await service.cancel(request.invocation_id, "operator request")

    status = await service.status(request.invocation_id)
    assert status.state == "unknown"
    assert "cleanup uncertain" in status.error
    assert lock.locked()
    assert lock.release_count == 0


async def test_stream_loss_joins_single_cleanup(tmp_path, public_config_fixture) -> None:
    """Finding: observation loss must raise for service-owned cleanup rather than silently
    settling as a safe "unknown" — `_hold_until_settled` then runs the SAME shared
    halt-confirming cleanup as timeout/cancel (one retained cleanup call, one release)."""
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(
        hang=frozenset({"inv-1"}),
        settle_raises={"inv-1": RuntimeError("event stream lost")},
        cancel_entered=cancel_entered,
        cancel_permit=cancel_permit,
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks["se" * 16] = lock
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    inv = service._invocations[request.invocation_id]
    assert lock.locked()

    engine._event(request.invocation_id).set()  # the (lost) stream's last signal
    await asyncio.wait_for(cancel_entered.wait(), timeout=5)
    assert lock.locked()
    assert lock.release_count == 0

    cancel_permit.set()
    await asyncio.wait_for(inv.settle_task, timeout=5)

    status = await service.status(request.invocation_id)
    assert status.state == "unknown"
    assert len(engine.cancel_calls) == 1
    assert lock.release_count == 1


async def test_ambiguous_send_settles_started_before_stalled_cleanup(
    tmp_path, public_config_fixture
) -> None:
    """Step1: an ambiguous send must settle `started` at the moment cleanup is claimed —
    the owner must exit without awaiting cleanup's own completion, so a caller observes
    the failure immediately even while cleanup itself stays blocked."""
    cancel_entered = asyncio.Event()
    cancel_permit = asyncio.Event()
    engine = FakeEngine(
        fail_ambiguous=True, cancel_entered=cancel_entered, cancel_permit=cancel_permit
    )
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks["se" * 16] = lock
    request = _request(public_config_fixture.config_version)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(service.start(request), timeout=5)

    inv = service._invocations[request.invocation_id]
    assert inv.started.done()
    assert engine.start_calls == [request.invocation_id]
    assert inv.cancel_task is not None and not inv.cancel_task.done()
    assert lock.release_count == 0

    await asyncio.wait_for(cancel_entered.wait(), timeout=5)
    assert lock.release_count == 0  # cleanup is blocked, not yet released
    cancel_permit.set()
    await asyncio.wait_for(inv.cancel_task, timeout=5)
    assert lock.release_count == 1


async def test_cleanup_releases_once_when_owner_stops_cleanly(
    tmp_path, public_config_fixture
) -> None:
    """Step1 success control for the owner barrier below: a cooperative owner that stops
    before the shared deadline lets cleanup finalize with exactly one release."""
    from ach_runtime.execution.service import _Invocation

    engine = FakeEngine()
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    session_ref = "se" * 16
    service._sessions._locks[session_ref] = lock
    await lock.acquire()
    request = _request(public_config_fixture.config_version, deadline_seconds=60.0)

    async def _owner():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()

    owner_task = asyncio.ensure_future(_owner())
    started_future: asyncio.Future = asyncio.get_running_loop().create_future()
    inv = _Invocation(
        request=request,
        public=public_config_fixture,
        run_id="r",
        started=started_future,
        owner_task=owner_task,
        reservation=lock,
    )
    service._invocations[request.invocation_id] = inv

    cleanup_task = service._ensure_cleanup(
        inv, "test cleanup", "cancelled", join=lambda: service._stop_owner(inv)
    )
    await asyncio.wait_for(cleanup_task, timeout=5)

    assert inv.state == "cancelled"
    assert lock.release_count == 1
    assert not lock.locked()


@pytest.mark.parametrize("owner_mode", ["already_failed", "fails_on_cancel", "stalled"])
async def test_cleanup_checks_failed_or_stalled_owner(
    tmp_path, public_config_fixture, owner_mode, monkeypatch
) -> None:
    """Step1: engine halt returning cleanly is not enough on its own — a failed, a
    cancellation-converting, or a stalled owner must still surface as retained `unknown`
    with the reservation held and the same invocation/task identities intact, and a
    successor/retry must never slip past the still-pending barrier."""
    from ach_runtime.execution.service import _Invocation

    monkeypatch.setattr("ach_runtime.execution.service.CANCEL_KILL_BOUND_SECONDS", 0.3)

    engine = FakeEngine()
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    session_ref = "se" * 16
    service._sessions._locks[session_ref] = lock
    await lock.acquire()
    request = _request(public_config_fixture.config_version, deadline_seconds=60.0)

    release = asyncio.Event()
    if owner_mode == "already_failed":

        async def _owner():
            raise RuntimeError("owner crashed before cleanup ever joined it")

        owner_task = asyncio.ensure_future(_owner())
        await asyncio.sleep(0)
        assert owner_task.done()
    elif owner_mode == "fails_on_cancel":

        async def _owner():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise RuntimeError("owner's own cancel handling failed") from None

        owner_task = asyncio.ensure_future(_owner())
    else:  # stalled: resists the first cancel, released explicitly below

        async def _owner():
            with contextlib.suppress(asyncio.CancelledError):
                await release.wait()
            await release.wait()

        owner_task = asyncio.ensure_future(_owner())

    started_future: asyncio.Future = asyncio.get_running_loop().create_future()
    inv = _Invocation(
        request=request,
        public=public_config_fixture,
        run_id="r",
        started=started_future,
        owner_task=owner_task,
        reservation=lock,
    )
    service._invocations[request.invocation_id] = inv

    cleanup_task = service._ensure_cleanup(inv, "test cleanup", "cancelled")
    assert inv.cleanup_budget is not None
    done = await _observe_task(cleanup_task, deadline=inv.cleanup_budget.force_deadline + 0.5)
    assert done, "cleanup must resolve at the shared deadline, not hang indefinitely"

    engine_cancel_task = inv.engine_cancel_task
    owner_stop_task = inv.owner_stop_task
    assert engine_cancel_task is not None and engine_cancel_task.done()
    assert engine_cancel_task.exception() is None
    assert owner_stop_task is not None and owner_stop_task.done()
    assert owner_stop_task.exception() is not None

    assert inv.state == "unknown"
    assert inv.reservation is lock
    assert lock.release_count == 0
    assert service._invocations[request.invocation_id] is inv
    assert service._invocations[request.invocation_id].owner_task is owner_task
    assert service._invocations[request.invocation_id].cancel_task is cleanup_task

    successor = _request(
        public_config_fixture.config_version, session_ref=session_ref, invocation_id="successor"
    )
    successor_caller = asyncio.ensure_future(service.start(successor))
    await asyncio.sleep(0.05)
    assert "successor" not in engine.start_calls
    assert not successor_caller.done()
    successor_caller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await successor_caller
    successor_inv = service._invocations.get("successor")
    if successor_inv is not None:
        for task in (successor_inv.owner_task, successor_inv.deadline_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    await service.cancel(request.invocation_id, "retry")
    assert service._invocations[request.invocation_id].cancel_task is cleanup_task
    assert inv.engine_cancel_task is engine_cancel_task
    assert inv.owner_stop_task is owner_stop_task
    assert lock.release_count == 0

    if owner_mode == "stalled":
        release.set()
    if not owner_task.done():
        owner_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, RuntimeError, asyncio.TimeoutError):
        await asyncio.wait_for(owner_task, timeout=5)


async def test_cleanup_bounds_a_stalled_engine_cancel_not_just_the_owner(
    tmp_path, public_config_fixture, monkeypatch
) -> None:
    """I1: `engine.cancel()` itself never returning (and never bounding itself) must not
    hang the whole retained cleanup indefinitely — the service's own bounded observation
    against the shared clock must retain `unknown` with the reservation held, keeping the
    engine-cancel task identity retained (never cancelled/torn down) for inspection."""
    monkeypatch.setattr("ach_runtime.execution.service.CANCEL_KILL_BOUND_SECONDS", 0.2)
    engine = FakeEngine(
        hang=frozenset({"inv-1"}), cancel_permit=asyncio.Event()
    )  # never set: engine.cancel() hangs forever
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    service._sessions._locks["se" * 16] = lock
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    inv = service._invocations[request.invocation_id]
    assert lock.locked()

    start = time.monotonic()
    await asyncio.wait_for(service.cancel(request.invocation_id, "operator request"), timeout=3.0)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, "cleanup must resolve at the shared deadline, not hang on a stalled engine"
    status = await service.status(request.invocation_id)
    assert status.state == "unknown"
    assert lock.locked()
    assert lock.release_count == 0
    assert inv.engine_cancel_task is not None
    assert not inv.engine_cancel_task.cancelled()
    assert not inv.engine_cancel_task.done()  # retained, never torn down on expiry


async def test_owner_finishing_within_frozen_confirmation_window_is_not_unknown(
    tmp_path, public_config_fixture, monkeypatch
) -> None:
    """I2: once an actual force has stamped the shared `confirmation_deadline`, both
    `_stop_owner` and the outer cleanup observation must extend to that SAME frozen
    boundary — an owner that misses the original `force_deadline` but stops within the
    allowed confirmation window must still finalize cleanly, never prematurely `unknown`."""
    from ach_runtime.execution.service import _Invocation

    monkeypatch.setattr("ach_runtime.execution.service.CANCEL_KILL_BOUND_SECONDS", 0.2)

    class _ForcingEngine(FakeEngine):
        async def cancel(self, invocation_id, reason, *, _halt_budget=None):
            if _halt_budget is not None:
                _halt_budget.mark_forced()  # simulates native's actual first-force stamp
            await super().cancel(invocation_id, reason, _halt_budget=_halt_budget)

    engine = _ForcingEngine()
    service, engine = _service(tmp_path, public_config_fixture, engine)
    lock = _ObservedLock()
    session_ref = "se" * 16
    service._sessions._locks[session_ref] = lock
    await lock.acquire()
    request = _request(public_config_fixture.config_version, deadline_seconds=60.0)

    release = asyncio.Event()

    async def _owner():
        with contextlib.suppress(asyncio.CancelledError):
            await release.wait()
        await release.wait()

    owner_task = asyncio.ensure_future(_owner())
    started_future: asyncio.Future = asyncio.get_running_loop().create_future()
    inv = _Invocation(
        request=request,
        public=public_config_fixture,
        run_id="r",
        started=started_future,
        owner_task=owner_task,
        reservation=lock,
    )
    service._invocations[request.invocation_id] = inv

    cleanup_task = service._ensure_cleanup(
        inv, "test cleanup", "cancelled", join=lambda: service._stop_owner(inv)
    )
    await asyncio.sleep(0.25)  # past the 0.2s force_deadline, still inside the +5s window
    assert not cleanup_task.done(), "must still be observing the frozen confirmation window"
    release.set()
    await asyncio.wait_for(cleanup_task, timeout=5)

    assert inv.state == "cancelled"
    assert lock.release_count == 1



async def test_session_start_error_redacts_facade_tokens(tmp_path, public_config_fixture):
    service, _ = _service(tmp_path, public_config_fixture)
    service.configure(_with_session_start(
        public_config_fixture, "echo 'GET http://c/facades/mcp/t/tok-123/mcp/x 401' >&2; exit 1"
    ))
    await service.start(_request(public_config_fixture.config_version))
    error = (await service.status("inv-1")).error
    assert "tok-123" not in error and "/t/<redacted>/" in error

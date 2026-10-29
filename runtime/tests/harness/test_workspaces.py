# SPDX-License-Identifier: MIT
"""harness/workspaces.py — WorkspaceManager coordination over an in-memory `FakeSource`."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.harness import workspaces as ws
from ach_runtime.harness.refs import workspace_name
from ach_runtime.harness.refs import workspace_ref as compute_workspace_ref
from ach_runtime.harness.workspaces import (
    InstanceMismatch,
    WorkspaceAssemblyUnavailable,
    WorkspaceCapacityExceeded,
    WorkspaceConflict,
    WorkspaceEndpoint,
    WorkspaceManager,
    WorkspaceRef,
    WorkspaceRetireTimeout,
    WorkspaceUnhosted,
)
from ach_runtime.storage.api import PersistReceipt, SnapshotTransientFailure
from ach_runtime.storage.refs import StateRef
from fake_source import FakeSource

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


def _config(*, capacity: int = 4, shutdown: float = 5, ready: int | None = None) -> RuntimeConfig:
    config = RuntimeConfig.model_validate(json.loads(FIXTURE_PATH.read_text(encoding="utf-8")))
    return config.model_copy(update={
        "limits": config.limits.model_copy(update={"max_active_workspaces": capacity}),
        "workspace": config.workspace.model_copy(update={"shutdown_timeout_seconds": shutdown, **({"ready_timeout_seconds": ready} if ready is not None else {})}),
    })


def test_startup_deadline_uses_the_configured_ready_timeout() -> None:
    h = _Harness(_config(ready=90))
    before = time.monotonic()
    assert 89 < h.manager._startup_deadline(None) - before <= 91


def test_startup_deadline_is_still_clipped_by_the_admission_deadline() -> None:
    h = _Harness(_config(ready=90))
    near = datetime.now(timezone.utc) + timedelta(seconds=5)
    before = time.monotonic()
    assert h.manager._startup_deadline(near) - before <= 6


def _ref(config: RuntimeConfig, key: str) -> WorkspaceRef:
    digest = compute_workspace_ref(config.agent.namespace, config.agent.uid, key)
    return WorkspaceRef(config.agent.namespace, config.agent.uid, digest)


def _name(config: RuntimeConfig, ref: WorkspaceRef) -> str:
    return workspace_name(config.agent.name, ref.workspace_ref)


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


class _Harness:
    """Manager + FakeSource + recording callbacks. Instance IDs are `id-<n>` per probe."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self.source = FakeSource()
        self.restored: list[WorkspaceEndpoint] = []
        self.committed: list[WorkspaceEndpoint] = []
        self.reconnected: list[WorkspaceEndpoint] = []
        self.commit_error: BaseException | None = None
        self.probe_fail: set[str] = set()
        self.probes = 0
        self.manager = WorkspaceManager(
            config, self.source,
            restore_workspace=self.restore, commit_workspace=self.commit,
            reconnect_workspace=self.reconnect, wait_mini_health=self.health,
        )

    async def health(self, base_url: str, name: str, deadline: float) -> str:
        self.source.events.append("probe")
        if name in self.probe_fail:
            raise RuntimeError("probe failed")
        self.probes += 1
        return f"id-{self.probes}"

    async def restore(self, endpoint: WorkspaceEndpoint) -> None:
        self.source.events.append("restore")
        self.restored.append(endpoint)

    async def reconnect(self, endpoint: WorkspaceEndpoint) -> None:
        self.source.events.append("reconnect")
        self.reconnected.append(endpoint)

    async def commit(self, endpoint: WorkspaceEndpoint) -> PersistReceipt | None:
        self.committed.append(endpoint)
        if self.commit_error is not None:
            raise self.commit_error
        ref = StateRef(agent=self.config.agent, workspace_ref=endpoint.ref.workspace_ref,
                       domain="workspace", session_ref=None)
        return PersistReceipt(ref=ref, snapshot_id="snap-1", writer_epoch=1, generation=1,
                              etag="e", checksum="c", size=1, version=1)

    def adopt_unit(self, ref: WorkspaceRef) -> None:
        self.source.adopted.append((ref, self.source.placement(_name(self.config, ref))))


@pytest.fixture
def alarms(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(ws, "alarm", lambda kind, **_: seen.append(kind))
    return seen


# 1 ---------------------------------------------------------------------------------------------

async def test_ensure_runs_the_activation_sequence_and_coalesces() -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-1")
    h.source.place_gate = asyncio.Event()
    first = asyncio.ensure_future(h.manager.ensure(ref))
    second = asyncio.ensure_future(h.manager.ensure(ref))
    await _settle()
    assert not first.done() and not second.done()
    h.source.place_gate.set()
    a, b = await asyncio.gather(first, second)

    assert a == b == h.manager.active_endpoint(ref.workspace_ref)
    assert a.instance_id == "id-1"
    assert a.base_url == f"http://{_name(h.config, ref)}"
    assert h.source.place_calls == [_name(h.config, ref)]
    assert h.source.events == ["place", "probe", "observed", "restore", "reconnect", "serving"]


# 2 ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("flag", ["observed_ok", "serving_ok"])
async def test_replaced_registration_fails_activation_and_frees_slot_after_retire(flag: str) -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-2")
    key = ref.workspace_ref
    setattr(h.source, flag, False)
    h.source.retire_gate = asyncio.Event()
    task = asyncio.ensure_future(h.manager.ensure(ref))
    await _settle()
    assert h.source.retire_calls == [_name(h.config, ref)]
    assert key in h.manager._occupied and not task.done()
    h.source.retire_gate.set()
    with pytest.raises(WorkspaceConflict):
        await task
    assert key not in h.manager._occupied
    assert h.manager.active_endpoint(key) is None


async def test_place_failures_release_or_hold_the_slot() -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-3")
    key = ref.workspace_ref
    h.source.place_error = WorkspaceUnhosted("no unit")
    with pytest.raises(WorkspaceUnhosted):
        await h.manager.ensure(ref)
    assert key not in h.manager._occupied

    h.source.place_error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await h.manager.ensure(ref)
    assert key not in h.manager._occupied

    h.source.place_error = WorkspaceConflict("already hosted")
    with pytest.raises(WorkspaceConflict):
        await h.manager.ensure(ref)
    assert key in h.manager._occupied  # the source holds something under that name


# 3 ---------------------------------------------------------------------------------------------

async def test_close_commits_validates_retires_and_frees_the_slot() -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-4")
    endpoint = await h.manager.ensure(ref)
    await h.manager.close(ref)
    assert h.committed == [endpoint]
    assert h.source.retire_calls == [endpoint.name]
    assert ref.workspace_ref not in h.manager._occupied
    assert h.manager.active_endpoint(ref.workspace_ref) is None


async def test_commit_failure_keeps_serving_or_records_the_error() -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-5")
    key = ref.workspace_ref
    endpoint = await h.manager.ensure(ref)
    h.commit_error = SnapshotTransientFailure("try later")
    with pytest.raises(SnapshotTransientFailure):
        await h.manager.close(ref)
    assert h.manager.active_endpoint(key) == endpoint
    assert key in h.manager._occupied
    assert isinstance(h.manager._errors[key], SnapshotTransientFailure)
    assert h.source.retire_calls == []

    h.commit_error = RuntimeError("disk full")
    with pytest.raises(RuntimeError):
        await h.manager.close(ref)
    assert isinstance(h.manager._errors[key], RuntimeError)
    assert key in h.manager._occupied and h.source.retire_calls == []


# 4 ---------------------------------------------------------------------------------------------

async def test_retire_timeout_keeps_the_slot_and_late_completion_frees_it_once(alarms: list[str]) -> None:
    h = _Harness(_config(capacity=1, shutdown=0.05))
    ref = _ref(h.config, "wk-6")
    key = ref.workspace_ref
    await h.manager.ensure(ref)
    h.source.retire_gate = asyncio.Event()
    await h.manager.close(ref)  # the commit is durable: a timeout is an anomaly, not a failure
    assert alarms == ["workspace_retire_timeout"]
    assert key in h.manager._occupied
    assert isinstance(h.manager._errors[key], WorkspaceRetireTimeout)

    h.source.retire_gate.set()
    await _settle()
    assert key not in h.manager._occupied and key not in h.manager._errors

    # The same ref takes the slot again; a stale second completion must not free it.
    h.source.retire_gate = None
    await h.manager.ensure(ref)
    assert key in h.manager._occupied
    stale = asyncio.get_running_loop().create_future()
    stale.set_result(None)
    h.manager._retired_late(key, WorkspaceRetireTimeout("old"), stale)
    assert key in h.manager._occupied


# 5 ---------------------------------------------------------------------------------------------

async def test_recover_ignores_older_instance_and_replaces_the_matching_one(alarms: list[str]) -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-7")
    key = ref.workspace_ref
    old = await h.manager.ensure(ref)

    assert await h.manager.recover(ref, "id-older") is None
    assert h.manager.active_endpoint(key) == old and alarms == []

    new = await h.manager.recover(ref, old.instance_id)
    assert alarms == ["pod_unhealthy"]
    assert h.manager.is_lost(old.instance_id)
    assert new is not None and new.instance_id != old.instance_id
    assert h.manager.active_endpoint(key) == new
    assert h.source.retire_calls == [old.name]
    assert len(h.source.place_calls) == 2
    assert h.restored == [old, new]


# 6 ---------------------------------------------------------------------------------------------

async def test_recovery_does_not_place_before_the_late_retire_completes(alarms: list[str]) -> None:
    h = _Harness(_config(capacity=1, shutdown=0.05))
    ref = _ref(h.config, "wk-8")
    key = ref.workspace_ref
    old = await h.manager.ensure(ref)
    h.source.retire_gate = asyncio.Event()
    assert await h.manager.recover(ref, old.instance_id) is None
    assert len(h.source.place_calls) == 1
    assert key in h.manager._occupied
    with pytest.raises(WorkspaceRetireTimeout):
        await h.manager.ensure(ref)
    assert len(h.source.place_calls) == 1

    h.source.retire_gate.set()
    await _settle()
    assert key not in h.manager._occupied
    assert len(h.source.place_calls) == 1  # recovery ended; the next ensure places


# 7 ---------------------------------------------------------------------------------------------

async def test_report_lost_schedules_recover_and_logs_failures(caplog: pytest.LogCaptureFixture) -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-9")
    endpoint = await h.manager.ensure(ref)
    calls: list[tuple[WorkspaceRef, str]] = []

    async def recover(r: WorkspaceRef, lost: str) -> None:
        calls.append((r, lost))
        raise RuntimeError("boom")

    h.manager.recover = recover  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING, logger=ws.__name__):
        h.manager.report_lost(endpoint)
        await asyncio.gather(*h.manager._loss_tasks, return_exceptions=True)
        await _settle()
    assert calls == [(ref, endpoint.instance_id)]
    assert "recovery after a lost instance failed" in caplog.text
    assert not h.manager._loss_tasks


# 8 / 9 -----------------------------------------------------------------------------------------

async def test_adoption_over_capacity_adopts_all_and_new_ensure_waits() -> None:
    h = _Harness(_config(capacity=1))
    a, b, c = (_ref(h.config, k) for k in ("ad-a", "ad-b", "ad-c"))
    h.adopt_unit(a)
    h.adopt_unit(b)
    await h.manager.adopt()
    assert set(h.manager.active_endpoints()) == {a.workspace_ref, b.workspace_ref}
    assert h.manager._occupied == {a.workspace_ref, b.workspace_ref}
    assert h.source.events.count("serving") == 2 and h.restored == []

    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    waiting = asyncio.ensure_future(h.manager.ensure(c, deadline_at=deadline))
    await _settle()
    assert not waiting.done() and h.source.place_calls == []
    await h.manager.close(a)
    await h.manager.close(b)
    endpoint = await asyncio.wait_for(waiting, 5)
    assert endpoint.ref == c


async def test_ensure_without_a_slot_fails_at_the_deadline() -> None:
    h = _Harness(_config(capacity=1))
    await h.manager.ensure(_ref(h.config, "full-a"))
    with pytest.raises(WorkspaceCapacityExceeded):
        await h.manager.ensure(_ref(h.config, "full-b"))


async def test_ensure_waits_for_adoption_before_placing() -> None:
    h = _Harness(_config())
    ref = _ref(h.config, "wk-10")
    h.source.adopt_gate = asyncio.Event()
    task = asyncio.ensure_future(h.manager.ensure(ref))
    await _settle()
    assert not task.done() and h.source.place_calls == []
    h.source.adopt_gate.set()
    await task
    assert len(h.source.place_calls) == 1


# 10 --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("retire_times_out", [False, True])
async def test_instance_mismatch_at_close_is_a_loss(
    alarms: list[str], retire_times_out: bool
) -> None:
    h = _Harness(_config(shutdown=0.05 if retire_times_out else 5))
    ref = _ref(h.config, "wk-11")
    key = ref.workspace_ref
    endpoint = await h.manager.ensure(ref)
    if retire_times_out:
        h.source.retire_gate = asyncio.Event()

    async def commit(e: WorkspaceEndpoint) -> None:
        h.manager.report_lost(e)  # what the transfer path does before raising
        raise InstanceMismatch()

    h.manager._commit = commit  # type: ignore[assignment]
    await h.manager.close(ref)  # a loss, not a failed close: nothing is raised
    await asyncio.gather(*h.manager._loss_tasks, return_exceptions=True)

    assert alarms.count("pod_unhealthy") == 1
    assert h.manager.is_lost(endpoint.instance_id)
    assert h.source.retire_calls == [endpoint.name]
    assert h.manager.active_endpoint(key) is None
    if retire_times_out:
        assert isinstance(h.manager._errors[key], WorkspaceRetireTimeout)
        h.source.retire_gate.set()
        await _settle()
    assert key not in h.manager._occupied and key not in h.manager._errors


# 11 --------------------------------------------------------------------------------------------

async def test_failed_adoption_keeps_the_slot_until_the_host_confirms() -> None:
    h = _Harness(_config(capacity=2))
    good, bad = _ref(h.config, "fa-good"), _ref(h.config, "fa-bad")
    h.adopt_unit(good)
    h.adopt_unit(bad)
    bad_name = _name(h.config, bad)
    h.probe_fail.add(bad_name)
    h.source.retire_gate = asyncio.Event()

    await h.manager.adopt()  # does not wait for the DELETE
    assert bad_name in h.source.released
    assert bad.workspace_ref in h.manager._occupied
    assert "retire" in h.manager._owners[bad.workspace_ref].tasks
    assert set(h.manager.active_endpoints()) == {good.workspace_ref}

    h.probe_fail.clear()
    again = asyncio.ensure_future(h.manager.ensure(bad))
    await _settle()
    assert not again.done() and h.source.place_calls == []
    assert bad.workspace_ref in h.manager._occupied

    h.source.retire_gate.set()
    endpoint = await asyncio.wait_for(again, 5)
    assert h.source.place_calls == [bad_name]
    assert endpoint.ref == bad
    assert h.source.retire_calls == [bad_name]


async def test_failed_adoption_is_deferred_while_gateway_unavailable() -> None:
    h = _Harness(_config(capacity=2))
    bad = _ref(h.config, "fa-gw")
    h.adopt_unit(bad)
    bad_name = _name(h.config, bad)
    h.probe_fail.add(bad_name)
    h.source.gateway = False

    await h.manager.adopt()
    assert bad_name not in h.source.released
    assert bad.workspace_ref not in h.manager.active_endpoints()

    h.probe_fail.clear()
    h.source.gateway = True
    await h.manager._adopt_when_gateway_returns(bad, h.source.adopted[0][1])
    assert bad.workspace_ref in h.manager.active_endpoints()
    assert bad_name not in h.source.released


async def test_failed_adoption_with_retire_timeout_never_places() -> None:
    h = _Harness(_config(shutdown=0.05))
    bad = _ref(h.config, "fa-timeout")
    h.adopt_unit(bad)
    h.probe_fail.add(_name(h.config, bad))
    h.source.retire_gate = asyncio.Event()
    await h.manager.adopt()
    with pytest.raises(WorkspaceRetireTimeout):
        await h.manager.ensure(bad)
    assert h.source.place_calls == []
    assert bad.workspace_ref in h.manager._occupied

    h.source.retire_gate.set()
    await _settle()
    assert bad.workspace_ref not in h.manager._occupied


# construction ----------------------------------------------------------------------------------

def test_reconnect_callback_is_required() -> None:
    with pytest.raises(WorkspaceAssemblyUnavailable):
        WorkspaceManager(_config(), FakeSource(), restore_workspace=None,
                         commit_workspace=None, reconnect_workspace=None)

# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
import pytest

from ach_runtime.harness.lifecycle import Lifecycle
from ach_runtime.config.schema import AgentIdentity


class _Admission:
    def __init__(self): self.tickets = []
    def has_work(self, workspace_ref, session_ref=None):
        return any(t.workspace_ref == workspace_ref and (session_ref is None or t.session_ref == session_ref) for t in self.tickets)


class _Manager:
    def __init__(self):
        self._active = {}
        self._restore = None
        self._commit = None
        self.lost: list = []

    def report_lost(self, endpoint):
        self.lost.append(endpoint)

    close = staticmethod(lambda ref: _close(ref))

    def bind_callbacks(self, *, restore=None, commit=None, **_):
        self._restore = restore or self._restore
        self._commit = commit or self._commit

    def active_endpoint(self, key):
        return self._active.get(key)

    def active_endpoints(self):
        return dict(self._active)


class _Control:
    def __init__(self):
        policy = type("P", (), {"enabled": False, "retention_days": 0})
        self.config = type("C", (), {"workspace": type("W", (), {"persistence": policy(), "session": type("S", (), {"persistence": policy(), "idle_timeout_seconds": 20})(), "idle_timeout_seconds": 30, "shutdown_timeout_seconds": 5})(), "agent": AgentIdentity(name="agent", namespace="ach", uid="3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77")})()
        self.admission = _Admission()
        self.manager = _Manager()
        self.storage = None
        self.mini_client = None
        self._headers = lambda e: {}
        self.workspace_hooks: list[tuple[str, bool]] = []
        self.workspace_hook_error: dict[str, Exception] = {}

    async def run_workspace_hook(self, endpoint, phase, *, restored=False):
        self.workspace_hooks.append((phase, restored))
        if phase in self.workspace_hook_error:
            raise self.workspace_hook_error[phase]


async def _close(ref): pass


def test_lifecycle_exposes_manager_callbacks_and_owned_close() -> None:
    async def scenario():
        control = _Control()
        lifecycle = Lifecycle(control)
        assert control.manager._restore == lifecycle.restore_workspace
        assert control.manager._commit == lifecycle.commit_workspace
        await lifecycle.close()
    asyncio.run(scenario())


def test_owner_health_ok_needs_no_engine() -> None:
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
            "ws", "pod", "http://mini",
        )
        control.manager._active = {"a" * 64: endpoint}
        control.delivery = _Delivery()
        control.manager.closed = []
        control.manager.close = _close_record(control.manager)
        calls = []

        async def mini_json(observed, method, path, payload=None):
            calls.append(path)
            if path == "/healthz":
                return 200, {"status": "ok"}
            if path == "/sessions":
                return 200, []
            raise AssertionError(path)

        control._mini_json = mini_json
        recovered = []

        async def recover(ref, lost_instance_id):
            assert lost_instance_id == endpoint.instance_id
            recovered.append(ref)
            raise AssertionError("a healthy owner must not be recovered")

        control.manager.recover = recover
        lifecycle = Lifecycle(control)
        now = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(now)
        await lifecycle.tick(now + timedelta(seconds=31))
        assert calls.count("/healthz") == 2
        assert calls.count("/sessions") == 2
        assert recovered == []
        assert control.manager.closed == [endpoint.ref]
        await lifecycle.close()

    asyncio.run(scenario())


def test_owner_health_failure_calls_recover_under_operation_lock() -> None:
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.harness.workspaces import WorkspaceAssemblyUnavailable

    async def scenario():
        alarms = []
        control = _Control()
        workspace_ref = "a" * 64
        other_ref = "b" * 64
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, workspace_ref),
            "ws", "pod", "http://mini",
        )
        other = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, other_ref),
            "ws-other", "pod-other", "http://mini",
        )
        control.manager._active = {workspace_ref: endpoint, other_ref: other}
        control.delivery = _Delivery()
        calls = []
        probe = {workspace_ref: (503, {"status": "unhealthy"}), other_ref: (200, {"status": "ok"})}
        other_inventory = asyncio.Event()

        async def mini_json(observed, method, path, payload=None):
            owner = observed.ref.workspace_ref
            calls.append((owner, path))
            if path == "/healthz":
                result = probe[owner]
                if isinstance(result, BaseException):
                    raise result
                return result
            if path == "/sessions":
                if owner == other_ref:
                    other_inventory.set()
                return 200, []
            raise AssertionError(path)

        control._mini_json = mini_json
        lifecycle = Lifecycle(control, alarm=alarms.append)
        lifecycle._failed_workspace_saves.add((workspace_ref, endpoint.instance_id))
        recovery_entered = asyncio.Event()
        release_recovery = asyncio.Event()
        recovered = []

        async def recover(ref, lost_instance_id):
            assert lost_instance_id == endpoint.instance_id
            recovered.append(ref)
            recovery_entered.set()
            await release_recovery.wait()
            return endpoint

        control.manager.recover = recover
        tick = asyncio.create_task(lifecycle.tick(datetime(2026, 10, 1, tzinfo=UTC)))
        operation_entered = asyncio.Event()

        async def same_workspace_operation():
            async with lifecycle.operation(workspace_ref):
                operation_entered.set()

        same_workspace = None
        try:
            await asyncio.wait_for(recovery_entered.wait(), timeout=2)
            same_workspace = asyncio.create_task(same_workspace_operation())
            await asyncio.wait_for(other_inventory.wait(), timeout=2)
            await asyncio.sleep(0)
            assert not operation_entered.is_set()
            assert (workspace_ref, "/healthz") in calls
            assert (workspace_ref, "/sessions") not in calls
            assert (other_ref, "/sessions") in calls
            release_recovery.set()
            await asyncio.wait_for(tick, timeout=2)
            await asyncio.wait_for(same_workspace, timeout=2)
        finally:
            release_recovery.set()
            if same_workspace is not None:
                await asyncio.gather(same_workspace, return_exceptions=True)
            await asyncio.gather(tick, return_exceptions=True)

        assert recovered == [endpoint.ref]
        assert alarms == []  # the manager emits pod_unhealthy now

        probe[workspace_ref] = WorkspaceAssemblyUnavailable("mini-harness instance ID did not match observation")
        await lifecycle.tick(datetime(2026, 10, 1, 0, 0, 1, tzinfo=UTC))
        assert recovered == [endpoint.ref, endpoint.ref]
        assert alarms == []
        assert calls.count((workspace_ref, "/healthz")) == 2
        assert calls.count((workspace_ref, "/sessions")) == 0
        await lifecycle.close()

    asyncio.run(scenario())


def test_an_export_answered_by_another_instance_reports_the_loss_to_the_manager() -> None:
    import httpx

    from ach_runtime.harness.workspaces import InstanceMismatch, WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini",
        )

        async def handler(request):
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "restarted-unit"}, content=b"empty")

        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        lifecycle = Lifecycle(control, alarm=lambda reason: None)
        with pytest.raises(InstanceMismatch):
            await lifecycle._save_session(endpoint, "b" * 64)
        assert control.manager.lost == [endpoint]
        await control.mini_client.aclose()
        await lifecycle.close()

    asyncio.run(scenario())


def test_storage_restore_failure_rethrows_and_counts_finite_restore_anomaly(caplog) -> None:
    from types import SimpleNamespace
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL

    async def scenario():
        control = _Control()
        control.config.workspace.persistence.enabled = True
        control.storage = SimpleNamespace(restore=fail_restore)
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
        )
        before = ANOMALIES_TOTAL.labels(kind="restore_failed")._value.get()
        lifecycle = Lifecycle(control)
        with pytest.raises(RuntimeError, match="restore-secret"):
            await lifecycle.restore_workspace(endpoint)
        assert ANOMALIES_TOTAL.labels(kind="restore_failed")._value.get() == before + 1
        await lifecycle.close()

    async def fail_restore(_ref, *, retention_days=0):
        raise RuntimeError("restore-secret")

    asyncio.run(scenario())
    assert "restore-secret" not in caplog.text


def test_saved_suspend_hook_anomaly_is_reported_after_archive_export(monkeypatch) -> None:
    import ach_runtime.harness.lifecycle as lifecycle_module
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
        )
        reports = []
        session_ref = "b" * 64

        async def mini_json(_endpoint, method, path, payload=None):
            if method == "GET" and path.endswith("/info"):
                return 200, {"suspendHookFailed": True}
            if method == "POST" and path == f"/sessions/{session_ref}/retire":
                return 200, {"status": "retired", "sessionRef": session_ref}
            raise AssertionError((method, path))

        control._mini_json = mini_json
        async def archive(*args, **kwargs):
            yield b"saved archive"

        monkeypatch.setattr(lifecycle_module, "get_archive", archive)
        lifecycle = Lifecycle(control, alarm=reports.append)
        await lifecycle._save_session(endpoint, session_ref)
        assert reports == ["Harness suspend hook failed"]
        await lifecycle.close()

    asyncio.run(scenario())


def test_idle_session_then_workspace() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        state = {"active": True}
        seen = []
        async def handler(request):
            seen.append(request.url.path)
            if request.url.path == "/sessions":
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json=[{"sessionRef": "b"*64}])
            if request.url.path.endswith("/info"):
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json={"active": state["active"], "uncertain": False, "initialized": True})
            if request.url.path.endswith("/archive"):
                state["active"] = False
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, content=b"snapshot")
            if request.url.path.endswith("/retire"):
                session_ref = request.url.path.split("/")[2]
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"},
                                      json={"status": "retired", "sessionRef": session_ref})
            raise AssertionError(request.url)
        control = _Control()
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        control.manager._bootstrap_complete = True
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", "a"*64), "ws", "pod", "http://mini")
        control.manager._active = {"a"*64: endpoint}
        control.delivery = _Delivery()
        control.manager.closed = []
        control.manager.close = _close_record(control.manager)
        lifecycle = Lifecycle(control)
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        await lifecycle.tick(start + timedelta(seconds=21))
        assert "/sessions/" + "b"*64 + "/archive" in seen
        assert control.manager.closed == []
        await lifecycle.tick(start + timedelta(seconds=22))
        await lifecycle.tick(start + timedelta(seconds=53))
        assert control.manager.closed == [endpoint.ref]
        await lifecycle.close()
        await control.mini_client.aclose()
    asyncio.run(scenario())


def test_start_owns_one_tick_task_and_close_joins_it() -> None:
    async def scenario():
        control = _Control()
        alarms = []
        lifecycle = Lifecycle(control, alarm=alarms.append)
        tick_started = asyncio.Event()

        async def failing_tick(now):
            tick_started.set()
            raise RuntimeError("save failed")

        lifecycle.tick = failing_tick
        await lifecycle.start()
        task = lifecycle._tick_task
        await lifecycle.start()
        assert lifecycle._tick_task is task
        await asyncio.wait_for(tick_started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert alarms == ["Harness lifecycle tick failed"]
        await lifecycle.close()
        assert task.done()

    asyncio.run(scenario())


def test_writer_pod_and_expected_base() -> None:
    import hashlib
    from ach_runtime.storage.api import PersistReceipt, RestoreReceipt, WriterLease
    from ach_runtime.storage.refs import DOMAIN_WORKSPACE
    import httpx
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        events = []
        class Storage:
            restores = 0
            persists = []
            async def restore(self, ref, *, retention_days=0):
                self.restores += 1
                events.append(("restore", ref, retention_days))
                etag = "before-writer" if self.restores == 1 else "after-writer"
                async def opened():
                    yield b"stored"
                return RestoreReceipt(ref, f"snapshot-{self.restores}", 4, 1, etag,
                                      "checksum", 6, 1, opened)
            async def acquire_writer(self, ref, writer_instance_id):
                events.append(("writer", ref, writer_instance_id))
                return WriterLease(ref, writer_instance_id, 5, "after-writer")
            async def persist(self, ref, snapshot_id, stream, lease, expected_base, *, retention_days=0):
                payload = b"".join([block async for block in stream])
                events.append(("persist", ref, snapshot_id, lease.writer_instance_id, expected_base, payload, retention_days))
                receipt = PersistReceipt(ref, snapshot_id, lease.epoch, len(self.persists)+2,
                                        f"committed-{len(self.persists)+1}", hashlib.sha256(payload).hexdigest(), len(payload), 1)
                self.persists.append(receipt)
                return receipt
        storage = Storage()
        control = _Control()
        control.config.workspace.persistence.enabled = True
        control.config.workspace.persistence.retention_days = 17
        control.storage = storage
        async def mini(request):
            events.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "instance-id"}, content=b"archive")
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "instance-id"}, json={"status": "restored"})
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a"*64),
                                    "workspace", "instance-id", "http://mini")
        lifecycle = Lifecycle(control)
        await lifecycle.restore_workspace(endpoint)
        await lifecycle.commit_workspace(endpoint)
        await lifecycle.commit_workspace(endpoint)
        writes = [event for event in events if event[0] == "persist"]
        assert [event[2] for event in events if event[0] == "restore"] == [17]
        assert [write[-1] for write in writes] == [17, 17]
        assert events[0][0] == "restore"
        writer_index = next(i for i, event in enumerate(events) if event[0] == "writer")
        assert events[1][0] == "PUT"
        assert events[writer_index] == ("writer", writes[0][1], "instance-id")
        assert writes[0][1].domain == DOMAIN_WORKSPACE
        assert writes[0][1].agent.uid == control.config.agent.uid
        assert writes[0][3] == "instance-id"
        assert [write[4] for write in writes] == ["after-writer", "committed-1"]
        assert all(write[2] != write[4] for write in writes)
        await control.mini_client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("domain", ["workspace", "session"])
def test_close_transient_export_and_save_retry_one_id_and_bytes(monkeypatch, domain) -> None:
    import hashlib
    import httpx
    import ach_runtime.harness.lifecycle as lifecycle_module
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import PersistReceipt, SnapshotTransientFailure, WriterLease

    class Interrupted(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            raise httpx.ReadTimeout("interrupted export")

        async def aclose(self):
            return None

    async def scenario():
        control = _Control()
        if domain == "workspace":
            control.config.workspace.persistence.enabled = True
            control.config.workspace.persistence.retention_days = 6
        else:
            control.config.workspace.session.persistence.enabled = True
            control.config.workspace.session.persistence.retention_days = 9
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
        )
        archive = b"complete captured archive"
        calls = {"get": 0, "persist": []}

        class Storage:
            async def acquire_writer(self, ref, writer_instance_id):
                return WriterLease(ref, writer_instance_id, 4)

            async def restore(self, ref, *, retention_days=0):
                assert retention_days == (6 if domain == "workspace" else 9)
                return None

            async def persist(self, ref, snapshot_id, stream, lease, expected_base, *, retention_days=0):
                payload = b"".join([chunk async for chunk in stream])
                calls["persist"].append((snapshot_id, payload, lease, expected_base, retention_days))
                if len(calls["persist"]) < 3:
                    raise SnapshotTransientFailure("temporary storage outage")
                return PersistReceipt(
                    ref, snapshot_id, lease.epoch, 2, "etag", hashlib.sha256(payload).hexdigest(),
                    len(payload), 1,
                )

        control.storage = Storage()

        async def response(request):
            if domain == "session" and request.url.path.endswith("/info"):
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json={})
            if domain == "session" and request.url.path.endswith("/retire"):
                session_ref = request.url.path.split("/")[2]
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"},
                                      json={"status": "retired", "sessionRef": session_ref})
            calls["get"] += 1
            stream = Interrupted() if calls["get"] == 1 else None
            return httpx.Response(
                200, headers={"X-ACH-Instance-Id": "pod"},
                content=None if stream is not None else archive, stream=stream,
            )

        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        control._headers = lambda observed: {"X-ACH-Instance-Id": observed.instance_id}
        control._mini_json = _mini_json(control)
        delays = []

        async def fake_sleep(delay):
            delays.append(delay)

        monkeypatch.setattr(lifecycle_module.asyncio, "sleep", fake_sleep)
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)
        if domain == "workspace":
            receipt = await lifecycle.commit_workspace(endpoint)
        else:
            await lifecycle._save_session(endpoint, "b" * 64)
            receipt = None
        assert receipt is None or receipt.snapshot_id == calls["persist"][0][0]
        assert calls["get"] == 2  # partial failed export was discarded; completed archive reused
        assert len(calls["persist"]) == 3
        assert len({item[0] for item in calls["persist"]}) == 1
        assert {item[1] for item in calls["persist"]} == {archive}
        assert len({id(item[2]) for item in calls["persist"]}) == 1
        assert {item[3] for item in calls["persist"]} == {None}
        assert {item[4] for item in calls["persist"]} == ({6} if domain == "workspace" else {9})
        assert delays == [1, 2, 4]
        await control.mini_client.aclose()

    asyncio.run(scenario())


def test_close_exhaustion_retains_owner_until_cooldown_recovery(monkeypatch) -> None:
    import hashlib
    import httpx
    from datetime import timedelta
    import ach_runtime.harness.lifecycle as lifecycle_module
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import PersistReceipt, SnapshotTransientFailure, WriterLease

    async def scenario():
        control = _Control()
        control.config.workspace.persistence.enabled = True
        control.config.workspace.idle_timeout_seconds = 0
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
        )
        key = endpoint.ref.workspace_ref
        control.manager._active = {key: endpoint}
        control.manager._occupied = {key}
        control.manager._errors = {}
        control.manager.closed = []
        control.delivery = _Delivery()
        control.config.workspace.shutdown_timeout_seconds = 5
        captured = []
        backend = {"ready": False}

        class Storage:
            async def acquire_writer(self, ref, pod):
                return WriterLease(ref, pod, 2)

            async def restore(self, ref, *, retention_days=0):
                return None

            async def persist(self, ref, snapshot_id, stream, lease, base, *, retention_days=0):
                payload = b"".join([chunk async for chunk in stream])
                captured.append((snapshot_id, payload))
                if not backend["ready"]:
                    raise SnapshotTransientFailure("backend unavailable")
                return PersistReceipt(
                    ref, snapshot_id, lease.epoch, 2, "receipt", hashlib.sha256(payload).hexdigest(),
                    len(payload), 1,
                )

        control.storage = Storage()
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)

        async def close(ref):
            try:
                await lifecycle.commit_workspace(endpoint)
            except SnapshotTransientFailure as exc:
                control.manager._active[key] = endpoint
                control.manager._errors[key] = exc
                raise
            control.manager._active.pop(key, None)
            control.manager._occupied.discard(key)
            control.manager._errors.pop(key, None)
            control.manager.closed.append(ref)

        control.manager.close = close
        async def mini_json(observed, method, path, payload=None):
            if path == "/healthz":
                return 200, {"status": "ok"}
            if path == "/sessions":
                return 200, []
            raise AssertionError(path)

        control._mini_json = mini_json
        async def mini(request):
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, content=b"complete archive")

        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        now = [100.0]
        sleeps = []
        monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: now[0])

        async def advance(delay):
            sleeps.append(delay)
            now[0] += delay

        monkeypatch.setattr(lifecycle_module.asyncio, "sleep", advance)
        start = datetime(2026, 10, 2, tzinfo=UTC)
        await lifecycle.tick(start)
        assert sleeps == [1, 2, 2]
        assert len(captured) == 3 and len({sid for sid, _ in captured}) == 1
        assert key in control.manager._occupied and key in control.manager._errors
        assert control.manager._active[key] == endpoint
        assert control.manager.closed == []

        for i in range(1, 30):
            now[0] += 1
            await lifecycle.tick(start + timedelta(seconds=i))
        assert len(captured) == 3

        backend["ready"] = True
        now[0] = 135.0  # exhaustion at 105 plus the explicit 30 second cooldown
        await lifecycle.tick(start + timedelta(seconds=30))
        assert len(captured) == 4
        assert captured[3][0] != captured[0][0]
        assert all(payload == b"complete archive" for _, payload in captured)
        assert control.manager.closed == [endpoint.ref]
        assert key not in control.manager._occupied and key not in control.manager._errors
        assert key not in lifecycle._failed_workspace_saves
        await control.mini_client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["writer", "snapshot", "concurrent", "restore", "unavailable", "receipt", "uid"])
def test_close_permanent_errors_are_not_retried(monkeypatch, failure) -> None:
    import hashlib
    import httpx
    import ach_runtime.harness.lifecycle as lifecycle_module
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import (
        ConcurrentModification, PersistReceipt, RestoreFailed, SnapshotConflict,
        StorageUnavailable, WriterFenced, WriterLease,
    )

    async def scenario():
        control = _Control()
        control.config.workspace.persistence.enabled = True
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
        )
        error_types = {
            "writer": WriterFenced, "snapshot": SnapshotConflict,
            "concurrent": ConcurrentModification, "restore": RestoreFailed,
            "unavailable": StorageUnavailable,
        }
        calls = {"persist": 0}

        class Storage:
            async def acquire_writer(self, ref, pod):
                return WriterLease(ref, pod, 2)

            async def restore(self, ref, *, retention_days=0):
                return None

            async def persist(self, ref, snapshot_id, stream, lease, base, *, retention_days=0):
                calls["persist"] += 1
                payload = b"".join([chunk async for chunk in stream])
                if failure in error_types:
                    raise error_types[failure]("permanent close error")
                return PersistReceipt(
                    ref, snapshot_id, lease.epoch + 1, 2, "bad", hashlib.sha256(payload).hexdigest(),
                    len(payload), 1,
                )

        control.storage = Storage()
        header_uid = "wrong-pod" if failure == "uid" else "pod"
        async def mini(request):
            return httpx.Response(200, headers={"X-ACH-Instance-Id": header_uid}, content=b"archive")

        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda observed: {"X-ACH-Instance-Id": observed.instance_id}
        sleeps = []
        async def fake_sleep(delay):
            sleeps.append(delay)
        monkeypatch.setattr(lifecycle_module.asyncio, "sleep", fake_sleep)
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)
        if failure == "uid":
            from ach_runtime.harness.workspaces import InstanceMismatch

            with pytest.raises(InstanceMismatch):
                await lifecycle.commit_workspace(endpoint)
            assert calls["persist"] == 0
            assert control.manager.lost == [endpoint], "a foreign export is a loss, reported once"
        elif failure == "receipt":
            with pytest.raises(RuntimeError, match="invalid close receipt"):
                await lifecycle.commit_workspace(endpoint)
            assert calls["persist"] == 1
        else:
            with pytest.raises(error_types[failure], match="permanent close error"):
                await lifecycle.commit_workspace(endpoint)
            assert calls["persist"] == 1
        assert sleeps == []
        await control.mini_client.aclose()

    asyncio.run(scenario())


def test_close_cancellation_drains_export_and_cleans_attempt_staging() -> None:
    """I2: cancelling a close attempt mid-export must drain the owned write worker to
    completion, leave no `ach-close-*` staging file behind, and never reach persist."""
    import glob
    import tempfile
    import httpx
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import WriterLease

    class Stalling(httpx.AsyncByteStream):
        def __init__(self, released: asyncio.Event) -> None:
            self._released = released

        async def __aiter__(self):
            yield b"partial-chunk"
            await self._released.wait()
            yield b"more-than-cancellation-should-ever-see"

        async def aclose(self) -> None:
            return None

    async def scenario():
        control = _Control()
        control.config.workspace.persistence.enabled = True
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
        )
        persisted = []

        class Storage:
            async def acquire_writer(self, ref, pod):
                return WriterLease(ref, pod, 1)

            async def restore(self, ref, *, retention_days=0):
                return None

            async def persist(self, ref, snapshot_id, stream, lease, base, *, retention_days=0):
                persisted.append(snapshot_id)
                raise AssertionError("persist must not be reached while export is cancelled")

        control.storage = Storage()
        released = asyncio.Event()
        streaming_started = asyncio.Event()

        async def mini(request):
            streaming_started.set()
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, stream=Stalling(released))

        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda observed: {"X-ACH-Instance-Id": observed.instance_id}
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)

        before_spools = set(glob.glob(tempfile.gettempdir() + "/ach-close-*"))
        task = asyncio.create_task(lifecycle.commit_workspace(endpoint))
        await asyncio.wait_for(streaming_started.wait(), timeout=2)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert persisted == []
        after_spools = set(glob.glob(tempfile.gettempdir() + "/ach-close-*"))
        assert after_spools == before_spools
        released.set()
        await control.mini_client.aclose()

    asyncio.run(scenario())


def test_failed_stopped_session_close_retries_after_cooldown(monkeypatch) -> None:
    import ach_runtime.harness.lifecycle as lifecycle_module
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import SnapshotTransientFailure

    async def scenario():
        control = _Control()
        control.config.workspace.session.persistence.enabled = True
        control.config.workspace.idle_timeout_seconds = 1000
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
        )
        control.manager._active = {endpoint.ref.workspace_ref: endpoint}
        control.delivery = _Delivery()
        async def mini_json(observed, method, path, payload=None):
            if path == "/healthz":
                return 200, {"status": "ok"}
            if path == "/sessions":
                return 200, [{"sessionRef": "b" * 64}]
            return 200, {"active": False, "initialized": True, "nativeSessionId": "sid"}

        control._mini_json = mini_json
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)
        attempts = []
        async def save(_endpoint, session_ref):
            attempts.append(session_ref)
            if len(attempts) == 1:
                raise SnapshotTransientFailure("temporary close failure")

        lifecycle._save_session = save
        mono = [20.0]
        monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: mono[0])
        start = datetime(2026, 10, 2, tzinfo=UTC)
        await lifecycle.tick(start)
        assert attempts == ["b" * 64]
        mono[0] = 49.9
        await lifecycle.tick(start + timedelta(seconds=1))
        assert attempts == ["b" * 64]
        mono[0] = 50.0
        await lifecycle.tick(start + timedelta(seconds=30))
        assert attempts == ["b" * 64, "b" * 64]
        assert (endpoint.ref.workspace_ref, "b" * 64, endpoint.instance_id) not in lifecycle._failed_session_saves
        assert endpoint.ref.workspace_ref not in lifecycle._failed_workspace_saves

    asyncio.run(scenario())


def test_failed_save_retains_home_pod_slot() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        alarms = []
        control = _Control()
        control.config.workspace.session.persistence.enabled = True
        control.manager.closed = []
        control.manager.close = _close_record(control.manager)
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a"*64), "ws", "pod", "http://mini")
        control.manager._active = {"a"*64: endpoint}
        control.delivery = _Delivery()
        async def mini(request):
            if request.url.path == "/sessions":
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json=[{"sessionRef": "b"*64}])
            if request.url.path.endswith("/info"):
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json={"active": True, "uncertain": False})
            raise AssertionError(request.url)
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control, alarm=alarms.append)
        async def failed(endpoint, session_ref):
            raise RuntimeError("archive failed")
        lifecycle._save_session = failed
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        await lifecycle.tick(start + timedelta(seconds=21))
        await lifecycle.tick(start + timedelta(seconds=90))
        assert alarms == ["Harness session persistence failed"]
        assert control.manager._active["a"*64] is endpoint
        assert control.manager.closed == []
        await control.mini_client.aclose()
    asyncio.run(scenario())


def test_uncertain_cleanup_blocks_workspace_idle_close() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        control.manager.closed = []
        control.manager.close = _close_record(control.manager)
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a"*64), "ws", "pod", "http://mini")
        control.manager._active = {"a"*64: endpoint}
        control.delivery = _Delivery()
        async def mini(request):
            if request.url.path == "/sessions":
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json=[{"sessionRef": "b"*64}])
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json={"active": False, "uncertain": True})
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control)
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        await lifecycle.tick(start + timedelta(seconds=300))
        assert control.manager.closed == []
        await control.mini_client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reason",
    [
        "Harness session archive export failed",
        "Harness session retire failed",
        "restore_failed",
    ],
)
def test_non_persistence_failure_keeps_alarm_marker_without_diagnostic(caplog, reason) -> None:
    import logging

    import ach_runtime.harness.lifecycle as lifecycle_module

    alarms = []
    lifecycle = Lifecycle(_Control(), alarm=alarms.append)
    error = RuntimeError("unrelated operation failure")

    with caplog.at_level(logging.ERROR, logger=lifecycle_module.__name__):
        lifecycle._report_failure(error, reason)

    assert alarms == [reason]
    assert getattr(error, "_ach_anomaly_reported", False) is True
    assert not [
        record for record in caplog.records
        if record.name == lifecycle_module.__name__ and "error_type=" in record.getMessage()
    ]


@pytest.mark.parametrize("domain", ["workspace", "session"])
async def test_archive_persistence_failure_logs_redacted_root_cause(
    monkeypatch, capsys, domain: str
) -> None:
    import logging

    import httpx
    import ach_runtime.harness.lifecycle as lifecycle_module
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL
    from ach_runtime.observability.runs import configure_logging
    from ach_runtime.storage.api import SnapshotTransientFailure, WriterLease

    control = _Control()
    if domain == "workspace":
        control.config.workspace.persistence.enabled = True
    else:
        control.config.workspace.session.persistence.enabled = True
    endpoint = WorkspaceEndpoint(
        WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
        "ws", "pod", "http://mini",
    )
    message = (
        "[Errno 13] Permission denied https://user:hunter2@pod.local/x "
        "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz0123456789"
    )
    calls = {"get": 0, "persist": 0}

    class Storage:
        async def acquire_writer(self, ref, writer_instance_id):
            return WriterLease(ref, writer_instance_id, 1)

        async def persist(self, *args, **kwargs):
            calls["persist"] += 1
            raise AssertionError("archive export exhaustion must precede persistence")

    control.storage = Storage()

    async def mini(request):
        calls["get"] += 1
        assert request.method == "GET"
        return httpx.Response(
            502,
            headers={"X-ACH-Instance-Id": "pod"},
            json={"type": "Failed", "message": message},
        )

    control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
    control._headers = lambda observed: {"X-ACH-Instance-Id": observed.instance_id}
    lifecycle = Lifecycle(control)
    now = [100.0]
    delays = []
    monkeypatch.setattr(lifecycle_module.time, "monotonic", lambda: now[0])

    async def fake_sleep(delay):
        delays.append(delay)
        now[0] += delay

    monkeypatch.setattr(lifecycle_module.asyncio, "sleep", fake_sleep)
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    for handler in saved_handlers:
        root.removeHandler(handler)
    before = ANOMALIES_TOTAL.labels(kind="persistence_failed")._value.get()
    try:
        configure_logging()
        capsys.readouterr()
        with pytest.raises(SnapshotTransientFailure) as raised:
            if domain == "workspace":
                await lifecycle.commit_workspace(endpoint)
            else:
                await lifecycle._save_session(endpoint, "b" * 64)
    finally:
        for handler in saved_handlers:
            root.addHandler(handler)
        await control.mini_client.aclose()

    error = raised.value
    assert isinstance(error.__cause__, httpx.HTTPStatusError)
    assert "Permission denied" in str(error.__cause__)
    assert delays == [1, 2, 2]
    assert calls == {"get": 3, "persist": 0}
    assert getattr(error, "_ach_anomaly_reported", False) is True
    assert ANOMALIES_TOTAL.labels(kind="persistence_failed")._value.get() == before + 1

    from ach_runtime.harness.control import _failure_reason

    cause_text = _failure_reason(error.__cause__, limit=300)
    assert len(cause_text) <= 303
    stderr = capsys.readouterr().err
    expected_reason = (
        "Harness Workspace persistence failed" if domain == "workspace"
        else "Harness session persistence failed"
    )
    assert expected_reason in stderr
    assert "error_type=SnapshotTransientFailure" in stderr
    assert "HTTPStatusError" in stderr
    assert "Permission denied" in stderr
    assert "hunter2" not in stderr
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in stderr


def test_workspace_close_is_rechecked_against_arriving_admission() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        control.manager.closed = []
        entered = asyncio.Event()
        release = asyncio.Event()
        async def close(ref):
            entered.set()
            await release.wait()
            if control.admission.has_work(ref.workspace_ref):
                return
            control.manager.closed.append(ref)
        control.manager.close = close
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a"*64), "ws", "pod", "http://mini")
        control.manager._active = {"a"*64: endpoint}
        control.delivery = _Delivery()
        async def mini(request):
            if request.url.path == "/sessions":
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json=[])
            raise AssertionError(request.url)
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control)
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        closing = asyncio.create_task(lifecycle.tick(start + timedelta(seconds=31)))
        await entered.wait()
        control.admission.tickets.append(type("Ticket", (), {"workspace_ref": "a"*64, "session_ref": "c"*64})())
        release.set()
        await closing
        assert control.manager.closed == []
        await control.mini_client.aclose()
    asyncio.run(scenario())


def test_workspace_first_lazy_session_restore() -> None:
    import httpx
    from ach_runtime.storage.api import RestoreReceipt, WriterLease
    from ach_runtime.storage.refs import DOMAIN_SESSION
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        calls = []
        control = _Control()
        control.config.workspace.persistence.enabled = True
        control.config.workspace.session.persistence.enabled = True
        class Storage:
            async def restore(self, ref, *, retention_days=0):
                calls.append(("storage-restore", ref.domain, ref.session_ref))
                if ref.session_ref is None:
                    async def opened():
                        yield b"workspace"
                    return RestoreReceipt(ref, "workspace-snapshot", 2, 1, "workspace-etag",
                                          "sum", 9, 1, opened)
                return None
            async def acquire_writer(self, ref, pod):
                calls.append(("writer", ref.domain, ref.session_ref, pod))
                base_etag = "workspace-etag" if ref.session_ref is None else None
                return WriterLease(ref, pod, 1, base_etag)
        control.storage = Storage()
        async def mini(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/workspace/archive":
                if request.method == "GET":
                    return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, content=b"workspace")
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json={"status": "restored"})
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json={"status": "restored"})
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a"*64), "ws", "pod", "http://mini")
        lifecycle = Lifecycle(control)
        await lifecycle.restore_workspace(endpoint)
        control._mini_json = _mini_json(control)
        ticket = type("Ticket", (), {"session_ref": "b"*64})()
        await lifecycle.restore_session(ticket, endpoint)
        put_position = next(i for i, call in enumerate(calls) if call == ("PUT", "/workspace/archive"))
        info_position = next(i for i, call in enumerate(calls) if call == ("GET", f"/sessions/{ticket.session_ref}/info"))
        assert put_position < info_position
        assert ("storage-restore", DOMAIN_SESSION, ticket.session_ref) in calls
        assert not any(call[0] == "PUT" and call[1].startswith("/sessions/") for call in calls)
        assert all(call[-1] == ticket.session_ref for call in calls
                   if call[0] == "storage-restore" and call[1] == DOMAIN_SESSION)
        await control.mini_client.aclose()
    asyncio.run(scenario())


def test_tick_workspaces_progress_independently() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        endpoint_a = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
                                       "ws-a", "pod-a", "http://mini")
        endpoint_b = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "b" * 64),
                                       "ws-b", "pod-b", "http://mini")
        control.manager._active = {"a" * 64: endpoint_a, "b" * 64: endpoint_b}
        control.delivery = _Delivery()
        inventory_b = asyncio.Event()
        save_a = asyncio.Event()
        release_save = asyncio.Event()
        async def mini(request):
            if request.url.path == "/sessions":
                if request.headers["x-ach-instance-id"] == "pod-b":
                    inventory_b.set()
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[{"sessionRef": "c" * 64}])
            return httpx.Response(200, json={"active": True, "uncertain": False})
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control)
        async def save(endpoint, session_ref):
            save_a.set()
            await release_save.wait()
        lifecycle._save_session = save
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        inventory_b.clear()
        tick = asyncio.create_task(lifecycle.tick(start + timedelta(seconds=21)))
        prepare = None
        try:
            await asyncio.wait_for(save_a.wait(), timeout=2)
            await asyncio.wait_for(inventory_b.wait(), timeout=2)
            entered_prepare = asyncio.Event()
            async def prepare_a():
                async with lifecycle.operation("a" * 64):
                    entered_prepare.set()
            prepare = asyncio.create_task(prepare_a())
            await asyncio.sleep(0)
            assert not entered_prepare.is_set()
            release_save.set()
            await asyncio.wait_for(tick, timeout=2)
            await asyncio.wait_for(prepare, timeout=2)
            assert entered_prepare.is_set()
        finally:
            release_save.set()
            if prepare is not None:
                await asyncio.gather(prepare, return_exceptions=True)
            await asyncio.gather(tick, return_exceptions=True)
            await lifecycle.close()
            await control.mini_client.aclose()
    asyncio.run(scenario())


def test_tick_drains_workspace_tasks_on_error_and_close() -> None:
    import httpx
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        endpoint_a = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
                                       "ws-a", "pod-a", "http://mini")
        endpoint_b = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "b" * 64),
                                       "ws-b", "pod-b", "http://mini")
        control.manager._active = {"a" * 64: endpoint_a, "b" * 64: endpoint_b}
        control.delivery = _Delivery()
        release_b = asyncio.Event()
        b_entered = asyncio.Event()
        async def mini(request):
            if request.url.path == "/sessions" and request.headers["x-ach-instance-id"] == "pod-a":
                return httpx.Response(200, json={"invalid": True})
            if request.url.path == "/sessions":
                b_entered.set()
                await release_b.wait()
                return httpx.Response(200, json=[])
            raise AssertionError(request.url)
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control)
        task = asyncio.create_task(lifecycle.tick(datetime(2026, 10, 1, tzinfo=UTC)))
        closing = None
        try:
            await asyncio.wait_for(b_entered.wait(), timeout=2)
            await asyncio.sleep(0)
            assert not task.done()
            release_b.set()
            with pytest.raises(RuntimeError, match="invalid session inventory"):
                await asyncio.wait_for(task, timeout=2)

            release_b.clear()
            b_entered.clear()
            lifecycle._closed = False
            await lifecycle.start()
            await asyncio.wait_for(b_entered.wait(), timeout=2)
            closing = asyncio.create_task(lifecycle.close())
            await asyncio.sleep(0)
            assert not closing.done()
            assert not control.mini_client.is_closed
            release_b.set()
            await asyncio.wait_for(closing, timeout=2)
            assert control.mini_client.is_closed is False
        finally:
            release_b.set()
            await asyncio.gather(task, return_exceptions=True)
            if closing is not None:
                await asyncio.gather(closing, return_exceptions=True)
            await lifecycle.close()
            await control.mini_client.aclose()
    asyncio.run(scenario())


def test_prepare_holding_workspace_does_not_block_tick_for_another_workspace() -> None:
    import httpx
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        endpoint_a = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
                                       "ws-a", "pod-a", "http://mini")
        endpoint_b = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "b" * 64),
                                       "ws-b", "pod-b", "http://mini")
        control.manager._active = {"a" * 64: endpoint_a, "b" * 64: endpoint_b}
        control.delivery = _Delivery()
        inventory_b = asyncio.Event()
        async def mini(request):
            if request.url.path == "/sessions":
                if request.headers["x-ach-instance-id"] == "pod-b":
                    inventory_b.set()
                return httpx.Response(200, json=[])
            raise AssertionError(request.url)
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control)
        tick = None
        start = datetime(2026, 10, 1, tzinfo=UTC)
        try:
            async with lifecycle.operation("a" * 64):
                tick = asyncio.create_task(lifecycle.tick(start))
                await asyncio.wait_for(inventory_b.wait(), timeout=2)
                assert not tick.done()
            await asyncio.wait_for(tick, timeout=2)
        finally:
            if tick is not None:
                await asyncio.gather(tick, return_exceptions=True)
            await lifecycle.close()
            await control.mini_client.aclose()
    asyncio.run(scenario())


def test_recreated_lifecycle_retries_failed_inactive_session() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        alarms = []
        control = _Control()
        control.config.workspace.session.persistence.enabled = True
        control.manager.closed = []
        control.manager.close = _close_record(control.manager)
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
                                     "ws", "pod", "http://mini")
        control.manager._active = {"a" * 64: endpoint}
        control.delivery = _Delivery()
        async def mini(request):
            if request.url.path == "/sessions":
                return httpx.Response(200, json=[{"sessionRef": "b" * 64}])
            return httpx.Response(200, json={"initialized": True, "nativeSessionId": "native-1",
                                             "active": False, "uncertain": False})
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        start = datetime(2026, 10, 1, tzinfo=UTC)

        first = Lifecycle(control, alarm=alarms.append)
        calls = []
        async def fail(endpoint, session_ref):
            calls.append("failed")
            raise RuntimeError("Storage persist failed after mini suspension")
        first._save_session = fail
        await first.tick(start)
        await first.tick(start + timedelta(seconds=60))
        assert calls == ["failed"]
        assert control.manager._active["a" * 64] is endpoint
        assert control.manager.closed == []

        restarted = Lifecycle(control, alarm=alarms.append)
        second_calls = []
        async def fail_again(endpoint, session_ref):
            second_calls.append("failed")
            raise RuntimeError("Storage persist still failed")
        restarted._save_session = fail_again
        await restarted.tick(start + timedelta(seconds=120))
        await restarted.tick(start + timedelta(seconds=1000))
        assert second_calls == ["failed"]
        assert control.manager._active["a" * 64] is endpoint
        assert control.manager.closed == []

        assert alarms == ["Harness session persistence failed", "Harness session persistence failed"]
        assert control.manager.closed == []
        await control.mini_client.aclose()
    asyncio.run(scenario())


async def test_restart_after_export_persist_failure_requires_session_receipt(tmp_path: Path) -> None:
    import io
    import hashlib
    import json
    import tarfile
    import httpx
    from datetime import timedelta
    from types import SimpleNamespace

    from ach_runtime.config.public import public_config
    from ach_runtime.config.schema import parse_config
    from ach_runtime.engine.opencode import OpenCodeEngine, _NativeSession
    from ach_runtime.execution.service import ExecutionService
    from ach_runtime.execution.sessions import SESSION_STARTED_MARKER, SessionRegistry
    from ach_runtime.execution.snapshots import export_session
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import PersistReceipt, RestoreReceipt, WriterLease

    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    config = parse_config(wire)
    public = public_config(config, config.engine.env)

    workspace_ref = "a" * 64
    session_ref = "b" * 64
    native_sid = "native-retained-session"
    engine = OpenCodeEngine()
    service = ExecutionService(
        engine=engine,
        sessions=SessionRegistry(tmp_path / "mini" / "sessions"),
        workspace_dir=tmp_path / "mini" / "workspace",
        workspace_ref=workspace_ref,
    )
    service.configure(public)
    state = service._sessions.get_or_create(session_ref)
    state.native_session_id = native_sid
    (state.home / SESSION_STARTED_MARKER).touch()
    (state.home / "home-sentinel").write_text("retained after suspension", encoding="utf-8")
    native = _NativeSession(session_ref, state.home, state.scratch)
    native.sid = native_sid
    native._proc = SimpleNamespace(returncode=None)
    engine._sessions[session_ref] = native
    original_stop_cleanly = native.stop_cleanly
    fake_clean_stops = []

    async def first_clean_stop(*, timeout_seconds: float = 5.0) -> bool:
        if native.alive:
            fake_clean_stops.append(timeout_seconds)
            native._proc.returncode = 0
            native._confirmed_halted = True
            native.clean_exit = True
            return True
        return await original_stop_cleanly(timeout_seconds=timeout_seconds)

    native.stop_cleanly = first_clean_stop

    class Storage:
        def __init__(self) -> None:
            self.persist_calls = 0
            self.archives: list[bytes] = []
            self.expected_bases: list[str | None] = []
            self.retention_policies: list[int] = []
            self.latest_etag: str | None = None

        async def restore(self, ref, *, retention_days=0):
            if self.latest_etag is None:
                return None

            async def opened():
                yield b"stored snapshot"

            return RestoreReceipt(
                ref, "stored-snapshot", 7, 1, self.latest_etag, "checksum", 15, 1, opened
            )

        async def acquire_writer(self, ref, writer_instance_id):
            return WriterLease(ref, writer_instance_id, 8)

        async def persist(self, ref, snapshot_id, stream, lease, expected_base, *, retention_days=0):
            payload = b"".join([chunk async for chunk in stream])
            self.archives.append(payload)
            self.expected_bases.append(expected_base)
            self.retention_policies.append(retention_days)
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                members = {Path(member.name).name: member for member in archive.getmembers()}
                assert "home-sentinel" in members
                registry_member = members[".ach-registry.json"]
                registry = json.loads(archive.extractfile(registry_member).read())
                assert registry["nativeSessionId"] == native_sid
            self.persist_calls += 1
            if self.persist_calls == 1:
                raise RuntimeError("Storage failed after consuming the suspended HOME archive")
            if self.persist_calls == 2:
                return PersistReceipt(
                    ref, snapshot_id, lease.epoch + 1, 2, "invalid-etag",
                    hashlib.sha256(payload).hexdigest(), len(payload), 1,
                )
            receipt = PersistReceipt(
                ref, snapshot_id, lease.epoch, 3, "session-etag-3", hashlib.sha256(payload).hexdigest(), len(payload), 1
            )
            self.latest_etag = receipt.etag
            return receipt

    storage = Storage()
    control = _Control()
    control.config.workspace.session.persistence.enabled = True
    control.config.workspace.session.persistence.retention_days = config.workspace.session.persistence.retention_days
    control.storage = storage
    control.manager.closed = []
    control.manager.close = _close_record(control.manager)
    endpoint = WorkspaceEndpoint(
        WorkspaceRef("ach", control.config.agent.uid, workspace_ref),
        "workspace", "pod-1", "http://mini",
    )
    control.manager._active = {workspace_ref: endpoint}
    control.delivery = _Delivery()
    alarms = []
    exported_archives = []

    async def mini(request):
        if request.url.path == "/sessions":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": endpoint.instance_id},
                                  json=[{"sessionRef": s.session_ref}
                                        for s in service._sessions.inventory()])
        if request.url.path.endswith("/info"):
            return httpx.Response(200, headers={"X-ACH-Instance-Id": endpoint.instance_id},
                                  json=await service.session_info(session_ref))
        if request.url.path.endswith("/archive"):
            path = await export_session(service, session_ref)
            exported_archives.append(path)
            try:
                return httpx.Response(200, headers={"X-ACH-Instance-Id": endpoint.instance_id},
                                      content=path.read_bytes())
            finally:
                path.unlink(missing_ok=True)
        if request.url.path.endswith("/retire"):
            await service.retire_session(session_ref)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": endpoint.instance_id},
                                  json={"status": "retired", "sessionRef": session_ref})
        raise AssertionError(request.url)

    control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
    control._headers = lambda observed: {"X-ACH-Instance-Id": observed.instance_id}
    control._mini_json = _mini_json(control)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    first = Lifecycle(control, alarm=alarms.append)
    try:
        await first.tick(start)
        await first.tick(start + timedelta(seconds=21))
        assert storage.persist_calls == 1
        assert storage.retention_policies == [config.workspace.session.persistence.retention_days]
        assert fake_clean_stops == [5.0]
        assert native.alive is False and native.clean_exit is True
        assert control.manager._active[workspace_ref] is endpoint
        assert control.manager.closed == []
        assert storage.archives[0]

        restarted = Lifecycle(control, alarm=alarms.append)
        await restarted.tick(start + timedelta(seconds=120))
        await restarted.tick(start + timedelta(seconds=1000))
        assert storage.persist_calls == 2
        assert len(storage.archives) == 2
        assert native.alive is False and native.clean_exit is True
        assert fake_clean_stops == [5.0]
        assert control.manager._active[workspace_ref] is endpoint
        assert control.manager.closed == []

        receipt_lifecycle = Lifecycle(control, alarm=alarms.append)
        await receipt_lifecycle.tick(start + timedelta(seconds=2000))
        assert storage.persist_calls == 3
        assert receipt_lifecycle._bases[(workspace_ref, session_ref)] == "session-etag-3"
        assert control.manager._active[workspace_ref] is endpoint
        await receipt_lifecycle.tick(start + timedelta(seconds=2031))
        assert control.manager.closed == [endpoint.ref]

        assert len(exported_archives) == 3
        assert all(not path.exists() for path in exported_archives)
        assert storage.expected_bases == [None, None, None]
        assert alarms == ["Harness session persistence failed", "Harness session persistence failed"]
        assert service._invocations == {}
        assert native._invocations == {}
    finally:
        await control.mini_client.aclose()
        await service.close()


async def test_same_pod_idle_close_reopen_uses_restore_or_new_sid(tmp_path: Path, monkeypatch) -> None:
    """I6, real ExecutionService + authenticated ASGI app + fake native adapter.

    Enabled-mode close+reopen imports the committed HOME and keeps the committed SID
    (one Restore, no fresh native Start); disabled-mode close+reopen starts a fresh
    native session into a brand-new HOME. A bystander session on the same pod is left
    completely alone throughout. A failed close never retires (HOME/registry survive,
    zero retire calls)."""
    import hashlib
    import json as json_module
    import httpx
    from datetime import timedelta, timezone
    from types import SimpleNamespace

    from ach_runtime.config.public import public_config
    from ach_runtime.config.schema import parse_config
    from ach_runtime.engine.opencode import OpenCodeEngine, _NativeSession
    from ach_runtime.execution.auth import create_authenticated_app
    from ach_runtime.execution.service import ExecutionService
    from ach_runtime.execution.sessions import SESSION_STARTED_MARKER, SessionRegistry
    from ach_runtime.execution.wire import StartRequest
    from ach_runtime.harness.tokens import engine_bearer, engine_verify_key
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.api import PersistReceipt, RestoreReceipt, WriterLease

    def _sref(name: str) -> str:
        return hashlib.sha256(name.encode()).hexdigest()

    wire = json_module.loads(
        (Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text()
    )
    config = parse_config(wire)
    public = public_config(config, config.engine.env)
    key = b"k" * 64
    workspace_name, instance_id = "ws-reopen", "pod-reopen"

    async def fake_ensure_started(native, *, env, config_path, deadline=None):
        return None

    async def fake_stop_cleanly(native, *, timeout_seconds=5.0):
        if native.alive:
            native._proc.returncode = 0
            native._confirmed_halted = True
            native.clean_exit = True
        return True

    fresh_sid_counter = {"n": 0}
    fresh_session_data: dict[str, dict] = {}

    async def fake_request(native, method, path, **kwargs):
        json_body = kwargs.get("json_body")
        if method == "GET" and path == "/api/config":
            return 200, []
        if method == "GET" and path == "/api/model":
            return 200, {"data": [{"providerID": public.model.type, "id": public.model.name,
                                   "enabled": True}]}
        if method == "GET" and path.startswith("/api/agent/"):
            agent_name = path.rsplit("/", 1)[-1]
            return 200, {"data": {"id": agent_name, "steps": public.limits.max_steps,
                                  "system": "managed system prompt"}}
        if method == "POST" and path == "/api/session":
            fresh_sid_counter["n"] += 1
            sid = f"fresh-sid-{fresh_sid_counter['n']}"
            fresh_session_data[sid] = {
                "location": json_body["location"], "model": json_body["model"],
                "agent": json_body["agent"],
            }
            return 200, {"data": {"id": sid}}
        if method == "GET" and path.startswith("/api/session/"):
            sid = path.rsplit("/", 1)[-1]
            saved = fresh_session_data.get(sid, {
                "location": {"directory": ""},
                "model": {"providerID": public.model.type, "id": public.model.name},
                "agent": "build",
            })
            return 200, {"data": {"id": sid, **saved}}
        if method == "POST" and path.endswith("/prompt"):
            sid = path.split("/")[3]
            return 200, {"data": {
                "id": f"input-{sid}", "sessionID": sid, "delivery": json_body["delivery"],
                "type": "user", "payload": {"text": json_body["text"]},
            }}
        raise AssertionError((method, path, json_body))

    monkeypatch.setattr(_NativeSession, "ensure_started", fake_ensure_started)
    monkeypatch.setattr(_NativeSession, "stop_cleanly", fake_stop_cleanly)
    monkeypatch.setattr(_NativeSession, "request", fake_request)

    def setup(tag: str, *, persistence_enabled: bool):
        engine = OpenCodeEngine()
        service = ExecutionService(
            engine=engine,
            sessions=SessionRegistry(tmp_path / f"sessions-{tag}"),
            workspace_dir=tmp_path / f"workspace-{tag}",
            workspace_ref=workspace_name,
        )
        service.configure(public)
        app = create_authenticated_app(
            service, verify_key=engine_verify_key(key), workspace_name=workspace_name,
            instance_id=instance_id,
        )
        control = _Control()
        control.config.workspace.session.persistence.enabled = persistence_enabled
        control.config.workspace.session.persistence.retention_days = 7
        control.config.workspace.idle_timeout_seconds = 10_000
        control.config.workspace.session.idle_timeout_seconds = 20
        control.manager.closed = []
        control.manager.close = _close_record(control.manager)
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
            workspace_name, instance_id, "http://mini",
        )
        control.manager._active = {"a" * 64: endpoint}
        control.delivery = _Delivery()
        control.mini_client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mini"
        )
        control._headers = lambda observed: {
            "Authorization": f"Bearer {engine_bearer(key, workspace_name)}",
            "X-ACH-Instance-Id": observed.instance_id,
        }
        control._mini_json = _mini_json(control)
        return service, control, endpoint

    class Storage:
        def __init__(self):
            self.by_ref: dict[tuple[str, str], dict] = {}
            self.fail_next: set[tuple[str, str]] = set()

        async def acquire_writer(self, ref, writer_instance_id):
            return WriterLease(ref, writer_instance_id, 1)

        async def restore(self, ref, *, retention_days=0):
            saved = self.by_ref.get((ref.workspace_ref, ref.session_ref))
            if saved is None:
                return None

            async def opened():
                yield saved["bytes"]

            return RestoreReceipt(ref, saved["snapshot_id"], 1, 1, saved["etag"], "checksum",
                                  len(saved["bytes"]), 1, opened)

        async def persist(self, ref, snapshot_id, stream, lease, expected_base, *, retention_days=0):
            payload = b"".join([chunk async for chunk in stream])
            key_ = (ref.workspace_ref, ref.session_ref)
            if key_ in self.fail_next:
                self.fail_next.discard(key_)
                raise RuntimeError("permanent backend rejection")
            etag = f"etag-{snapshot_id}"
            self.by_ref[key_] = {"bytes": payload, "snapshot_id": snapshot_id, "etag": etag}
            return PersistReceipt(ref, snapshot_id, lease.epoch, 2, etag,
                                  hashlib.sha256(payload).hexdigest(), len(payload), 1)

    def add_bystander(service, control, session_ref: str) -> _NativeSession:
        state = service._sessions.get_or_create(session_ref)
        (state.home / SESSION_STARTED_MARKER).touch()
        (state.home / "bystander-marker").write_text("untouched", encoding="utf-8")
        native = _NativeSession(session_ref, state.home, state.scratch)
        native.sid = "bystander-sid"
        native._proc = SimpleNamespace(returncode=None)
        service._engine._sessions[session_ref] = native
        control.admission.tickets.append(
            SimpleNamespace(workspace_ref="a" * 64, session_ref=session_ref)
        )
        return native

    def add_target(service, session_ref: str, sid: str) -> _NativeSession:
        state = service._sessions.get_or_create(session_ref)
        (state.home / SESSION_STARTED_MARKER).touch()
        native = _NativeSession(session_ref, state.home, state.scratch)
        native.sid = sid
        native._proc = SimpleNamespace(returncode=None)
        service._engine._sessions[session_ref] = native
        return native

    async def close_target(control, endpoint, target_ref: str) -> None:
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        await lifecycle.tick(start + timedelta(seconds=21))
        await lifecycle.close()

    # --- Enabled mode: close imports/commits, reopen restores the committed SID ---
    storage = Storage()
    service_en, control_en, endpoint_en = setup("enabled", persistence_enabled=True)
    control_en.storage = storage
    bystander = add_bystander(service_en, control_en, _sref("bystander-enabled"))
    add_target(service_en, _sref("target-enabled"), "committed-sid")

    await close_target(control_en, endpoint_en, _sref("target-enabled"))

    assert service_en._sessions.get(_sref("target-enabled")) is None
    assert service_en._engine._sessions[_sref("target-enabled")].sid is None
    assert storage.by_ref[("a" * 64, _sref("target-enabled"))]["bytes"]
    assert bystander.sid == "bystander-sid"
    bystander_state = service_en._sessions.get(_sref("bystander-enabled"))
    assert bystander_state is not None
    assert (bystander_state.home / "bystander-marker").read_text(encoding="utf-8") == "untouched"

    reopen_lifecycle = Lifecycle(control_en, alarm=lambda _reason: None)
    ticket = SimpleNamespace(
        session_ref=_sref("target-enabled"),
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )
    await reopen_lifecycle.restore_session(ticket, endpoint_en)
    await reopen_lifecycle.close()

    reopened_state = service_en._sessions.get(_sref("target-enabled"))
    assert reopened_state is not None and reopened_state.started is True
    assert reopened_state.native_session_id == "committed-sid"
    assert service_en._engine._sessions[_sref("target-enabled")].sid == "committed-sid"
    bystander_state = service_en._sessions.get(_sref("bystander-enabled"))
    assert bystander_state is not None and bystander_state.native_session_id is None
    assert bystander.sid == "bystander-sid"
    assert (bystander_state.home / "bystander-marker").read_text(encoding="utf-8") == "untouched"
    await control_en.mini_client.aclose()
    await service_en.close()

    # --- Disabled mode: close only drains/exports, reopen starts a fresh native SID ---
    service_dis, control_dis, endpoint_dis = setup("disabled", persistence_enabled=False)
    bystander_dis = add_bystander(service_dis, control_dis, _sref("bystander-disabled"))
    add_target(service_dis, _sref("target-disabled"), "original-sid")

    await close_target(control_dis, endpoint_dis, _sref("target-disabled"))

    assert service_dis._sessions.get(_sref("target-disabled")) is None
    assert service_dis._engine._sessions[_sref("target-disabled")].sid is None
    assert bystander_dis.sid == "bystander-sid"

    reopen_lifecycle_dis = Lifecycle(control_dis, alarm=lambda _reason: None)
    await reopen_lifecycle_dis.restore_session(
        SimpleNamespace(session_ref=_sref("target-disabled")), endpoint_dis
    )  # persistence disabled: a no-op, never touches storage/engine
    await reopen_lifecycle_dis.close()
    assert service_dis._sessions.get(_sref("target-disabled")) is None

    deadline = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    fresh_request = StartRequest(
        workspaceKey="wk", sessionKey="sk",
        invocationId="fresh-start", channel="webhook", workspaceRef=workspace_name,
        sessionRef=_sref("target-disabled"), configVersion=public.config_version,
        prompt="continue", deadlineAt=deadline,
    )
    await service_dis.start(fresh_request)

    fresh_state = service_dis._sessions.get(_sref("target-disabled"))
    assert fresh_state is not None
    assert service_dis._engine._sessions[_sref("target-disabled")].sid == f"fresh-sid-{fresh_sid_counter['n']}"
    assert service_dis._engine._sessions[_sref("target-disabled")].sid != "original-sid"
    assert bystander_dis.sid == "bystander-sid"
    await control_dis.mini_client.aclose()
    # The fresh "continue" invocation never settles under this fake (no event stream is
    # simulated) — nothing left to assert about it, so skip service_dis.close()'s graceful
    # shutdown wait rather than fake out native settlement for its own sake.
    inv = service_dis._invocations[fresh_request.invocation_id]
    if inv.deadline_task is not None:
        inv.deadline_task.cancel()
    if inv.settle_task is not None:
        inv.settle_task.cancel()
    await asyncio.gather(
        *(t for t in (inv.deadline_task, inv.settle_task) if t is not None),
        return_exceptions=True,
    )

    # --- A failed close never retires: HOME/registry survive, zero retire calls ---
    storage.fail_next.add(("a" * 64, _sref("target-failed")))
    service_fail, control_fail, endpoint_fail = setup("failed", persistence_enabled=True)
    control_fail.storage = storage
    add_target(service_fail, _sref("target-failed"), "will-not-retire-sid")
    retire_attempts: list[str] = []
    original_retire = service_fail.retire_session

    async def counting_retire(session_ref: str) -> None:
        retire_attempts.append(session_ref)
        await original_retire(session_ref)

    service_fail.retire_session = counting_retire

    lifecycle_fail = Lifecycle(control_fail, alarm=lambda _reason: None)
    start = datetime(2026, 10, 1, tzinfo=UTC)
    await lifecycle_fail.tick(start)
    await lifecycle_fail.tick(start + timedelta(seconds=21))
    await lifecycle_fail.close()

    assert retire_attempts == []
    failed_state = service_fail._sessions.get(_sref("target-failed"))
    assert failed_state is not None and failed_state.home.is_dir()
    assert service_fail._engine._sessions[_sref("target-failed")].sid == "will-not-retire-sid"
    await control_fail.mini_client.aclose()
    await service_fail.close()


@pytest.mark.parametrize("domain", ["workspace", "session"])
def test_live_writer_can_close_after_predecessor_expiry_and_harness_restart(domain) -> None:
    """A live Workspace/session must persist new state after a Harness restart even when
    its predecessor snapshot has since expired: the restarted Lifecycle's fresh writer
    lease carries the base it needs without ever restoring (and expiry-checking) the old
    payload. A genuine cold restore of that same expired predecessor must still reject."""
    import sys
    import hashlib
    import httpx
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
    from ach_runtime.storage.refs import DOMAIN_SESSION, DOMAIN_WORKSPACE, StateRef
    from ach_runtime.storage.s3 import S3Storage

    sys.path.insert(0, str(Path(__file__).parent.parent / "storage"))
    from fakes import FakeS3Client

    async def _archive_bytes(payload: bytes):
        yield payload

    async def scenario():
        client = FakeS3Client()
        now = [datetime(2026, 10, 1, tzinfo=UTC)]
        storage = S3Storage(bucket="b", prefix="pfx", client=client, clock=lambda: now[0])

        control = _Control()
        workspace_ref = "a" * 64
        session_ref = "b" * 64
        ref_domain = DOMAIN_WORKSPACE if domain == "workspace" else DOMAIN_SESSION
        ref = StateRef(
            agent=control.config.agent, workspace_ref=workspace_ref, domain=ref_domain,
            session_ref=None if domain == "workspace" else session_ref,
        )
        if domain == "workspace":
            control.config.workspace.persistence.enabled = True
            control.config.workspace.persistence.retention_days = 1
        else:
            control.config.workspace.session.persistence.enabled = True
            control.config.workspace.session.persistence.retention_days = 1
        control.storage = storage

        seed_lease = await storage.acquire_writer(ref, "pod-old")
        old_receipt = await storage.persist(ref, "snap-old", _archive_bytes(b"old-data"),
                                            seed_lease, None, retention_days=1)
        now[0] = datetime(2026, 10, 3, tzinfo=UTC)  # past the 1-day retention

        with pytest.raises(Exception) as cold_restore_error:
            await storage.restore(ref, retention_days=1)
        assert type(cold_restore_error.value).__name__ == "SnapshotExpired"

        snapshot_key = ref.snapshot_key("pfx", "snap-old")
        opened_keys: list[str] = []
        real_get_object = client.get_object

        def tracking_get_object(*, Bucket, Key):
            opened_keys.append(Key)
            return real_get_object(Bucket=Bucket, Key=Key)

        client.get_object = tracking_get_object

        new_archive = b"new-live-data"

        async def mini(request):
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-new"}, content=new_archive)

        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda observed: {"X-ACH-Instance-Id": observed.instance_id}
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", control.config.agent.uid, workspace_ref), "ws", "pod-new",
            "http://mini",
        )

        # Fresh Lifecycle: a Harness restart starts with no cached leases/bases, even though
        # the mini-harness-owned Workspace/session itself is already live.
        lifecycle = Lifecycle(control, alarm=lambda _reason: None)
        try:
            if domain == "workspace":
                await lifecycle._ensure_workspace_writer(endpoint)
                receipt = await lifecycle.commit_workspace(endpoint)
            else:
                receipt = await lifecycle._persist_close(
                    endpoint, ref, f"/sessions/{session_ref}/archive", retention_days=1,
                )

            assert receipt.snapshot_id != old_receipt.snapshot_id
            assert receipt.checksum == hashlib.sha256(new_archive).hexdigest()
            assert receipt.expires_at is not None
            assert snapshot_key not in opened_keys

            restored = await storage.restore(ref, retention_days=1)
            assert restored is not None
            assert restored.snapshot_id == receipt.snapshot_id
        finally:
            await lifecycle.close()
            await control.mini_client.aclose()

    asyncio.run(scenario())


def test_disabled_persistence_preserves_existing_idle_close() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        control.manager.closed = []
        control.manager.close = _close_record(control.manager)
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
                                     "ws", "pod", "http://mini")
        control.manager._active = {"a" * 64: endpoint}
        control.delivery = _Delivery()
        async def mini(request):
            if request.url.path == "/sessions":
                return httpx.Response(200, json=[{"sessionRef": "b" * 64}])
            return httpx.Response(200, json={"initialized": True, "nativeSessionId": "native-1",
                                             "active": False, "uncertain": False})
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control)
        saves = []
        async def save(endpoint, session_ref):
            saves.append(session_ref)
        lifecycle._save_session = save
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        await lifecycle.tick(start + timedelta(seconds=31))
        assert saves == []
        assert control.manager.closed == [endpoint.ref]
        await control.mini_client.aclose()
    asyncio.run(scenario())


def test_confirmed_session_close_is_per_pod_and_cleared_by_work() -> None:
    import httpx
    from datetime import timedelta
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    async def scenario():
        control = _Control()
        control.config.workspace.session.persistence.enabled = True
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", control.config.agent.uid, "a" * 64),
                                     "ws", "pod-1", "http://mini")
        control.manager._active = {"a" * 64: endpoint}
        control.delivery = _Delivery()
        async def mini(request):
            if request.url.path == "/sessions":
                return httpx.Response(200, json=[{"sessionRef": "b" * 64}])
            return httpx.Response(200, json={"initialized": True, "nativeSessionId": "native-1",
                                             "active": False, "uncertain": False})
        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        control._headers = lambda endpoint: {"X-ACH-Instance-Id": endpoint.instance_id}
        control._mini_json = _mini_json(control)
        lifecycle = Lifecycle(control)
        saves = []
        async def save(endpoint, session_ref):
            saves.append((endpoint.instance_id, session_ref))
        lifecycle._save_session = save
        start = datetime(2026, 10, 1, tzinfo=UTC)
        await lifecycle.tick(start)
        await lifecycle.tick(start + timedelta(seconds=1))
        assert saves == [("pod-1", "b" * 64)]
        control.admission.tickets.append(type("Ticket", (), {"workspace_ref": "a" * 64,
                                                               "session_ref": "b" * 64})())
        await lifecycle.tick(start + timedelta(seconds=2))
        control.admission.tickets.clear()
        await lifecycle.tick(start + timedelta(seconds=3))
        assert saves == [("pod-1", "b" * 64), ("pod-1", "b" * 64)]
        replacement = WorkspaceEndpoint(endpoint.ref, "ws", "pod-2", "http://mini")
        control.manager._active["a" * 64] = replacement
        await lifecycle.tick(start + timedelta(seconds=4))
        assert saves[-1] == ("pod-2", "b" * 64)
        assert len(saves) == 3
        await control.mini_client.aclose()
    asyncio.run(scenario())


def test_restart_idle_and_started_preserve_ticket_deadline() -> None:
    from datetime import timedelta
    from ach_runtime.harness.admission import AdmissionTicket
    from ach_runtime.channels.event import Event, RoutedEvent

    async def scenario():
        control = _Control()
        endpoint = type("Endpoint", (), {"ref": type("Ref", (), {"workspace_ref": "a"*64})()})()
        control.manager._active = {}
        ticket = AdmissionTicket(
            "inv", "ach", control.config.agent.uid, "a"*64, "b"*64,
            RoutedEvent(Event("evt", "ch", "push", "webhook"), "wk", "sk"),
            datetime.now(UTC) + timedelta(seconds=60),
            asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future(),
        )
        deadline = ticket.deadline_at
        lifecycle = Lifecycle(control)
        await lifecycle.tick(datetime.now(UTC))
        assert ticket.deadline_at is deadline
        assert not ticket.started.done()
        await lifecycle.close()
    asyncio.run(scenario())


def _mini_json(control):
    async def call(endpoint, method, path, payload=None):
        if method == "GET" and path == "/healthz":
            return 200, {"status": "ok"}
        response = await control.mini_client.request(method, endpoint.base_url + path, headers=control._headers(endpoint), json=payload)
        return response.status_code, response.json()
    return call


class _Delivery:
    async def list_runs(self, endpoint):
        return []


def _close_record(manager):
    async def close(ref):
        manager.closed.append(ref)
    return close


def _run_tick_loop(control, monkeypatch, script, *, on_sleep=None):
    """Drive `_tick_loop` with a fake clock: every tick consumes one `script` entry
    (whether that tick saw run/queue activity, or an exception it raises) and the loop
    closes when it runs out."""
    import ach_runtime.harness.lifecycle as lifecycle_module

    clock = [0.0]
    tick_times = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        clock[0] += delay
        if on_sleep is not None:
            on_sleep(clock[0])
        await real_sleep(0)

    monkeypatch.setattr(lifecycle_module.asyncio, "sleep", fake_sleep)

    async def scenario():
        lifecycle = Lifecycle(control)

        async def fake_tick(_now):
            tick_times.append(clock[0])
            entry = script.pop(0)
            if not script:
                lifecycle._closed = True
            if isinstance(entry, Exception):
                raise entry
            lifecycle._saw_activity = entry

        lifecycle.tick = fake_tick
        await asyncio.wait_for(lifecycle._tick_loop(), timeout=5)

    asyncio.run(scenario())
    return tick_times


def test_tick_loop_backs_off_while_idle_and_wakes_on_admitted_work(monkeypatch) -> None:
    from types import SimpleNamespace

    control = _Control()
    control.config.workspace.idle_timeout_seconds = 600
    control.config.workspace.session.idle_timeout_seconds = 600
    control.manager._active = {"a" * 64: object()}
    ticket = SimpleNamespace(workspace_ref="a" * 64, session_ref="b" * 64)

    def on_sleep(now):
        if now == 9.0:  # work admitted one second into the 5 s idle wait
            control.admission.tickets.append(ticket)
        if now == 10.0:
            control.admission.tickets.clear()

    ticks = _run_tick_loop(control, monkeypatch, [False, False, False, False, True, False, False],
                           on_sleep=on_sleep)
    # idle: 1 s, 2 s, 5 s; the admitted ticket cuts the 5 s wait to 1 s; busy keeps 1 s;
    # back to idle restarts the 1 s, 2 s, ... ladder.
    assert ticks == [0.0, 1.0, 3.0, 8.0, 9.0, 10.0, 11.0]


def test_tick_loop_idle_backoff_stays_under_a_tenth_of_the_smallest_idle_timeout(monkeypatch) -> None:
    control = _Control()  # session idle timeout 20 s -> idle tick never exceeds 2 s
    ticks = _run_tick_loop(control, monkeypatch, [False] * 5)
    assert ticks == [0.0, 1.0, 3.0, 5.0, 7.0]


def test_failing_tick_logs_cause_alarms_own_kind_and_backs_off(monkeypatch, caplog) -> None:
    from types import SimpleNamespace

    from ach_runtime import observability

    kinds: list[str] = []
    monkeypatch.setattr(observability, "alarm", lambda kind, **_refs: kinds.append(kind))
    control = _Control()
    control.config.workspace.idle_timeout_seconds = 600
    control.config.workspace.session.idle_timeout_seconds = 600
    control.manager._active = {"a" * 64: object()}
    # Admitted work must not pin a deterministically failing tick at 1 Hz.
    control.admission.tickets.append(SimpleNamespace(workspace_ref="a" * 64, session_ref="b" * 64))
    failure = RuntimeError("storage index exploded")

    with caplog.at_level(logging.ERROR, logger="ach_runtime.harness.lifecycle"):
        ticks = _run_tick_loop(control, monkeypatch, [failure] * 4 + [True, True])

    # failures back off 1 s, 2 s, 5 s, 5 s; the first success resets to the busy 1 s.
    assert ticks == [0.0, 1.0, 3.0, 8.0, 13.0, 14.0]
    assert kinds == ["lifecycle_tick_failed"] * 4
    failures = [r for r in caplog.records if r.name == "ach_runtime.harness.lifecycle"]
    assert len(failures) == 4
    assert all("RuntimeError: storage index exploded" in r.getMessage() for r in failures)
    assert "fake_tick" in failures[0].getMessage()
    assert "fake_tick" not in failures[1].getMessage()


def _ws_endpoint(control):
    from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

    return WorkspaceEndpoint(
        WorkspaceRef("ach", control.config.agent.uid, "a" * 64), "ws", "pod", "http://mini"
    )


def test_workspace_start_hook_runs_after_restore_without_persistence() -> None:
    async def scenario():
        control = _Control()
        lifecycle = Lifecycle(control)
        await lifecycle.restore_workspace(_ws_endpoint(control))
        assert control.workspace_hooks == [("start", False)]
        await lifecycle.close()
    asyncio.run(scenario())


def test_workspace_start_hook_reports_a_restored_workspace() -> None:
    import httpx
    from types import SimpleNamespace

    async def scenario():
        control = _Control()
        control.config.workspace.persistence.enabled = True

        async def chunks():
            yield b"archive"

        async def restore(_ref, *, retention_days=0):
            return SimpleNamespace(open=chunks, etag="etag-1")

        async def acquire_writer(_ref, _instance_id):
            return SimpleNamespace(base_etag="etag-1")

        control.storage = SimpleNamespace(restore=restore, acquire_writer=acquire_writer)

        async def mini(request):
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, json={"status": "restored"})

        control.mini_client = httpx.AsyncClient(transport=httpx.MockTransport(mini))
        lifecycle = Lifecycle(control)
        await lifecycle.restore_workspace(_ws_endpoint(control))
        assert control.workspace_hooks == [("start", True)]
        await control.mini_client.aclose()
        await lifecycle.close()
    asyncio.run(scenario())


def test_workspace_start_hook_failure_alarms_but_never_blocks_the_workspace() -> None:
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL

    async def scenario():
        control = _Control()
        control.workspace_hook_error["start"] = RuntimeError("start-hook-boom")
        before = ANOMALIES_TOTAL.labels(kind="workspace_start_failed")._value.get()
        lifecycle = Lifecycle(control)
        await lifecycle.restore_workspace(_ws_endpoint(control))
        assert ANOMALIES_TOTAL.labels(kind="workspace_start_failed")._value.get() == before + 1
        await lifecycle.close()
    asyncio.run(scenario())


def test_workspace_stop_hook_runs_before_commit() -> None:
    async def scenario():
        control = _Control()
        lifecycle = Lifecycle(control)
        assert await lifecycle.commit_workspace(_ws_endpoint(control)) is None
        assert control.workspace_hooks == [("stop", False)]
        await lifecycle.close()
    asyncio.run(scenario())


def test_workspace_stop_hook_failure_alarms_but_never_blocks_the_close() -> None:
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL

    async def scenario():
        control = _Control()
        control.workspace_hook_error["stop"] = RuntimeError("stop-hook-boom")
        before = ANOMALIES_TOTAL.labels(kind="workspace_stop_failed")._value.get()
        lifecycle = Lifecycle(control)
        assert await lifecycle.commit_workspace(_ws_endpoint(control)) is None
        assert ANOMALIES_TOTAL.labels(kind="workspace_stop_failed")._value.get() == before + 1
        await lifecycle.close()
    asyncio.run(scenario())

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

from ach_runtime.storage.refs import DOMAIN_SESSION, DOMAIN_WORKSPACE, StateRef


pytestmark = pytest.mark.asyncio


def _state_ref(stack, endpoint, domain: str, session_ref: str | None = None) -> StateRef:
    return StateRef(
        agent=stack.control.config.agent,
        workspace_ref=endpoint.ref.workspace_ref,
        domain=domain,
        session_ref=session_ref,
    )


async def _settle(stack, started, timeout: float = 20.0):
    async with asyncio.timeout(timeout):
        while True:
            status = await stack.status_for(started)
            if status.state in {"completed", "failed", "cancelled"}:
                return status
            await asyncio.sleep(0.05)


def _workspace_and_session(stack, started):
    return stack.current_session(started)


_BUSINESS_PROMPT_PREFIX = "Review the current state of this merge request."


def _is_business_input(item):
    return item.get("role") == "user" and any(
        isinstance(part.get("text"), str) and part["text"].startswith(_BUSINESS_PROMPT_PREFIX)
        for part in item.get("parts", [])
    )


def _business_input_occurrences(records):
    occurrences = []
    for record in records:
        if ":streamGenerateContent" not in record["url"]:
            continue
        contents = record["body"].get("contents", [])
        business_index = next((
            index for index in range(len(contents) - 1, -1, -1)
            if _is_business_input(contents[index])
        ), None)
        if business_index is None:
            continue
        continuation = any(
            item.get("role") == "user"
            and any("functionResponse" in part for part in item.get("parts", []))
            for item in contents[business_index + 1:]
        )
        if not continuation:
            occurrences.append(contents[business_index])
    return occurrences


def _assert_delivered_once(native, started):
    invocation = native._invocations[started.invocation_id]
    assert invocation.input_id
    deliveries = [
        event["data"] for event, accepted in native._event_log[invocation.event_seq_baseline:]
        if accepted
        and event.get("type") == "session.inbox.delivered"
        and event.get("data", {}).get("sessionID") == native.sid
        and event.get("data", {}).get("inboxID") == invocation.input_id
    ]
    assert len(deliveries) == 1
    return deliveries[0]


async def _snapshot_baseline(stack, endpoint, session_ref: str) -> tuple[object, object]:
    await stack.control.lifecycle._ensure_workspace_writer(endpoint)
    await stack.control.lifecycle._save_session(endpoint, session_ref)
    workspace = await stack.control.lifecycle.commit_workspace(endpoint)
    session = await stack.control.storage.restore(_state_ref(stack, endpoint, DOMAIN_SESSION, session_ref))
    assert workspace is not None and session is not None
    return workspace, session


async def _archive_bytes(receipt) -> bytes:
    return b"".join([chunk async for chunk in receipt.open()])


def _archive_has(payload: bytes, marker: bytes) -> bool:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile():
                content = archive.extractfile(member)
                if content is not None and marker in content.read():
                    return True
    return False


async def test_session_snapshot_and_workspace_sleep_wake(e2e_stack_factory):
    stack = await e2e_stack_factory(persistent=True, session_idle_seconds=1, workspace_idle_seconds=2)
    first_event = "40000000-0000-4000-8000-000000000001"
    assert (await stack.post_event(event_uuid=first_event)).status_code == 202
    started = stack.started[0]
    assert (await _settle(stack, started)).state == "completed"
    endpoint, state, native = _workspace_and_session(stack, started)
    _assert_delivered_once(native, started)
    workspace = stack._workspace_roots[endpoint.ref.workspace_ref]
    marker = workspace / "review-state.txt"
    marker.write_text("durable workspace marker", encoding="utf-8")
    private_marker = state.home / "private-home-marker"
    private_marker.write_text("session-only marker", encoding="utf-8")
    scratch_marker = native.scratch / "must-not-enter-session-snapshot"
    scratch_marker.parent.mkdir(parents=True, exist_ok=True)
    scratch_marker.write_text("Scratch is ephemeral", encoding="utf-8")
    native_sid = native.sid
    await stack.control.lifecycle._ensure_workspace_writer(endpoint)
    session_ref = _state_ref(stack, endpoint, DOMAIN_SESSION, started.session_ref)
    session_lease_key = (endpoint.ref.workspace_ref, started.session_ref, endpoint.instance_id)
    session_lease = await stack.control.storage.acquire_writer(session_ref, endpoint.instance_id)
    stack.control.lifecycle._leases[session_lease_key] = session_lease
    await stack.control.lifecycle._save_session(endpoint, started.session_ref)
    session_receipt = await stack.control.storage.restore(session_ref)
    await stack.control.lifecycle.commit_workspace(endpoint)
    workspace_ref = _state_ref(stack, endpoint, DOMAIN_WORKSPACE)
    workspace_receipt = await stack.control.storage.restore(workspace_ref)
    assert session_receipt is not None and workspace_receipt is not None
    assert session_receipt.ref.domain == "session" and workspace_receipt.ref.domain == "workspace"
    assert session_receipt.etag != workspace_receipt.etag
    assert session_receipt.checksum != workspace_receipt.checksum
    assert session_receipt.size > 0 and workspace_receipt.size > 0
    session_archive = await _archive_bytes(session_receipt)
    workspace_archive = await _archive_bytes(workspace_receipt)
    assert _archive_has(workspace_archive, b"durable workspace marker")
    assert not _archive_has(workspace_archive, b"session-only marker")
    assert _archive_has(session_archive, b"session-only marker")
    assert not _archive_has(session_archive, b"Scratch is ephemeral")

    async with asyncio.timeout(20.0):
        while stack.control.admission.has_work(endpoint.ref.workspace_ref):
            await asyncio.sleep(0.05)

    now = datetime.now(timezone.utc)
    await stack.control.lifecycle.tick(now + timedelta(seconds=2))
    await stack.control.lifecycle.tick(now + timedelta(seconds=4))
    assert stack.control.manager.active_endpoint(endpoint.ref.workspace_ref) is None
    assert endpoint.instance_id in stack.closed_instances
    assert endpoint.name not in stack.host.units

    wake_marker = "e2e-wake-only-new-event-2"
    assert (await stack.post_event(event_type="note", text=wake_marker, event_uuid="40000000-0000-4000-8000-000000000002")).status_code == 202
    resumed = stack.started[-1]
    assert (await _settle(stack, resumed)).state == "completed"
    resumed_endpoint = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert resumed_endpoint.instance_id != endpoint.instance_id
    resumed_workspace = stack._workspace_roots[endpoint.ref.workspace_ref]
    assert (resumed_workspace / "review-state.txt").read_text(encoding="utf-8") == "durable workspace marker"
    resumed_endpoint, resumed_state, resumed_native = _workspace_and_session(stack, resumed)
    assert resumed_endpoint.instance_id != endpoint.instance_id
    assert resumed_state is not None and resumed_state.native_session_id == native_sid
    assert (resumed_state.home / "private-home-marker").read_text(encoding="utf-8") == "session-only marker"
    assert not (resumed_native.scratch / scratch_marker.name).exists()
    assert resumed.invocation_id != started.invocation_id
    assert resumed_native._invocations[resumed.invocation_id].input_id != native._invocations[started.invocation_id].input_id
    captured = await stack.captured()
    assert len(captured["mcpCalls"]) == 2
    latest_history = json.dumps(captured["records"][-1]["body"])
    assert first_event in latest_history and wake_marker in latest_history
    assert len(_business_input_occurrences(captured["records"])) == 2
    _assert_delivered_once(resumed_native, resumed)
    assert sum(uid == resumed_endpoint.instance_id and path.endswith("/workspace/archive") for uid, path in stack.archive_restores) == 1
    assert sum(uid == resumed_endpoint.instance_id and path.endswith("/archive") and path.startswith("/sessions/") for uid, path in stack.archive_restores) == 1


@pytest.mark.parametrize("mode", ["duplicate", "lost"])
async def test_duplicate_and_lost_started_response_do_not_replay(e2e_stack_factory, mode):
    stack = await e2e_stack_factory(scenario="review")
    event_id = "eb64cc44-8ef8-443a-9d37-c3b89decc718"
    if mode == "lost":
        stack.faults["lost_started"] = True
        responses = [await stack.post_event(event_uuid=event_id)]
    else:
        responses = await asyncio.gather(stack.post_event(event_uuid=event_id), stack.post_event(event_uuid=event_id))
    assert all(response.status_code == 202 for response in responses)
    expected_starts = 2 if mode == "lost" else 1
    assert len(stack.started) == expected_starts
    assert len(stack.start_payloads) == expected_starts
    started = stack.started[0]
    if mode == "lost":
        start_positions = [
            index for index, request in enumerate(stack.requests)
            if request.method == "POST" and request.url.path == "/start"
        ]
        status_path = f"/status/{started.invocation_id}"
        recovery_status_positions = [
            index for index, request in enumerate(stack.requests)
            if request.method == "GET" and request.url.path == status_path
        ]
        assert len(start_positions) == 2
        assert any(start_positions[0] < index < start_positions[1] for index in recovery_status_positions), (
            "real recovery GET /status must follow the dropped Started POST and precede the cached retry"
        )
    assert (await _settle(stack, started)).state == "completed"
    native = _workspace_and_session(stack, started)[2]
    assert set(native._invocations) == {started.invocation_id}
    _assert_delivered_once(native, started)
    captured = await stack.captured()
    assert len(captured["mcpCalls"]) == 1
    assert len([request for request in stack.requests if request.url.path == "/start"]) == expected_starts
    assert len(set(stack.start_instance_ids)) == 1
    status = await stack.status_for(started)
    assert (status.invocation_id, status.run_id, status.workspace_ref, status.session_ref, status.config_version) == (
        started.invocation_id, started.run_id, stack.start_payloads[0]["workspaceRef"], started.session_ref, stack.start_payloads[0]["configVersion"]
    )
    native = _workspace_and_session(stack, started)[2]
    assert native._invocations[started.invocation_id].input_id
    assert len(_business_input_occurrences(captured["records"])) == 1
    if mode == "lost":
        assert "deadlineAt" in stack.start_payloads[0]
        assert stack.start_payloads[0] == stack.start_payloads[-1]
        assert stack.start_raw_bodies[0] == stack.start_raw_bodies[-1]


async def test_lost_started_changed_owner_never_replays(e2e_stack_factory, monkeypatch):
    stack = await e2e_stack_factory(scenario="review")
    unknown_outcomes: list[tuple[str, str, str]] = []
    record_run = type(stack.control)._record_run

    def observe_run(self, expected, status, started_run_id, event, *, outcome=None):
        if self is stack.control and outcome == "unknown":
            unknown_outcomes.append((expected.invocation_id, expected.workspace_ref, expected.session_ref))
        record_run(self, expected, status, started_run_id, event, outcome=outcome)

    monkeypatch.setattr(type(stack.control), "_record_run", observe_run)
    stack.faults["lost_started"] = True
    old_uids: list[str] = []

    async def replace_owner(current, app):
        endpoint = next(value for value in current.control.manager.active_endpoints().values() if value.instance_id == app.state.instance_id)
        old_uids.append(endpoint.instance_id)
        current.host.withhold_delete(endpoint.name)
        recovery = asyncio.create_task(current.control.manager.recover(endpoint.ref, endpoint.instance_id))
        await asyncio.sleep(0.1)
        await current.host.release_delete(endpoint.name)
        await recovery

    stack.faults["after_lost_started"] = replace_owner
    response = await stack.post_event()
    assert response.status_code == 202  # admitted before the subsequent owner loss
    assert len(stack.started) == 1
    assert len(stack.start_payloads) == 1
    assert stack.start_payloads[0]["invocationId"] == stack.started[0].invocation_id
    assert len([request for request in stack.requests if request.url.path == "/start"]) == 1
    assert len(stack.control.manager.active_endpoints()) == 1
    replacement = next(iter(stack.control.manager.active_endpoints().values()))
    assert replacement.instance_id != old_uids[0]
    assert stack.start_instance_ids == [old_uids[0]]
    invocation_id = stack.start_payloads[0]["invocationId"]
    old_native = stack.minis[old_uids[0]].state.service._engine._sessions[stack.start_payloads[0]["sessionRef"]]
    _assert_delivered_once(old_native, stack.started[0])
    failed_ticket = stack.control.admission._tickets[invocation_id]
    assert failed_ticket.started.done() and failed_ticket.started.exception() is not None
    assert failed_ticket.completed.done() and failed_ticket.completed.exception() is not None
    assert "unknown" in str(failed_ticket.completed.exception()).lower()
    assert unknown_outcomes == [(invocation_id, stack.start_payloads[0]["workspaceRef"], stack.start_payloads[0]["sessionRef"])]
    with pytest.raises(Exception, match="HTTP 404"):
        await stack.control.delivery.status(replacement, invocation_id)


async def test_health_ko_recovers_latest_without_replay(e2e_stack_factory):
    stack = await e2e_stack_factory(scenario="review", persistent=True)
    assert (await stack.post_event()).status_code == 202
    started = stack.started[0]
    assert (await _settle(stack, started)).state == "completed"
    endpoint, state, native = _workspace_and_session(stack, started)
    _assert_delivered_once(native, started)
    sid = native.sid
    workspace_marker = stack._workspace_roots[endpoint.ref.workspace_ref] / "durable-before-health-failure"
    workspace_marker.write_text("committed baseline", encoding="utf-8")
    await _snapshot_baseline(stack, endpoint, started.session_ref)
    (stack._workspace_roots[endpoint.ref.workspace_ref] / "uncommitted-before-health-failure").write_text("may be lost", encoding="utf-8")
    stack.faults["healthz"] = True
    stack.host.withhold_delete(endpoint.name)
    recovery = asyncio.create_task(stack.control.lifecycle.tick(datetime.now(timezone.utc)))
    async with asyncio.timeout(10):
        while stack.healthz_failures == 0 or endpoint.instance_id not in stack.closed_instances:
            await asyncio.sleep(0.02)
    assert not recovery.done()
    assert stack.faults["healthz"] is False
    assert stack.control.manager.active_endpoint(endpoint.ref.workspace_ref) is None
    await stack.host.release_delete(endpoint.name)
    await recovery
    assert stack.healthz_failures == 1
    replacement_unit = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert replacement_unit.instance_id != endpoint.instance_id
    assert stack.host.started_count == 2
    assert len([request for request in stack.requests if request.url.path == "/start"]) == 1
    pre_followup = await stack.captured()
    assert len(_business_input_occurrences(pre_followup["records"])) == 1
    assert (await stack.post_event(event_type="note", text="fresh event after recovery", event_uuid="50000000-0000-4000-8000-000000000002")).status_code == 202
    resumed = stack.started[-1]
    assert (await _settle(stack, resumed)).state == "completed"
    resumed_endpoint, resumed_state, resumed_native = _workspace_and_session(stack, resumed)
    assert resumed_endpoint.instance_id != endpoint.instance_id
    assert resumed_state.native_session_id == sid == resumed_native.sid
    assert (stack._workspace_roots[endpoint.ref.workspace_ref] / "durable-before-health-failure").read_text(encoding="utf-8") == "committed baseline"
    assert not (stack._workspace_roots[endpoint.ref.workspace_ref] / "uncommitted-before-health-failure").exists()
    assert len([request for request in stack.requests if request.url.path == "/start"]) == 2
    final_capture = await stack.captured()
    assert len(final_capture["mcpCalls"]) == 2
    assert len(_business_input_occurrences(final_capture["records"])) == 2
    _assert_delivered_once(resumed_native, resumed)


async def test_forced_pod_loss_recovers_latest_without_replay(e2e_stack_factory):
    stack = await e2e_stack_factory(scenario="review", persistent=True)
    assert (await stack.post_event()).status_code == 202
    started = stack.started[0]
    assert (await _settle(stack, started)).state == "completed"
    endpoint, _state, native = _workspace_and_session(stack, started)
    original_native_sid = native.sid
    _assert_delivered_once(native, started)

    workspace_root = stack._workspace_roots[endpoint.ref.workspace_ref]
    committed_marker = workspace_root / "durable-before-forced-pod-loss"
    uncommitted_marker = workspace_root / "uncommitted-before-forced-pod-loss"
    committed_marker.write_text("committed baseline", encoding="utf-8")
    await _snapshot_baseline(stack, endpoint, started.session_ref)
    uncommitted_marker.write_text("may be lost", encoding="utf-8")

    old_uid = endpoint.instance_id
    await stack.host.kill(endpoint.name, report=False)
    assert old_uid in stack.closed_instances

    await stack.control.lifecycle.tick(datetime.now(timezone.utc))

    recovered_endpoint = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert recovered_endpoint.instance_id != endpoint.instance_id
    assert committed_marker.read_text(encoding="utf-8") == "committed baseline"
    assert not uncommitted_marker.exists()
    assert len([request for request in stack.requests if request.url.path == "/start"]) == 1
    assert len(_business_input_occurrences((await stack.captured())["records"])) == 1

    assert (await stack.post_event(
        event_type="note",
        text="fresh event after forced pod loss",
        event_uuid="50000000-0000-4000-8000-000000000003",
    )).status_code == 202
    resumed = stack.started[-1]
    assert (await _settle(stack, resumed)).state == "completed"
    resumed_endpoint, resumed_state, resumed_native = _workspace_and_session(stack, resumed)
    assert resumed_endpoint.instance_id != endpoint.instance_id
    assert resumed_state.native_session_id == original_native_sid
    assert resumed_native.sid == original_native_sid
    assert len([request for request in stack.requests if request.url.path == "/start"]) == 2
    assert len(_business_input_occurrences((await stack.captured())["records"])) == 2
    _assert_delivered_once(resumed_native, resumed)


async def test_failed_snapshot_retains_local_writer(e2e_stack_factory, monkeypatch):
    stack = await e2e_stack_factory(persistent=True)
    assert (await stack.post_event()).status_code == 202
    started = stack.started[0]
    assert (await _settle(stack, started)).state == "completed"
    endpoint, _state, _native = _workspace_and_session(stack, started)
    workspace_receipt, _session_receipt = await _snapshot_baseline(stack, endpoint, started.session_ref)
    workspace_ref = _state_ref(stack, endpoint, DOMAIN_WORKSPACE)
    marker = stack._workspace_roots[endpoint.ref.workspace_ref] / "unsaved-local-bytes"
    marker.write_text("retain this writer on S3 failure", encoding="utf-8")
    storage_client = stack.control.storage._client
    original_put = storage_client.put_object

    failed_keys: list[str] = []
    def fail_snapshot(*, Bucket, Key, Body, **kwargs):
        if "/snapshots/" in Key and "/sessions/" not in Key:
            failed_keys.append(Key)
            raise OSError("injected real S3 snapshot write failure")
        return original_put(Bucket=Bucket, Key=Key, Body=Body, **kwargs)

    monkeypatch.setattr(storage_client, "put_object", fail_snapshot)
    observed = datetime.now(timezone.utc)
    await stack.control.lifecycle.tick(observed)
    await stack.control.lifecycle.tick(observed + timedelta(seconds=1))
    await stack.control.lifecycle.tick(observed + timedelta(seconds=5))
    await stack.control.lifecycle.tick(observed + timedelta(seconds=9))
    assert failed_keys, "the real S3 SDK snapshot write fault must be reached"
    assert workspace_receipt is not None
    current_receipt = await stack.control.storage.restore(workspace_ref)
    assert current_receipt is not None and current_receipt.etag == workspace_receipt.etag
    assert marker.read_text(encoding="utf-8") == "retain this writer on S3 failure"
    assert endpoint.name in stack.host.units
    assert endpoint.instance_id not in stack.closed_instances
    assert endpoint.ref.workspace_ref in stack.control.manager._occupied
    assert (endpoint.ref.workspace_ref, endpoint.instance_id) in stack.control.lifecycle._failed_workspace_saves


async def test_corrupt_latest_fails_closed(e2e_stack_factory):
    stack = await e2e_stack_factory(persistent=True)
    assert (await stack.post_event()).status_code == 202
    started = stack.started[0]
    assert (await _settle(stack, started)).state == "completed"
    # Drain Harness observation before manually removing its execution owner.
    await asyncio.wait_for(stack.control.admission._tickets[started.invocation_id].completed, 5)
    endpoint, _state, _native = _workspace_and_session(stack, started)
    (stack._workspace_roots[endpoint.ref.workspace_ref] / "baseline").write_text("persisted", encoding="utf-8")
    await _snapshot_baseline(stack, endpoint, started.session_ref)
    ref = _state_ref(stack, endpoint, DOMAIN_WORKSPACE)
    await stack.control.manager.close(endpoint.ref)
    receipt = await stack.control.storage.restore(ref)
    assert receipt is not None
    key = ref.snapshot_key(stack.control.storage._prefix, receipt.snapshot_id)
    stack.control.storage._client.put_object(Bucket=stack.control.storage._bucket, Key=key, Body=b"corrupt registered archive")
    before_starts = len(stack.start_payloads)
    response = await stack.post_event(event_type="note", text="must not run after corrupt restore")
    assert response.status_code == 202  # admission succeeds; restoration may fail later
    assert len(stack.start_payloads) == before_starts
    assert len([request for request in stack.requests if request.url.path == "/start"]) == before_starts
    assert stack.control.manager.active_endpoint(endpoint.ref.workspace_ref) is None
    # The failed activation retired its unit; the slot is free once the host confirmed the stop.
    assert endpoint.name not in stack.host.units
    assert endpoint.ref.workspace_ref not in stack.control.manager._occupied

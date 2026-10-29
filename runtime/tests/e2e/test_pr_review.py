from __future__ import annotations

import asyncio
import gzip
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest


pytestmark = pytest.mark.asyncio


async def _settled(stack, started, *, timeout: float = 20.0):
    async with asyncio.timeout(timeout):
        while True:
            status = await stack.status_for(started)
            if status.state in {"completed", "failed", "cancelled"}:
                return status
            await asyncio.sleep(0.05)


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


def _held_model_records(capture):
    return [
        record for record in capture["records"]
        if record.get("heldModel")
        and (":streamGenerateContent" in record["url"] or record["url"].split("?", 1)[0].endswith("/messages"))
    ]


def _workspace_and_session(stack, started):
    return stack.current_session(started)


async def test_mr_private_clone_started_and_managed_comment(e2e_stack_factory):
    stack = await e2e_stack_factory(scenario="review")
    assert stack.owned_inventory_ready and stack.ready_after_discovery
    await stack.upstream.control({"command": "hold"})
    ingress = asyncio.create_task(stack.post_event(event_uuid="10000000-0000-4000-8000-000000000001"))
    async with asyncio.timeout(20):
        while not _held_model_records(await stack.captured()):
            await asyncio.sleep(0.02)
    response = await ingress
    assert response.status_code == 202
    assert len(stack.host.units) == stack.host.started_count == 1
    assert len(stack.started) == 1
    started = stack.started[0]
    request = stack.start_payloads[0]
    response_uid, started_bytes, started_headers = stack.start_responses[0]
    started_body = json.loads(started_bytes)
    assert response_uid == stack.start_instance_ids[0]
    assert started_body["invocationId"] == started.invocation_id and started_headers["x-ach-instance-id"] == response_uid
    assert (started.invocation_id, started.session_ref, started.config_version) == (
        request["invocationId"], request["sessionRef"], request["configVersion"]
    )
    assert started.run_id and started.run_id != request["invocationId"] and request["deadlineAt"]
    endpoint, state, native = _workspace_and_session(stack, started)
    assert len(_business_input_occurrences((await stack.captured())["records"])) == 1
    async with asyncio.timeout(5):
        while not native._invocations[started.invocation_id].delivered:
            await asyncio.sleep(0.02)
    _assert_delivered_once(native, started)
    assert endpoint.instance_id
    assert response_uid == endpoint.instance_id
    assert len(stack.configure_responses) == len(stack.configure_receipts)
    assert all(
        response[0] == receipt[0] == endpoint.instance_id
        and json.loads(response[1]) == {"configVersion": receipt[1]}
        and response[2]["x-ach-instance-id"] == endpoint.instance_id
        for response, receipt in zip(stack.configure_responses, stack.configure_receipts)
    )
    pending = await stack.status_for(started)
    assert pending.state not in {"completed", "failed", "cancelled"}
    assert (pending.invocation_id, pending.run_id, pending.workspace_ref, pending.session_ref, pending.config_version) == (
        started.invocation_id, started.run_id, request["workspaceRef"], request["sessionRef"], request["configVersion"]
    )
    assert state.native_session_id and native.sid == state.native_session_id
    native_input = native._invocations[started.invocation_id].input_id
    assert native_input
    checkout = next(iter(stack._workspace_roots.values())) / "handoff" / "repo"
    assert checkout.is_dir()
    head_proc = await asyncio.create_subprocess_exec("git", "-C", str(checkout), "rev-parse", "HEAD", stdout=asyncio.subprocess.PIPE)
    head_output, _ = await head_proc.communicate()
    assert head_proc.returncode == 0
    assert head_output.decode().strip() == stack.repo_heads[("team/service", 4)]
    detached = await asyncio.create_subprocess_exec("git", "-C", str(checkout), "symbolic-ref", "-q", "HEAD", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    await detached.communicate()
    assert detached.returncode != 0, "fixture checkout must remain detached at the webhook SHA"
    assert "private-clone-token-only" not in json.dumps(request)
    assert stack.archives
    assert all(b"private-clone-token-only" not in gzip.decompress(archive) for archive in stack.archives)
    assert not any(b"private-clone-token-only" in entry for entry in Path(f"/proc/{native._proc.pid}/environ").read_bytes().split(b"\0"))
    assert len((await stack.captured())["mcpCalls"]) == 0
    await stack.release_model()
    status = await _settled(stack, started)
    assert status.state == "completed"
    assert (status.invocation_id, status.run_id, status.workspace_ref, status.session_ref, status.config_version) == (
        started.invocation_id, started.run_id, request["workspaceRef"], request["sessionRef"], request["configVersion"]
    )
    assert status.deadline_at == datetime.fromisoformat(request["deadlineAt"].replace("Z", "+00:00"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=stack.minis[endpoint.instance_id]), base_url="http://mini") as client:
        foreign = await client.get(
            f"/status/{started.invocation_id}",
            headers={**stack.control._headers(endpoint), "X-ACH-Instance-Id": "foreign-instance-id"},
        )
    assert foreign.status_code == 401
    captured = await stack.captured()
    assert captured["records"]
    assert all(record["achHeaders"]["x-ach-agent"] == stack.control.config.agent.name for record in captured["records"])
    assert all(record["achHeaders"]["x-ach-environment"] == stack.control.config.ach.environment for record in captured["records"])
    assert len(captured["mcpCalls"]) == 1
    assert captured["mcpCalls"][0]["name"] == "create_merge_request_note"
    assert captured["mcpCalls"][0]["arguments"]["project_id"] == 1
    assert captured["mcpCalls"][0]["arguments"]["merge_request_iid"] == 4
    assert captured["mcpCalls"][0]["arguments"]["body"] == "Review complete: one actionable finding."


async def test_followup_note_reuses_native_session_with_refreshed_checkout(e2e_stack_factory):
    stack = await e2e_stack_factory(scenario="review", handoff_scope="event")
    first_event = "10000000-0000-4000-8000-000000000002"
    first_response = await stack.post_event(event_uuid=first_event)
    assert first_response.status_code == 202
    first = stack.started[0]
    assert (await _settled(stack, first)).state == "completed"
    first_endpoint, first_state, first_native = _workspace_and_session(stack, first)
    original_workspace_ref = first_endpoint.ref.workspace_ref
    original_instance_id = first_endpoint.instance_id
    original_session_ref = first.session_ref
    original_sid = first_native.sid
    model_requests_before = len((await stack.captured())["records"])
    old_head = stack.repo_heads[("team/service", 4)]
    new_head = stack.advance_mr("team/service", 4)
    second_event = "20000000-0000-4000-8000-000000000002"
    new_marker = "e2e-followup-note-marker-2"
    second_response = await stack.post_event(event_type="note", text=new_marker, event_uuid=second_event)
    assert second_response.status_code == 202
    second = stack.started[1]
    endpoint, second_state, second_native = _workspace_and_session(stack, second)
    assert (await _settled(stack, second)).state == "completed"
    _assert_delivered_once(first_native, first)
    _assert_delivered_once(second_native, second)
    assert endpoint.instance_id == original_instance_id
    assert endpoint.ref.workspace_ref == original_workspace_ref
    assert original_session_ref == first.session_ref == second.session_ref
    assert first_state.native_session_id == second_state.native_session_id
    assert first.invocation_id != second.invocation_id
    assert first_native._invocations[first.invocation_id].input_id != second_native._invocations[second.invocation_id].input_id
    assert original_sid == first_native.sid == second_native.sid
    checkout = next(iter(stack._workspace_roots.values())) / "handoff" / "repo"
    head = await asyncio.create_subprocess_exec("git", "-C", str(checkout), "rev-parse", "HEAD", stdout=asyncio.subprocess.PIPE)
    output, _ = await head.communicate()
    assert output.decode().strip() == new_head and new_head != old_head
    captured = await stack.captured()
    async with asyncio.timeout(10):
        while len(captured["records"]) <= model_requests_before:
            await asyncio.sleep(0.02)
            captured = await stack.captured()
    latest_model_input = json.dumps(captured["records"][-1]["body"])
    assert first_event in latest_model_input
    assert new_marker in latest_model_input
    assert len(_business_input_occurrences(captured["records"])) == 2
    assert stack.handoff_import_observations
    handoff = stack.handoff_import_observations[-1]
    assert handoff["entry_processes"]
    assert all(handoff["suspended"].values())
    assert handoff["entry_head"] == handoff["boundary_head"] == old_head
    symbolic = await asyncio.create_subprocess_exec(
        "git", "-C", str(checkout), "symbolic-ref", "-q", "HEAD",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    symbolic_out, _ = await symbolic.communicate()
    assert symbolic.returncode != 0, f"refreshed checkout HEAD must be detached, got {symbolic_out.decode().strip()}"
    assert stack.closed_instances.count(original_instance_id) == 0, "event handoff must not replace the warm Workspace owner"


async def test_other_mr_and_other_project_route_to_correct_owners(e2e_stack_factory):
    stack = await e2e_stack_factory(scenario="review")
    assert (await stack.post_event()).status_code == 202
    first = stack.started[0]
    assert (await stack.post_event(iid=5)).status_code == 202
    second = stack.started[1]
    assert (await stack.post_event(project_id=2, project_path="team/other", iid=4)).status_code == 202
    third = stack.started[2]
    for run in (first, second, third):
        assert (await _settled(stack, run)).state == "completed"
    endpoints = [_workspace_and_session(stack, item)[0] for item in (first, second, third)]
    assert endpoints[0].ref.workspace_ref == endpoints[1].ref.workspace_ref
    assert endpoints[0].instance_id == endpoints[1].instance_id
    assert endpoints[0].name == endpoints[1].name
    assert endpoints[2].ref.workspace_ref != endpoints[0].ref.workspace_ref
    assert endpoints[2].instance_id != endpoints[0].instance_id
    assert endpoints[2].name != endpoints[0].name
    assert len(stack.host.units) == stack.host.started_count == 2
    identities = [_workspace_and_session(stack, item)[1].native_session_id for item in (first, second, third)]
    assert all(identities) and len(set(identities)) == 3
    assert len(_business_input_occurrences((await stack.captured())["records"])) == 3
    for run in (first, second, third):
        _assert_delivered_once(_workspace_and_session(stack, run)[2], run)
    homes = [_workspace_and_session(stack, item)[1].home for item in (first, second, third)]
    assert len(set(homes)) == 3
    calls = (await stack.captured())["mcpCalls"]
    assert len(calls) == 3
    assert [(call["arguments"]["project_id"], call["arguments"]["merge_request_iid"]) for call in calls] == [(1, 4), (1, 5), (2, 4)]


async def test_cold_concurrency_and_capacity(e2e_stack_factory):
    stack = await e2e_stack_factory(max_active_workspaces=1, max_invocation_seconds=15)
    await stack.upstream.control({"command": "hold"})
    gate = stack.host.hold_start()
    first = asyncio.create_task(stack.post_event())
    await asyncio.wait_for(gate.reached.wait(), timeout=10)
    second = asyncio.create_task(stack.post_event(event_uuid="2c09b693-7e6a-4a31-9549-097fa5578831"))
    assert not gate.release.is_set()
    assert not first.done() and not stack.started, "cold create must still be gated before the capacity probe"
    await asyncio.sleep(0.1)
    assert len(stack.host.units) <= 1
    gate.release.set()
    first_response = await asyncio.wait_for(first, timeout=10)
    assert first_response.status_code == 202
    await asyncio.sleep(0.1)
    assert not second.done()
    assert len(stack.started) == 1
    assert len(stack.host.units) == stack.host.started_count == 1
    queued_event = "2c09b693-7e6a-4a31-9549-097fa5578831"
    queued_ticket = next(ticket for ticket in stack.control.admission._tickets.values() if ticket.routed_event.event_id == queued_event)
    queued_deadline = queued_ticket.deadline_at
    await stack.release_model()
    second_response = await asyncio.wait_for(second, timeout=10)
    assert second_response.status_code == 202
    assert len(stack.started) == 2
    settled_statuses = [await _settled(stack, run) for run in stack.started]
    assert all(status.state == "completed" for status in settled_statuses)
    _assert_delivered_once(_workspace_and_session(stack, stack.started[0])[2], stack.started[0])
    _assert_delivered_once(_workspace_and_session(stack, stack.started[1])[2], stack.started[1])
    assert stack.configure_receipts
    assert all(instance == stack.start_instance_ids[0] and version == stack.control.public.config_version for instance, version in stack.configure_receipts)
    assert all(item.session_ref == stack.started[0].session_ref for item in stack.started)
    assert len({item.run_id for item in stack.started}) == 2
    assert stack.start_instance_ids[0] == stack.start_instance_ids[-1]
    assert datetime.fromisoformat(stack.start_payloads[-1]["deadlineAt"].replace("Z", "+00:00")) == queued_deadline
    assert stack.host.started_count == 1
    assert stack.workspace_restore_calls.count(stack.start_instance_ids[0]) == 1
    assert any(request.url.path == "/readyz" and request.method == "GET" for request in stack.requests)
    final_endpoint, *_ = _workspace_and_session(stack, stack.started[-1])
    stack.host.withhold_delete(final_endpoint.name)
    closing = asyncio.create_task(stack.control.manager.close(final_endpoint.ref))
    async with asyncio.timeout(10):
        while final_endpoint.instance_id not in stack.closed_instances:
            await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    assert not closing.done(), "the slot stays held until the host confirms the stop"
    assert final_endpoint.ref.workspace_ref in stack.control.manager._occupied
    await stack.host.release_delete(final_endpoint.name)
    await asyncio.wait_for(closing, timeout=10)
    assert final_endpoint.ref.workspace_ref not in stack.control.manager._occupied


async def test_full_capacity_waits_for_a_slot_until_the_deadline(e2e_stack_factory):
    stack = await e2e_stack_factory(max_active_workspaces=1, max_invocation_seconds=6)
    await stack.upstream.control({"command": "hold"})
    assert (await stack.post_event()).status_code == 202
    other = "90000000-0000-4000-8000-000000000001"
    response = await stack.post_event(
        project_id=2, project_path="team/other", iid=4, event_uuid=other, wait_started=False
    )
    assert response.status_code == 202
    ticket = next(t for t in stack.control.admission._tickets.values() if t.routed_event.event_id == other)
    await asyncio.sleep(0.3)
    assert not ticket.started.done(), "a full workspace limit must make activation wait, not fail"
    assert len(stack.host.units) == 1
    done, _ = await asyncio.wait({ticket.started}, timeout=12)
    assert done and ticket.started.exception() is not None, "the wait must end at the invocation deadline"
    assert len(stack.host.units) == 1
    await stack.release_model()


async def test_real_loop_stops_at_max_steps(e2e_stack_factory):
    stack = await e2e_stack_factory(scenario="loop", max_steps=2)
    response = await stack.post_event()
    assert response.status_code == 202
    status = await _settled(stack, stack.started[0])
    assert status.state == "completed"
    captured = await stack.captured()
    calls = captured["mcpCalls"]
    assert len(calls) == 1
    assert calls[0]["name"] == "create_merge_request_note"
    model = captured["records"]
    assert len(model) == 2
    assert len(_business_input_occurrences(model)) == 1
    _assert_delivered_once(_workspace_and_session(stack, stack.started[0])[2], stack.started[0])
    assert (await stack.post_event(event_type="note", text="budget reset marker")).status_code == 202
    assert (await _settled(stack, stack.started[-1])).state == "completed"
    captured = await stack.captured()
    assert len(captured["mcpCalls"]) == 2
    assert len(captured["records"]) == 4
    assert len(_business_input_occurrences(captured["records"])) == 2
    _assert_delivered_once(_workspace_and_session(stack, stack.started[-1])[2], stack.started[-1])


@pytest.mark.parametrize(("scenario", "expected"), [("deadline", "failed"), ("cancel", "cancelled")])
async def test_real_deadline_and_explicit_cancel(e2e_stack_factory, scenario, expected):
    stack = await e2e_stack_factory(scenario=scenario, max_invocation_seconds=10)
    ingress = asyncio.create_task(stack.post_event(event_uuid="30000000-0000-4000-8000-000000000001"))
    async with asyncio.timeout(13):
        while not stack.started or not _held_model_records(await stack.captured()):
            await asyncio.sleep(0.02)
    started = stack.started[0]
    endpoint, _state, native = _workspace_and_session(stack, started)
    first_input = native._invocations[started.invocation_id].input_id
    assert first_input
    mini = stack.minis[endpoint.instance_id]
    async with asyncio.timeout(13):
        if scenario == "cancel":
            held_record = _held_model_records(await stack.captured())[0]
            assert not held_record["responseClosed"], "explicit cancel must be sent while the real model request is still held"
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mini), base_url="http://mini", timeout=5.0) as client:
                cancelled = await client.post("/cancel", headers=stack.control._headers(endpoint), json={"invocationId": started.invocation_id, "reason": "test cancellation"})
            assert cancelled.status_code == 200
            while (await stack.captured())["heldModelRequests"] != 0:
                await asyncio.sleep(0.02)
        else:
            while not any(record.get("heldModel") and record["responseClosed"] for record in (await stack.captured())["records"]):
                await asyncio.sleep(0.02)
        response = await ingress
        assert response.status_code == 202
        status = await _settled(stack, started, timeout=13)
    assert status.state == expected
    captured = await stack.captured()
    assert captured["heldModelRequests"] == 0
    held_record = _held_model_records(captured)[0]
    assert held_record["responseClosed"], "the held native model connection must close without manual release"
    if scenario == "deadline":
        assert status.error == "deadline expired"
        assert status.deadline_at == datetime.fromisoformat(stack.start_payloads[0]["deadlineAt"].replace("Z", "+00:00"))
    assert len(captured["mcpCalls"]) == 0

    # Terminal cancellation/deadline must release native session capacity for a real
    # subsequent user input; an HTTP terminal status alone is insufficient evidence.
    await stack.upstream.control({"command": "scenario", "text": "capacity released after cancellation"})
    followup = await stack.post_event(event_type="note", text="fresh input after halt", event_uuid="30000000-0000-4000-8000-000000000002")
    assert followup.status_code == 202
    second = stack.started[-1]
    assert second.invocation_id != started.invocation_id and second.session_ref == started.session_ref
    assert (await _settled(stack, second)).state == "completed"
    second_native = _workspace_and_session(stack, second)[2]
    assert second_native._invocations[second.invocation_id].input_id != first_input
    completed_capture = await stack.captured()
    _assert_delivered_once(native, started)
    _assert_delivered_once(second_native, second)
    assert len(_business_input_occurrences(completed_capture["records"])) == 2
    assert len(completed_capture["mcpCalls"]) == 0

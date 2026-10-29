"""Spec 13 integration scenarios 1-8 and acceptance tests T01-T03, T05-T09 against a `FakeHost`.

Each test names its spec ID. Nothing here sleeps for a fixed time to wait for a state: every
wait is a bounded poll."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.responses import Response

from ach_runtime.harness.workspaces import WorkspaceRetireTimeout
from ach_runtime.storage.api import WriterFenced
from ach_runtime.storage.refs import DOMAIN_WORKSPACE, StateRef

pytestmark = pytest.mark.asyncio


async def _settle(stack, started, timeout: float = 20.0):
    async with asyncio.timeout(timeout):
        while True:
            status = await stack.status_for(started)
            if status.state in {"completed", "failed", "cancelled"}:
                return status
            await asyncio.sleep(0.05)


async def _until(predicate, *, timeout: float = 15.0, what: str = "condition"):
    try:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.02)
    except TimeoutError:
        raise AssertionError(f"timed out waiting for {what}") from None


def _ticket(stack, event_uuid: str):
    return next(t for t in stack.control.admission._tickets.values() if t.routed_event.event_id == event_uuid)


def _state_ref(stack, endpoint) -> StateRef:
    return StateRef(agent=stack.control.config.agent, workspace_ref=endpoint.ref.workspace_ref, domain=DOMAIN_WORKSPACE)


async def _commit_baseline(stack, endpoint, session_ref: str):
    await stack.control.lifecycle._ensure_workspace_writer(endpoint)
    await stack.control.lifecycle._save_session(endpoint, session_ref)
    assert await stack.control.lifecycle.commit_workspace(endpoint) is not None
    receipt = await stack.control.storage.restore(_state_ref(stack, endpoint))
    assert receipt is not None
    return receipt


def _listed(stack) -> dict[str, dict]:
    return {item["name"]: item for item in stack.control.source.snapshot()["workspaces"]}


@pytest.fixture
def anomalies(monkeypatch) -> list[str]:
    seen: list[str] = []

    def record(kind, **_kwargs):
        seen.append(kind)

    monkeypatch.setattr("ach_runtime.harness.workspaces.alarm", record)
    monkeypatch.setattr("ach_runtime.harness.hosted.alarm", record)
    return seen


async def _first_run(stack, **kwargs):
    assert (await stack.post_event(**kwargs)).status_code == 202
    started = stack.started[-1]
    assert (await _settle(stack, started)).state == "completed"
    return started, stack.current_session(started)[0]


async def test_scenario_1_first_event(e2e_stack_factory):
    stack = await e2e_stack_factory()
    started, endpoint = await _first_run(stack)
    listed = _listed(stack)[endpoint.name]
    assert listed["desired"] == "running"
    assert listed["instance"] == {
        "instanceId": endpoint.instance_id, "baseUrl": endpoint.base_url, "state": "serving"
    }
    assert stack.host.started_count == 1
    assert stack.workspace_restore_calls == [endpoint.instance_id]
    assert stack.configure_receipts and all(i == endpoint.instance_id for i, _ in stack.configure_receipts)
    assert [i for i in stack.start_instance_ids] == [endpoint.instance_id]


async def test_scenario_2_idle_close_and_reactivation_restores(e2e_stack_factory):
    stack = await e2e_stack_factory(persistent=True, max_active_workspaces=1)
    started, endpoint = await _first_run(stack)
    await asyncio.wait_for(stack.control.admission._tickets[started.invocation_id].completed, 5)
    (stack._workspace_roots[endpoint.ref.workspace_ref] / "kept").write_text("committed", encoding="utf-8")
    await stack.control.manager.close(endpoint.ref)
    # commit, released, DELETE, slot freed
    assert await stack.control.storage.restore(_state_ref(stack, endpoint)) is not None
    assert (endpoint.name, endpoint.instance_id) in stack.host.deleted
    assert endpoint.instance_id in stack.closed_instances
    assert _listed(stack) == {}
    assert endpoint.ref.workspace_ref not in stack.control.manager._occupied
    # a fresh unit restores what was committed
    assert (await stack.post_event(event_type="note", text="wake", event_uuid="60000000-0000-4000-8000-000000000001")).status_code == 202
    resumed = stack.started[-1]
    assert (await _settle(stack, resumed)).state == "completed"
    again = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert again.instance_id != endpoint.instance_id
    assert (stack._workspace_roots[endpoint.ref.workspace_ref] / "kept").read_text(encoding="utf-8") == "committed"
    assert sum(i == again.instance_id and p.endswith("/workspace/archive") for i, p in stack.archive_restores) == 1


async def test_scenario_3_recover_ends_runs_in_flight_unknown(e2e_stack_factory, monkeypatch):
    stack = await e2e_stack_factory(persistent=True)
    outcomes: list[str] = []
    record_run = type(stack.control)._record_run

    def observe(self, expected, status, started_run_id, event, *, outcome=None):
        outcomes.append(outcome or (status.state if status is not None else "unknown"))
        record_run(self, expected, status, started_run_id, event, outcome=outcome)

    monkeypatch.setattr(type(stack.control), "_record_run", observe)
    await stack.upstream.control({"command": "hold"})
    assert (await stack.post_event(event_uuid="60000000-0000-4000-8000-000000000003")).status_code == 202
    started = stack.started[0]
    endpoint = stack.current_session(started)[0]
    stack.faults["healthz"] = True
    await stack.control.lifecycle.tick(datetime.now(timezone.utc))
    replacement = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert replacement is not None and replacement.instance_id != endpoint.instance_id
    assert (endpoint.name, endpoint.instance_id) in stack.host.deleted
    assert stack.workspace_restore_calls == [endpoint.instance_id, replacement.instance_id]
    ticket = _ticket(stack, "60000000-0000-4000-8000-000000000003")
    await asyncio.wait({ticket.completed}, timeout=15)
    assert ticket.completed.done() and "unknown" in str(ticket.completed.exception()).lower()
    assert outcomes == ["unknown"]
    assert len(stack.start_payloads) == 1, "an unknown run is never started again"
    await stack.release_model()


async def test_scenario_4_t01_in_place_restart_never_commits_over_the_good_snapshot(e2e_stack_factory):
    stack = await e2e_stack_factory(persistent=True)
    started, endpoint = await _first_run(stack)
    await asyncio.wait_for(stack.control.admission._tickets[started.invocation_id].completed, 5)
    (stack._workspace_roots[endpoint.ref.workspace_ref] / "good").write_text("committed baseline", encoding="utf-8")
    good = await _commit_baseline(stack, endpoint, started.session_ref)
    ref = _state_ref(stack, endpoint)
    assert await stack.control.storage.current_writer(ref) == endpoint.instance_id
    assert stack.control.lifecycle._tick_task is None, "T01 runs without a lifecycle tick"

    await stack.host.restart_in_place(endpoint.name)  # no notification to the runtime
    assert (await stack.post_event(
        event_type="note", text="after the silent restart", event_uuid="60000000-0000-4000-8000-000000000004",
        wait_started=False,
    )).status_code == 202
    # the delivery saw the new instance ID; recover ran and the source listed released, then running
    await _until(lambda: stack.host.started_count == 2, what="the replacement unit")
    await _until(
        lambda: (e := stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)) is not None
        and e.instance_id != endpoint.instance_id,
        what="re-activation",
    )
    replacement = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert (endpoint.name, endpoint.instance_id) in stack.host.deleted
    assert stack.workspace_restore_calls[-1] == replacement.instance_id
    # storage still holds the good snapshot: the empty instance was never committed over it
    current = await stack.control.storage.restore(ref)
    assert (current.snapshot_id, current.checksum) == (good.snapshot_id, good.checksum)
    assert await stack.control.storage.current_writer(ref) == replacement.instance_id
    assert (stack._workspace_roots[endpoint.ref.workspace_ref] / "good").read_text(encoding="utf-8") == "committed baseline"


async def test_t02_restart_during_a_transfer_is_a_loss_not_a_commit(e2e_stack_factory, anomalies):
    stack = await e2e_stack_factory(persistent=True)
    started, endpoint = await _first_run(stack)
    await asyncio.wait_for(stack.control.admission._tickets[started.invocation_id].completed, 5)
    (stack._workspace_roots[endpoint.ref.workspace_ref] / "good").write_text("committed baseline", encoding="utf-8")
    good = await _commit_baseline(stack, endpoint, started.session_ref)

    await stack.host.restart_in_place(endpoint.name)
    await stack.control.manager.close(endpoint.ref)  # the export meets the new instance ID

    assert stack.control.manager.active_endpoint(endpoint.ref.workspace_ref) is None
    assert anomalies.count("pod_unhealthy") == 1
    current = await stack.control.storage.restore(_state_ref(stack, endpoint))
    assert (current.snapshot_id, current.checksum) == (good.snapshot_id, good.checksum)
    assert (endpoint.name, endpoint.instance_id) in stack.host.deleted
    assert endpoint.ref.workspace_ref not in stack.control.manager._occupied


async def test_scenario_5_control_restart_adopts_and_recovers_a_running_run(e2e_stack_factory):
    stack = await e2e_stack_factory()
    await stack.upstream.control({"command": "hold"})
    event = "60000000-0000-4000-8000-000000000005"
    assert (await stack.post_event(event_uuid=event)).status_code == 202
    started = stack.started[0]
    endpoint = stack.current_session(started)[0]
    old_epoch = stack.control.source.epoch

    await stack.restart_control()

    assert stack.control.source.epoch != old_epoch
    first = stack.host.first_listing
    assert first["adopting"] is True and first["workspaces"] == [], "an unlisted unit is not desired state"
    assert stack.closed_instances == [], "no unit was removed while adopting"
    adopted = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert adopted is not None and adopted.instance_id == endpoint.instance_id
    assert stack.host.started_count == 0
    assert stack.control.admission.recovered_run(started.invocation_id) is not None
    await stack.release_model()
    async with asyncio.timeout(20):
        while (await stack.control.delivery.status(adopted, started.invocation_id)).state != "completed":
            await asyncio.sleep(0.05)
    assert len(stack.start_payloads) == 1, "no duplicate Start"
    assert len((await stack.captured())["mcpCalls"]) == 1


def _failing_probe_app() -> FastAPI:
    """A unit whose probe is rejected outright (not retried): it answers 403."""
    app = FastAPI()

    async def close() -> None:
        return None

    app.state.service = SimpleNamespace(close=close)
    app.state.instance_id = "stub-instance"

    @app.get("/healthz")
    async def healthz():
        return Response(status_code=403)

    return app


async def test_scenario_5_t03_mixed_inventory_with_one_failure(e2e_stack_factory):
    stack = await e2e_stack_factory(max_active_workspaces=2)
    _, good = await _first_run(stack)
    _, bad = await _first_run(stack, project_id=2, project_path="team/other", iid=4)
    stack.units.apps[stack.units.host_of(bad.name)] = _failing_probe_app()

    await stack.restart_control(configure_host=lambda host: host.withhold_delete(bad.name))

    # readiness did not wait for the bad unit's DELETE, and no timeout was involved
    assert stack.ready_after_discovery
    assert stack.control.source.adopting is False
    listed = _listed(stack)
    assert listed[bad.name]["desired"] == "released"
    assert listed[good.name]["desired"] == "running"
    assert stack.control.manager.active_endpoint(good.ref.workspace_ref).instance_id == good.instance_id
    assert stack.control.manager.active_endpoint(bad.ref.workspace_ref) is None
    assert bad.ref.workspace_ref in stack.control.manager._occupied, "the slot stays held until the DELETE"
    await stack.host.release_delete(bad.name)
    await _until(lambda: bad.name not in _listed(stack), what="the bad unit's entry to go")
    await _until(lambda: bad.ref.workspace_ref not in stack.control.manager._occupied, what="its slot")


async def test_scenario_6_host_never_registers(e2e_stack_factory, anomalies):
    stack = await e2e_stack_factory(max_invocation_seconds=5)
    stack.host.withhold_registration("*")
    event = "60000000-0000-4000-8000-000000000006"
    assert (await stack.post_event(event_uuid=event, wait_started=False)).status_code == 202
    ticket = _ticket(stack, event)
    done, _ = await asyncio.wait({ticket.started}, timeout=15)
    assert done and ticket.started.exception() is not None, "the invocation fails at its deadline"
    assert "workspace_unhosted" in anomalies
    assert _listed(stack) == {}, "the entry is gone"
    assert stack.control.manager._occupied == set()
    assert stack.start_payloads == []


async def test_scenario_7_t07_host_never_deletes_on_close(e2e_stack_factory, anomalies):
    stack = await e2e_stack_factory(shutdown_timeout_seconds=1)
    started, endpoint = await _first_run(stack)
    await asyncio.wait_for(stack.control.admission._tickets[started.invocation_id].completed, 5)
    stack.host.withhold_delete(endpoint.name)
    await stack.control.manager.close(endpoint.ref)  # returns once the bounded wait ends
    assert "workspace_retire_timeout" in anomalies
    assert endpoint.ref.workspace_ref in stack.control.manager._occupied, "the slot is kept"
    assert isinstance(stack.control.manager._errors[endpoint.ref.workspace_ref], WorkspaceRetireTimeout)
    assert _listed(stack)[endpoint.name]["desired"] == "released", "the entry is still listed"

    await stack.host.release_delete(endpoint.name)
    await _until(lambda: endpoint.ref.workspace_ref not in stack.control.manager._occupied, what="the slot to free")
    assert endpoint.ref.workspace_ref not in stack.control.manager._errors
    # T07: a second DELETE frees nothing
    before = set(stack.control.manager._occupied)
    await stack.host._delete(endpoint.name, endpoint.instance_id)
    assert stack.control.manager._occupied == before
    assert _listed(stack) == {}


async def test_scenario_8_stale_fencing_with_a_dishonest_host(e2e_stack_factory):
    stack = await e2e_stack_factory(persistent=True)
    started, endpoint = await _first_run(stack)
    await asyncio.wait_for(stack.control.admission._tickets[started.invocation_id].completed, 5)
    (stack._workspace_roots[endpoint.ref.workspace_ref] / "good").write_text("committed baseline", encoding="utf-8")
    good = await _commit_baseline(stack, endpoint, started.session_ref)
    (stack._workspace_roots[endpoint.ref.workspace_ref] / "stale").write_text("stale writes", encoding="utf-8")
    ref = _state_ref(stack, endpoint)

    stack.host.dishonest = True  # on `released` it sends the DELETE but keeps the old process alive
    stack.faults["healthz"] = True
    await stack.control.lifecycle.tick(datetime.now(timezone.utc))
    replacement = stack.control.manager.active_endpoint(endpoint.ref.workspace_ref)
    assert replacement is not None and replacement.instance_id != endpoint.instance_id
    zombie_host = stack.host.zombie(endpoint.name)
    assert endpoint.instance_id not in stack.closed_instances, "the old process is still alive"
    assert await stack.control.storage.current_writer(ref) == replacement.instance_id

    # The zombie is driven directly and commits with the lease it held: storage rejects it.
    zombie = replace(endpoint, base_url=f"http://{zombie_host}")
    with pytest.raises(WriterFenced):
        await stack.control.lifecycle.commit_workspace(zombie)
    current = await stack.control.storage.restore(ref)
    assert (current.snapshot_id, current.checksum) == (good.snapshot_id, good.checksum)
    assert await stack.control.storage.current_writer(ref) == replacement.instance_id


async def test_t05_late_delete_leaves_the_new_unit_listed(e2e_stack_factory):
    stack = await e2e_stack_factory()
    started, old = await _first_run(stack)
    stack.faults["healthz"] = True
    await stack.control.lifecycle.tick(datetime.now(timezone.utc))
    new = stack.control.manager.active_endpoint(old.ref.workspace_ref)
    assert new.instance_id != old.instance_id
    client = stack.host.client
    for params in ({}, {"instanceId": "not-an-instance"}, {"instanceId": old.instance_id}):
        assert (await client.delete(f"/workspaces/{new.name}/instance", params=params)).status_code == 204
        listed = _listed(stack)[new.name]
        assert listed["desired"] == "running"
        assert listed["instance"]["instanceId"] == new.instance_id
        assert stack.control.manager.active_endpoint(new.ref.workspace_ref) == new
    assert (await stack.post_event(event_type="note", text="still served", event_uuid="60000000-0000-4000-8000-000000000009")).status_code == 202
    assert (await _settle(stack, stack.started[-1])).state == "completed"


async def test_t06_late_put_with_another_url_keeps_the_unit_serving(e2e_stack_factory):
    stack = await e2e_stack_factory()
    _, endpoint = await _first_run(stack)
    response = await stack.host.client.put(
        f"/workspaces/{endpoint.name}/instance", json={"baseUrl": "http://elsewhere.units"}
    )
    assert (response.status_code, response.json()) == (409, {"error": "registered"})
    listed = _listed(stack)[endpoint.name]["instance"]
    assert (listed["baseUrl"], listed["state"]) == (endpoint.base_url, "serving")
    assert stack.control.manager.active_endpoint(endpoint.ref.workspace_ref) == endpoint


async def test_t08_t09_kill_mid_run_without_a_report_ends_unknown(e2e_stack_factory, monkeypatch):
    stack = await e2e_stack_factory()
    outcomes: list[str] = []
    record_run = type(stack.control)._record_run

    def observe(self, expected, status, started_run_id, event, *, outcome=None):
        outcomes.append(outcome or (status.state if status is not None else "unknown"))
        record_run(self, expected, status, started_run_id, event, outcome=outcome)

    monkeypatch.setattr(type(stack.control), "_record_run", observe)
    await stack.upstream.control({"command": "hold"})
    event = "60000000-0000-4000-8000-00000000000a"
    assert (await stack.post_event(event_uuid=event)).status_code == 202
    endpoint = stack.current_session(stack.started[0])[0]
    await stack.host.kill(endpoint.name, report=False)
    await stack.control.lifecycle.tick(datetime.now(timezone.utc))  # the health check finds it gone
    ticket = _ticket(stack, event)
    await asyncio.wait({ticket.completed}, timeout=15)
    assert ticket.completed.done() and "unknown" in str(ticket.completed.exception()).lower()
    assert outcomes == ["unknown"]
    assert len(stack.start_payloads) == 1
    await stack.release_model()


async def test_flapping_instance_recovers_one_at_a_time(e2e_stack_factory, anomalies):
    stack = await e2e_stack_factory()
    _, endpoint = await _first_run(stack)
    manager = stack.control.manager
    running = peak = 0
    do_recover = manager._do_recover

    async def tracked(*args):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            return await do_recover(*args)
        finally:
            running -= 1

    manager._do_recover = tracked
    current = endpoint
    seen = {current.instance_id}
    for _ in range(3):
        await stack.host.restart_in_place(current.name)
        # two requests meet the new instance ID at once: one recovery, not two
        await asyncio.gather(
            *(stack.control._mini_json(current, "GET", "/readyz") for _ in range(2)), return_exceptions=True
        )
        await _until(
            lambda: (e := manager.active_endpoint(current.ref.workspace_ref)) is not None
            and e.instance_id not in seen,
            what="the replacement",
        )
        current = manager.active_endpoint(current.ref.workspace_ref)
        seen.add(current.instance_id)
    assert len(seen) == 4
    assert peak == 1
    assert anomalies.count("pod_unhealthy") == 3


async def test_t10_stuck_retirement_does_not_delay_another_workspace(e2e_stack_factory):
    stack = await e2e_stack_factory(max_active_workspaces=2)
    started_a, a = await _first_run(stack)
    await asyncio.wait_for(stack.control.admission._tickets[started_a.invocation_id].completed, 5)
    stack.host.withhold_delete(a.name)
    closing = asyncio.create_task(stack.control.manager.close(a.ref))
    await _until(lambda: a.instance_id in stack.closed_instances, what="A's unit to stop")
    assert (await stack.post_event(
        project_id=2, project_path="team/other", iid=4, event_uuid="60000000-0000-4000-8000-00000000000b"
    )).status_code == 202
    assert (await _settle(stack, stack.started[-1])).state == "completed"
    assert not closing.done() and a.ref.workspace_ref in stack.control.manager._occupied
    await stack.host.release_delete(a.name)
    await asyncio.wait_for(closing, 10)
    assert a.ref.workspace_ref not in stack.control.manager._occupied

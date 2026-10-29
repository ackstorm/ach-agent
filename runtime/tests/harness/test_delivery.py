from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ach_runtime.execution.wire import CancelRequest, RunStatus, Started, StartRequest
from ach_runtime.harness.admission import AdmissionTicket
from ach_runtime.harness.delivery import Delivery, DeliveryError, GatewayUnavailable, StartOutcomeUnknown
from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceManager, WorkspaceRef
from fake_source import FakeSource


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)
REF = "a" * 64
UID = "instance-1"


def pod_response(status_code, **kwargs):
    return httpx.Response(status_code, headers={"X-ACH-Instance-Id": UID}, **kwargs)


class Manager:
    def __init__(self, agent_uid, endpoint=None):
        self.identity = agent_uid
        self.config = SimpleNamespace(agent=SimpleNamespace(uid=agent_uid))
        self.endpoint = endpoint or WorkspaceEndpoint(
            WorkspaceRef("ach", self.identity, REF), "ach-ws-test", UID, "http://workspace"
        )
        self.ensure_deadlines: list[datetime | None] = []
        self.lost: list[WorkspaceEndpoint] = []

    def gateway_available(self):
        return True

    def report_lost(self, endpoint):
        self.lost.append(endpoint)

    async def ensure(self, ref, *, deadline_at=None):
        self.ensure_deadlines.append(deadline_at)
        return self.endpoint

    async def current(self, ref):
        return self.endpoint


def ticket(*, seconds=30):
    loop = asyncio.get_running_loop()
    return AdmissionTicket(
        "inv-1", "ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", REF, "session-ref", None,
        NOW + timedelta(seconds=seconds), loop.create_future(), loop.create_future(),
    )


def request(t, version):
    return StartRequest(
        workspaceKey="wk", sessionKey="sk",
        invocationId=t.invocation_id, channel="pr-review", workspaceRef=t.workspace_ref,
        sessionRef=t.session_ref, configVersion=version, prompt="do work",
        deadlineAt=t.deadline_at,
    )


def status(t, state="preparing", run_id="run-1", version=None):
    return RunStatus(
        invocationId=t.invocation_id, channel="pr-review", workspaceRef=t.workspace_ref,
        sessionRef=t.session_ref, runId=run_id, configVersion=version or "b" * 64,
        deadlineAt=t.deadline_at, state=state,
    )


@pytest.fixture
def public_config_fixture():
    from ach_runtime.config.public import public_config
    from ach_runtime.config.schema import RuntimeConfig

    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    runtime_config = RuntimeConfig.model_validate(wire)
    return public_config(runtime_config, engine_env=runtime_config.engine.env)


def make_delivery(handler, public, *, prepare=None, alarm=None, clock=lambda: NOW, manager=None):
    transport = httpx.MockTransport(handler)
    manager = manager or Manager(public.agent.uid)
    return Delivery(
        manager, key=b"k" * 64, public=public, prepare=prepare or (lambda ticket, ep: _ready(ticket, public.config_version)),
        clock=clock, alarm=alarm or (lambda reason, ticket: None), transport=transport,
    )


async def _ready(t, version):
    return request(t, version)


async def test_engine_acceptance_is_only_ack(public_config_fixture):
    accepted = asyncio.Event()
    entered = asyncio.Event()
    sends = 0
    t = ticket()

    async def handler(req):
        nonlocal sends
        if req.url.path == "/start":
            sends += 1
            entered.set()
            await accepted.wait()
            body = await req.aread()
            payload = json.loads(body)
            return pod_response(202, json={"invocationId": payload["invocationId"], "sessionRef": payload["sessionRef"], "runId": "run-1", "configVersion": payload["configVersion"]})
        raise AssertionError(req.url)

    d = make_delivery(handler, public_config_fixture)
    task = asyncio.create_task(d.start(t))
    await entered.wait()
    assert not t.started.done()
    accepted.set()
    started = await task
    assert started.run_id == "run-1"
    assert not t.started.done()  # ACK signaling belongs to Admission runner.
    assert sends == 1
    await d.close()


async def test_lost_reply_recovers_cached_started_same_uid(public_config_fixture):
    t = ticket()
    sent = 0
    calls = []

    async def handler(req):
        nonlocal sent
        calls.append(req.url.path)
        if req.url.path == "/start":
            sent += 1
            if sent == 1:
                raise httpx.ReadTimeout("reply lost", request=req)
            payload = json.loads(await req.aread())
            return pod_response(202, json={"invocationId": payload["invocationId"], "sessionRef": payload["sessionRef"], "runId": "run-1", "configVersion": payload["configVersion"]})
        if req.url.path == "/status/inv-1":
            return pod_response(200, json=status(t, "running", version=public_config_fixture.config_version).model_dump(by_alias=True, mode="json"))
        raise AssertionError(req.url)

    alarms = []
    d = make_delivery(handler, public_config_fixture, alarm=lambda *args: alarms.append(args))
    got = await d.start(t)
    assert got == Started(invocationId="inv-1", sessionRef="session-ref", runId="run-1", configVersion=public_config_fixture.config_version)
    assert sent == 2
    assert calls == ["/start", "/status/inv-1", "/start"]
    assert not alarms
    await d.close()


async def test_all_identity_fields_and_receipt_validated(public_config_fixture):
    t = ticket()
    sends = 0

    async def handler(req):
        nonlocal sends
        sends += 1
        return pod_response(202, json={"invocationId": "other", "sessionRef": "session-ref", "runId": "run", "configVersion": public_config_fixture.config_version})

    alarms = []
    d = make_delivery(handler, public_config_fixture, alarm=lambda *args: alarms.append(args))
    with pytest.raises(StartOutcomeUnknown):
        await d.start(t)
    assert len(alarms) == 1
    assert sends == 1
    await d.close()


async def test_non_202_is_a_definite_rejection(public_config_fixture):
    alarms = []

    async def handler(req):
        return pod_response(409, json={"detail": "rejected"})

    d = make_delivery(handler, public_config_fixture, alarm=lambda *args: alarms.append(args))
    with pytest.raises(DeliveryError, match="HTTP 409"):
        await d.start(ticket())
    assert not alarms
    await d.close()


async def test_malformed_202_receipt_is_unknown_without_retry_or_ack(public_config_fixture):
    sends = 0
    alarms = []

    async def handler(req):
        nonlocal sends
        sends += 1
        return pod_response(202, json={"invocationId": "inv-1"})

    t = ticket()
    d = make_delivery(handler, public_config_fixture, alarm=lambda *args: alarms.append(args))
    with pytest.raises(StartOutcomeUnknown):
        await d.start(t)
    assert sends == 1
    assert len(alarms) == 1
    assert not t.started.done()
    await d.close()


async def test_actual_workspace_manager_constructor_and_identity_precheck(public_config_fixture):
    from ach_runtime.config.schema import RuntimeConfig

    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    runtime_config = RuntimeConfig.model_validate(wire)

    async def noop(_endpoint):
        return None

    manager = WorkspaceManager(
        runtime_config, FakeSource(), restore_workspace=noop,
        commit_workspace=noop, reconnect_workspace=noop,
    )
    ensured = False

    async def ensure(ref, *, deadline_at=None):
        nonlocal ensured
        ensured = True
        return WorkspaceEndpoint(ref, "ach-ws-test", UID, "http://workspace")

    manager.ensure = ensure
    d = make_delivery(lambda req: pod_response(500), public_config_fixture, manager=manager)
    invalid = replace(ticket(), namespace="other")
    with pytest.raises(DeliveryError, match="ticket identity"):
        await d.start(invalid)
    assert not ensured
    await d.close()


async def test_public_config_replacement_during_prepare_uses_captured_version(public_config_fixture):
    t = ticket()
    alternate = public_config_fixture.model_copy(update={"config_version": "c" * 64})
    sent_versions = []
    holder = {}

    async def prepare(ticket, endpoint):
        holder["delivery"].public = alternate
        return request(ticket, public_config_fixture.config_version)

    async def handler(req):
        payload = json.loads(await req.aread())
        sent_versions.append(payload["configVersion"])
        return pod_response(202, json={"invocationId": payload["invocationId"], "sessionRef": payload["sessionRef"], "runId": "run-1", "configVersion": payload["configVersion"]})

    d = make_delivery(handler, public_config_fixture, prepare=prepare)
    holder["delivery"] = d
    await d.start(t)
    assert sent_versions == [public_config_fixture.config_version]
    await d.close()


async def test_start_body_consumption_uses_original_timeout_and_closes_response(public_config_fixture):
    base = datetime.now(timezone.utc)
    t = replace(ticket(), deadline_at=base + timedelta(milliseconds=30))
    closed = asyncio.Event()

    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"invocationId":"inv-1",'
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    async def handler(req):
        if req.url.path.startswith("/status/"):
            return pod_response(404)
        return httpx.Response(202, headers={"X-ACH-Instance-Id": UID}, stream=SlowBody())

    alarms = []
    d = make_delivery(handler, public_config_fixture, alarm=lambda *args: alarms.append(args), clock=lambda: base)
    with pytest.raises(StartOutcomeUnknown):
        await d.start(t)
    assert closed.is_set()
    assert len(alarms) == 1
    await d.close()


async def test_run_id_before_acceptance_is_not_started(public_config_fixture):
    t = ticket()

    async def handler(req):
        if req.url.path == "/start":
            raise httpx.ReadTimeout("lost", request=req)
        return pod_response(200, json=status(t, "preparing", "run-1", public_config_fixture.config_version).model_dump(by_alias=True, mode="json"))

    alarms = []
    d = make_delivery(handler, public_config_fixture, alarm=lambda *args: alarms.append(args))
    with pytest.raises(StartOutcomeUnknown):
        await d.start(t)
    assert not t.started.done()
    assert alarms
    await d.close()


async def test_changed_uid_or_unknown_never_replays(public_config_fixture):
    t = ticket()
    sent = 0
    async def handler(req):
        nonlocal sent
        sent += req.url.path == "/start"
        if req.url.path == "/start":
            raise httpx.ReadTimeout("lost", request=req)
        return pod_response(404)

    alarms = []
    d = make_delivery(handler, public_config_fixture, alarm=lambda *args: alarms.append(args), manager=Manager(public_config_fixture.agent.uid))
    with pytest.raises(StartOutcomeUnknown):
        await d.start(t)
    assert sent == 1
    assert alarms
    await d.close()


async def test_t01_a_response_from_another_instance_reports_the_loss_once(public_config_fixture):
    manager = Manager(public_config_fixture.agent.uid)

    async def handler(req):
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "restarted-unit"}, json=[])

    d = make_delivery(handler, public_config_fixture, manager=manager)
    with pytest.raises(DeliveryError, match="instance ID"):
        await d.list_runs(manager.endpoint)
    assert manager.lost == [manager.endpoint]
    await d.close()


async def test_t02_a_start_answered_by_another_instance_is_unknown_and_reports_the_loss(public_config_fixture):
    t = ticket()
    manager = Manager(public_config_fixture.agent.uid)
    sent = 0

    async def handler(req):
        nonlocal sent
        sent += req.url.path == "/start"
        return httpx.Response(202, headers={"X-ACH-Instance-Id": "restarted-unit"}, json={})

    d = make_delivery(handler, public_config_fixture, manager=manager)
    with pytest.raises(StartOutcomeUnknown):
        await d.start(t)
    assert sent == 1, "the Start is never replayed"
    assert manager.lost == [manager.endpoint]
    await d.close()


async def test_status_auth_failure_is_not_owner_death(public_config_fixture):
    t = ticket()
    calls = []

    async def handler(req):
        calls.append(req.url.path)
        return pod_response(401)

    d = make_delivery(handler, public_config_fixture)
    with pytest.raises(Exception):
        await d.status(Manager(public_config_fixture.agent.uid).endpoint, "inv-1")
    assert calls == ["/status/inv-1"]
    await d.close()


async def test_every_stage_consumes_original_deadline(public_config_fixture):
    t = ticket(seconds=1)
    observed = []
    manager = Manager(public_config_fixture.agent.uid)

    async def prepare(t, endpoint):
        observed.append(t.deadline_at)
        return request(t, public_config_fixture.config_version)

    async def handler(req):
        payload = json.loads(await req.aread())
        return pod_response(202, json={"invocationId": payload["invocationId"], "sessionRef": payload["sessionRef"], "runId": "run-1", "configVersion": payload["configVersion"]})

    d = make_delivery(handler, public_config_fixture, prepare=prepare, clock=lambda: NOW, manager=manager)
    await d.start(t)
    assert observed == [t.deadline_at]
    # I4: Delivery passes the ticket's own original deadline into every ensure() call --
    # never a fresh/renewed bound of its own.
    assert manager.ensure_deadlines == [t.deadline_at]
    await d.close()


async def test_missing_or_failed_preparation_prevents_start(public_config_fixture):
    t = ticket()
    sends = 0

    async def handler(req):
        nonlocal sends
        sends += 1
        return pod_response(202, json={})

    async def prepare(_t, _endpoint):
        raise RuntimeError("preparation unavailable")

    d = make_delivery(handler, public_config_fixture, prepare=prepare)
    with pytest.raises(RuntimeError):
        await d.start(t)
    assert sends == 0
    await d.close()


async def test_status_cancel_remain_available_during_paused_receive(public_config_fixture):
    t = ticket()
    entered = asyncio.Event()
    release = asyncio.Event()
    closed = []

    async def handler(req):
        if req.url.path == "/start":
            entered.set()
            await release.wait()
            return pod_response(202, json={"invocationId": "inv-1", "sessionRef": "session-ref", "runId": "run-1", "configVersion": public_config_fixture.config_version})
        if req.url.path == "/status/inv-1":
            return pod_response(200, json=status(t, "running", version=public_config_fixture.config_version).model_dump(by_alias=True, mode="json"))
        if req.url.path == "/cancel":
            return pod_response(200, json={"status": "cancelled"})
        raise AssertionError(req.url)

    d = make_delivery(handler, public_config_fixture)
    start_task = asyncio.create_task(d.start(t))
    await entered.wait()
    manager = Manager(public_config_fixture.agent.uid)
    assert (await d.status(manager.endpoint, "inv-1")).state == "running"
    await d.cancel(manager.endpoint, CancelRequest(invocationId="inv-1", reason="stop"))
    release.set()
    await start_task
    await d.close()


async def test_restart_lists_owned_runs_without_prompt(public_config_fixture):
    t = ticket()

    async def handler(req):
        assert req.url.path == "/runs"
        return pod_response(200, json=[status(t, "completed", version=public_config_fixture.config_version).model_dump(by_alias=True, mode="json")])

    d = make_delivery(handler, public_config_fixture)
    runs = await d.list_runs(Manager(public_config_fixture.agent.uid).endpoint)
    assert runs == [status(t, "completed", version=public_config_fixture.config_version)]
    await d.close()


async def test_recovered_delivery_status_validation(public_config_fixture):
    from ach_runtime.execution.wire import StartRequest

    expected = StartRequest(
        workspaceKey="wk", sessionKey="sk",
        invocationId="inv-1", channel="pr-review", workspaceRef=REF, sessionRef="session-ref",
        configVersion="f" * 64, prompt="unused", deadlineAt=NOW + timedelta(seconds=30),
    )
    valid = RunStatus(
        invocationId=expected.invocation_id, channel=expected.channel, workspaceRef=expected.workspace_ref,
        sessionRef=expected.session_ref, runId="run-1", configVersion=expected.config_version,
        deadlineAt=expected.deadline_at, state="running",
    )
    assert Delivery._validate_status(valid, expected) is None
    assert Delivery._validate_status(valid, valid) is None

    for field, value in (
        ("invocation_id", "other"), ("channel", "other"), ("workspace_ref", "other"),
        ("session_ref", "other"), ("config_version", "e" * 64),
        ("deadline_at", NOW + timedelta(seconds=31)),
    ):
        with pytest.raises(DeliveryError, match="RunStatus identity"):
            Delivery._validate_status(valid.model_copy(update={field: value}), expected)

    pinned = valid.model_copy(update={"run_id": "run-pinned"})
    with pytest.raises(DeliveryError, match="RunStatus identity"):
        Delivery._validate_status(valid, pinned)


def _real_clock():
    started = time.monotonic()
    return lambda: NOW + timedelta(seconds=time.monotonic() - started)


async def test_connect_failure_is_retried_until_it_succeeds(public_config_fixture):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            raise httpx.ConnectError("refused", request=request)
        payload = json.loads(request.content)
        return pod_response(202, json={"invocationId": payload["invocationId"], "sessionRef": payload["sessionRef"],
                                       "runId": "run-1", "configVersion": payload["configVersion"]})

    delivery = make_delivery(handler, public_config_fixture, clock=_real_clock())
    started = await delivery.start(ticket(seconds=30))
    assert calls == ["/start", "/start", "/start"]
    assert started.run_id == "run-1"


async def test_connect_failure_until_deadline_fails_without_replay(public_config_fixture):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("refused", request=request)

    manager = Manager(public_config_fixture.agent.uid)
    manager.gateway_available = lambda: False
    delivery = make_delivery(handler, public_config_fixture, clock=_real_clock(), manager=manager)
    with pytest.raises(GatewayUnavailable, match="gateway_unavailable"):
        await delivery.start(ticket(seconds=2))
    assert set(calls) == {"/start"}

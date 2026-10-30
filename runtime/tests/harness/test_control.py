from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.public import PublicConfig
from ach_runtime.config.schema import ScriptBlock, parse_config
from ach_runtime.execution.wire import RunStatus, StartRequest, Started
from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.channels.routing import route
from ach_runtime.harness.admission import AdmissionTicket, ScriptAdmissionTicket
from ach_runtime import __version__
from ach_runtime.harness.control import Control, _render_prompt, assemble
from ach_runtime.harness.delivery import Delivery
from ach_runtime.harness.tokens import facade_token, host_token
from ach_runtime.harness.workspaces import (
    WorkspaceAssemblyUnavailable, WorkspaceEndpoint, WorkspaceRef,
)
from fake_host import FakeHost


def _config(*, handoff: bool = False, hooks: dict | None = None, persistence: bool = False):
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"]["enabled"] = persistence
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    channel = wire["channels"][0]
    channel["prompt"] = "Review {{ payload.title | default(\"untitled\") }} for {{ internal.agent.name }}"
    if not handoff:
        channel.pop("handoff", None)
    if hooks:
        wire["hooks"].update(hooks)
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def _a2a_config():
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["channels"] = [{
        "name": "a2a-review", "type": "a2a", "a2a": {"auth": {"env": "A2A_TOKEN"}},
    }]
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def _catalog_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/platform/hydrate":
        return httpx.Response(200, json={
            "environment": "engineering-prod",
            "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                        "mcpServers": [], "a2aAgents": []},
            "context": {"skills": [], "prompts": [], "artifacts": []},
        })
    if request.url.path == "/v2/model/info":
        return httpx.Response(200, json={"data": []})
    raise AssertionError(request.url)


def _publish(control, endpoint: WorkspaceEndpoint) -> None:
    """White-box: serve `endpoint` as the Workspace's published owner without an activation."""
    control.manager._active[endpoint.ref.workspace_ref] = endpoint


def _unpublish(control, workspace_ref: str) -> None:
    control.manager._active.pop(workspace_ref, None)


def _healthy(instance_id: str = "pod-1", **headers) -> httpx.Response:
    """What a unit of this release answers on /healthz."""
    return httpx.Response(
        200, headers={"X-ACH-Instance-Id": instance_id, **headers}, json={"status": "ok", "release": __version__}
    )


async def _start_host(control, unit_url: str = "http://mini") -> tuple[FakeHost, asyncio.Task]:
    """A host that starts every unit at `unit_url` (the test's mini transport answers there)."""
    async def start(name: str, launch: dict) -> str:
        return unit_url

    async def stop(name: str) -> None:
        return None

    host = FakeHost(control.source, host_token(control.key, control.config.agent.uid), start, stop)
    task = asyncio.create_task(host.run())
    async with asyncio.timeout(5):
        await control.manager.adopt()
    return host, task


async def _stop_host(host: FakeHost, task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await host.aclose()


async def test_prompt_renderer_preserves_legacy_filters_and_rejects_template_helpers() -> None:
    context = {"payload": {"items": ["a", "b"]}, "internal": {"agent": {"name": "reviewer"}}}
    assert _render_prompt(
        '{{ payload.items | json }} {{ payload.missing | default("none") }} {{ internal.agent.name }}', context
    ) == '["a", "b"] none reviewer'
    with pytest.raises(WorkspaceAssemblyUnavailable, match="unsupported prompt template"):
        _render_prompt("{{ payload.value | upper }}", context)


def test_harness_completion_record_carries_normal_and_recovered_correlation(monkeypatch) -> None:
    import ach_runtime.observability as observability
    from ach_runtime.config.public import public_config
    from ach_runtime.execution.wire import RunStatus

    config = _config()
    control = Control.__new__(Control)
    control.config = config
    control.public = public_config(config, config.engine.env)
    records = []
    monkeypatch.setattr(observability, "record_run", records.append)
    status = RunStatus(
        invocationId="inv-opaque", channel="gitlab-mr-review", workspaceRef="a" * 64,
        sessionRef="b" * 64, runId="run-opaque", configVersion=config.config_version,
        deadlineAt=datetime.now(timezone.utc), state="completed", result={"reporting": {
            "duration_ms": 12.0,
            "usage": {"source": "opencode", "basis": "engine_estimate", "input": 0,
                      "output": 2, "reasoning": 1, "cache_read": 0, "cache_write": 0,
                      "cost_usd": 0.25, "steps": 1},
        }},
    )
    event = Event(event_id="evt-opaque", channel=status.channel, type="merge_request",
                  source="gitlab", actor="actor-opaque")
    control._record_run(status, status, status.run_id, event)
    control._record_run(status, status, None, None)
    normal, recovered = records
    assert normal.actor == "actor-opaque" and normal.event_id == "evt-opaque"
    assert recovered.actor is None and recovered.event_id is None
    assert normal.usage.to_dict()["cost_usd"] == 0.25


async def test_prepare_requires_real_configure_receipt_before_returning_start_request() -> None:
    seen: list[tuple[str, dict | None]] = []

    async def mini(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content) if request.content else None
        seen.append((request.url.path, payload))
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.headers["x-ach-instance-id"] == "pod-1"
        if request.url.path == "/configure":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": payload["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"ready": True})
        raise AssertionError(f"unexpected mini request {request.url}")

    control = await assemble(
        _config(), environ={"ACH_SANDBOX_KEY": "utf8-secret", "ACH_SECRET_IDENTITY": "ek-value"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    routed = RoutedEvent(Event(event_id="delivery-1", channel="gitlab-mr-review", type="merge_request",
                               source="gitlab", payload={"title": "fix"}), "gitlab:project:1", "mr:4")
    ticket = AdmissionTicket(
        "inv-1", control.config.agent.namespace, control.config.agent.uid, "a" * 64, "b" * 64,
        routed, datetime.now(timezone.utc), asyncio.get_running_loop().create_future(),
        asyncio.get_running_loop().create_future(),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, "a" * 64),
        "ach-workspace", "pod-1", "http://mini",
    )
    try:
        request = await control.prepare(ticket, endpoint)
        assert request.config_version == control.public.config_version
        assert request.prompt == (
            "Review fix for gitlab-reviewer\n\n## Memory\n\nUnavailable (no endpoint)."
        )
        assert request.memory_available is False
        assert "<output_format>" not in request.prompt
        assert [path for path, _ in seen] == ["/configure", "/readyz"]
        expected_token = facade_token(control.key, endpoint.name)
        assert seen[0][1]["catalog"]["providers"]["gemini"]["baseURL"] == (
            f"{control.public.infrastructure.facade_endpoint}/model/t/{expected_token}"
        )
    finally:
        await control.close()


@pytest.mark.parametrize("state", ["completed", "skipped"])
async def test_observe_run_returns_the_validated_terminal_status(state: str) -> None:
    config = _config()

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path in {"/readyz", "/runs"}:
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json=[])
        raise AssertionError(f"unexpected mini request {request.url}")

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=1)
    terminal = RunStatus(
        invocationId="observe-result", channel="gitlab-mr-review",
        workspaceRef="a" * 64, sessionRef="b" * 64, runId="run-1",
        configVersion=control.public.config_version, deadlineAt=deadline, state=state,
        result={"text": '{"action":"a2a_reply","text":"hello"}'},
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
        "workspace", "pod-1", "http://mini",
    )
    _publish(control, endpoint)
    control.delivery.status = lambda *_args: _return_status(terminal)
    try:
        assert await control._observe_run(endpoint, terminal) is terminal
    finally:
        await control.close()


def _observed_run(control, *, state: str = "running", invocation_id: str = "observe-run") -> RunStatus:
    return RunStatus(
        invocationId=invocation_id, channel="gitlab-mr-review",
        workspaceRef="a" * 64, sessionRef="b" * 64, runId="run-1",
        configVersion=control.public.config_version,
        deadlineAt=datetime.now(timezone.utc) + timedelta(minutes=1), state=state,
    )


async def test_t08_run_on_a_lost_instance_ends_unknown_and_is_never_started_again(monkeypatch) -> None:
    import ach_runtime.observability as observability
    from ach_runtime.harness.admission import WorkspaceOwnerLost

    config = _config()
    requests: list[str] = []

    def mini(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(503)

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    records = []
    monkeypatch.setattr(observability, "record_run", records.append)
    running = _observed_run(control)
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
        "workspace", "pod-1", "http://mini",
    )
    # Recovery found the instance lost: whether or not the Workspace was published again,
    # loss of access is not evidence that the run failed.
    monkeypatch.setattr(control.manager, "is_lost", lambda instance_id: instance_id == endpoint.instance_id)
    try:
        with pytest.raises(WorkspaceOwnerLost, match="lost"):
            await asyncio.wait_for(control._observe_run(endpoint, running), timeout=2)
        assert [record.outcome for record in records] == ["unknown"]
        assert "/start" not in requests
    finally:
        await control.close()


async def test_a_replacement_instance_also_ends_the_run_unknown(monkeypatch) -> None:
    import ach_runtime.observability as observability
    from ach_runtime.harness.admission import WorkspaceOwnerLost

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    records = []
    monkeypatch.setattr(observability, "record_run", records.append)
    ref = WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64)
    old = WorkspaceEndpoint(ref, "workspace", "pod-1", "http://mini")
    _publish(control, WorkspaceEndpoint(ref, "workspace", "pod-2", "http://mini"))
    try:
        with pytest.raises(WorkspaceOwnerLost):
            await asyncio.wait_for(control._observe_run(old, _observed_run(control)), timeout=2)
        assert [record.outcome for record in records] == ["unknown"]
    finally:
        await control.close()


async def test_t09_terminal_status_from_the_instance_is_kept_when_it_is_lost_in_the_same_iteration(
    monkeypatch,
) -> None:
    import ach_runtime.observability as observability

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    records = []
    monkeypatch.setattr(observability, "record_run", records.append)
    terminal = _observed_run(control, state="completed")
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
        "workspace", "pod-1", "http://mini",
    )
    _publish(control, endpoint)
    lost: set[str] = set()
    monkeypatch.setattr(control.manager, "is_lost", lambda instance_id: instance_id in lost)

    async def status(_endpoint, _invocation_id):
        # The response was verified against the instance's own ID, then the loss lands.
        lost.add(endpoint.instance_id)
        return terminal

    control.delivery.status = status
    try:
        assert await asyncio.wait_for(control._observe_run(endpoint, terminal), timeout=2) is terminal
        assert [record.outcome for record in records] == ["completed"]
    finally:
        await control.close()


@pytest.mark.parametrize("error", ["sessionStart exited 1: refused", "Workspace start returned HTTP 409"])
async def test_a_failed_terminal_status_still_records_failed(error, monkeypatch) -> None:
    import ach_runtime.observability as observability

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    records = []
    monkeypatch.setattr(observability, "record_run", records.append)
    failed = _observed_run(control, state="failed").model_copy(update={"error": error})
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
        "workspace", "pod-1", "http://mini",
    )
    _publish(control, endpoint)
    control.delivery.status = lambda *_args: _return_status(failed)
    try:
        assert await control._observe_run(endpoint, failed) is failed
        assert [record.outcome for record in records] == ["failed"]
    finally:
        await control.close()


async def test_owned_live_answers_from_the_manager_without_any_request(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"ownership must not do I/O: {request.method} {request.url}")

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(refuse), facade_transport=httpx.MockTransport(refuse),
    )

    def unit(ref: str, name: str, instance_id: str) -> WorkspaceEndpoint:
        return WorkspaceEndpoint(
            WorkspaceRef(config.agent.namespace, config.agent.uid, ref * 64), name, instance_id, "http://mini"
        )

    active, hook, lost = unit("a", "ws-active", "i-1"), unit("b", "ws-hook", "i-2"), unit("c", "ws-lost", "i-3")
    try:
        assert not hasattr(control, "kubernetes"), "Control owns no Kubernetes client"
        _publish(control, active)
        _publish(control, lost)
        control._hook_endpoints[hook.ref.workspace_ref] = hook
        monkeypatch.setattr(control.manager, "is_lost", lambda instance_id: instance_id == "i-3")
        assert await control._owned_live("ws-active")
        assert await control._owned_live("ws-hook"), "a Workspace in its hook phase is owned"
        assert not await control._owned_live("ws-lost")
        assert not await control._owned_live("ws-unknown")
    finally:
        await control.close()


async def test_a_response_from_another_instance_reports_the_loss_and_raises() -> None:
    config = _config()

    def mini(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "restarted"}, json={})

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64), "workspace", "pod-1", "http://mini",
    )
    reported: list[WorkspaceEndpoint] = []
    control.manager.report_lost = reported.append
    try:
        with pytest.raises(WorkspaceAssemblyUnavailable, match="instance ID"):
            await control._mini_json(endpoint, "GET", "/readyz")
        assert reported == [endpoint]
    finally:
        await control.close()


async def test_a_response_without_an_instance_id_is_an_error_not_a_loss() -> None:
    config = _config()

    def mini(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64), "workspace", "pod-1", "http://mini",
    )
    reported: list[WorkspaceEndpoint] = []
    control.manager.report_lost = reported.append
    try:
        with pytest.raises(WorkspaceAssemblyUnavailable, match="instance ID"):
            await control._mini_json(endpoint, "GET", "/status")
        assert reported == []
    finally:
        await control.close()


async def test_one_restore_per_activation_and_none_from_reconnect(monkeypatch) -> None:
    """Task 2's `_start` restores a fresh instance; reconnect then finds the storage writer is
    this instance and must not restore a second time."""
    from types import SimpleNamespace

    config = _config()
    monkeypatch.setattr("ach_runtime.harness.control.storage_from_env", lambda *_args: None)

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return _healthy()
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/runs":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json=[])
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    writer: list[str] = []
    restored: list[WorkspaceEndpoint] = []

    async def restore_workspace(endpoint) -> None:
        restored.append(endpoint)
        writer.append(endpoint.instance_id)  # what acquire_writer records

    async def current_writer(ref):
        return writer[-1] if writer else None

    control.config = _config(persistence=True)
    control.storage = SimpleNamespace(current_writer=current_writer)
    control.lifecycle = SimpleNamespace(
        workspace_state_ref=lambda endpoint: ("workspace", endpoint.ref.workspace_ref),
        restore_workspace=restore_workspace,
    )
    control.manager.bind_callbacks(restore=restore_workspace)
    host, task = await _start_host(control)
    try:
        endpoint = await control.manager.ensure(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64))
        assert restored == [endpoint]
    finally:
        await _stop_host(host, task)
        control.lifecycle = None
        await control.close()


async def test_a2a_prepare_adds_legacy_directive_only_to_a2a_prompt() -> None:
    from ach_runtime.channels.a2a import A2A_OUTPUT_INSTRUCTIONS

    config = _a2a_config()

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"ready": True})
        raise AssertionError(f"unexpected mini request {request.url}")

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    event = Event(
        event_id="a2a:task-1", channel="a2a-review", type="a2a", source="a2a",
        payload={"text": "hello"}, context={"a2a": {"taskId": "task-1", "contextId": "ctx"}},
    )
    routed = route(event, config.channels[0])
    ticket = AdmissionTicket(
        "inv-a2a", config.agent.namespace, config.agent.uid, "a" * 64, "b" * 64, routed,
        datetime.now(timezone.utc) + timedelta(minutes=1),
        asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future(),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
        "workspace", "pod-1", "http://mini",
    )
    try:
        request = await control.prepare(ticket, endpoint)
        assert request.prompt == (
            "hello\n\n## Memory\n\nUnavailable (no endpoint).\n\n" + A2A_OUTPUT_INSTRUCTIONS
        )
    finally:
        await control.close()


async def test_real_admission_and_control_run_forward_terminal_status() -> None:
    config = _a2a_config()

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path in {"/configure", "/readyz", "/runs"}:
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json=[])
        raise AssertionError(f"unexpected mini request {request.url}")

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    config_version = control.public.config_version
    terminal_result = {"text": '{"action":"a2a_reply","text":"through-control"}'}

    async def start(ticket):
        endpoint = WorkspaceEndpoint(
            WorkspaceRef(config.agent.namespace, config.agent.uid, ticket.workspace_ref),
            "workspace", "pod-1", "http://mini",
        )
        _publish(control, endpoint)
        request = StartRequest(
            workspaceKey="wk", sessionKey="sk",
            invocationId=ticket.invocation_id, channel=ticket.routed_event.channel,
            workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref,
            configVersion=config_version, prompt="a2a prompt", deadlineAt=ticket.deadline_at,
        )
        control._prepared[ticket.invocation_id] = (endpoint, request)
        return Started(
            invocationId=ticket.invocation_id, sessionRef=ticket.session_ref,
            runId="run-a2a", configVersion=config_version,
        )

    async def status(_endpoint, invocation_id):
        request = control._prepared[invocation_id][1]
        return RunStatus(
            invocationId=invocation_id, channel=request.channel,
            workspaceRef=request.workspace_ref, sessionRef=request.session_ref,
            runId="run-a2a", configVersion=config_version,
            deadlineAt=request.deadline_at, state="completed", result=terminal_result,
        )

    control.delivery.start = start
    control.delivery.status = status
    event = Event(
        event_id="a2a:control-task", channel="a2a-review", type="a2a", source="a2a",
        payload={"text": "hello"}, context={"a2a": {"taskId": "control-task", "contextId": "ctx"}},
    )
    ticket = control.admission.admit(route(event, config.channels[0]))
    try:
        await ticket.started
        assert await ticket.completed is not None
        assert ticket.completed.result().result == terminal_result
    finally:
        await control.close()


async def _return_status(status: RunStatus) -> RunStatus:
    return status


async def test_recovered_control_real_mini_handshake(tmp_path: Path) -> None:
    from ach_runtime.execution.auth import create_authenticated_app
    from ach_runtime.execution.service import ExecutionService
    from ach_runtime.execution.sessions import SessionRegistry

    class NeverStartedEngine:
        def __init__(self):
            self.start_calls: list[str] = []

        async def start(self, invocation, *, home, scratch, password=None):
            self.start_calls.append(invocation.invocation_id)
            raise AssertionError("configure and public probes must not start the engine")

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    engine = NeverStartedEngine()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=tmp_path / "workspace", workspace_ref=endpoint.ref.workspace_ref,
    )
    mini_app = create_authenticated_app(
        service, verify_key=control.verify_key, workspace_name=endpoint.name, instance_id=endpoint.instance_id,
    )
    old_client = control.mini_client
    control.mini_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mini_app), base_url=endpoint.base_url,
    )
    try:
        await control.configure_workspace(endpoint)
        health = await control.mini_client.get("/healthz")
        ready = await control.mini_client.get("/readyz")
        assert health.status_code == 200
        assert ready.status_code == 200
        assert health.headers["x-ach-instance-id"] == endpoint.instance_id
        assert ready.headers["x-ach-instance-id"] == endpoint.instance_id
        assert not engine.start_calls
    finally:
        await old_client.aclose()
        await control.close()
        await service.close()


async def test_unit_waits_for_its_health_then_activation_proceeds_to_configure() -> None:
    """I4: the actual bound Control.wait_mini_health retries a connection failure and a 503,
    then once /healthz answers with an instance ID does activation proceed to
    restore (a noop here)/reconnect's configure->readyz->runs. The ID is the unit's own."""
    config = _config()
    seen: list[str] = []
    health_attempts = {"n": 0}

    async def mini(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/healthz":
            health_attempts["n"] += 1
            if health_attempts["n"] == 1:
                raise httpx.ConnectError("connection refused")
            if health_attempts["n"] == 2:
                return httpx.Response(503, headers={"X-ACH-Instance-Id": "pod-1"})
            return _healthy()
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"ready": True})
        if request.url.path == "/runs":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json=[])
        raise AssertionError(f"unexpected mini request {request.url}")

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    host, task = await _start_host(control)
    try:
        ref = WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64)
        endpoint = await control.manager.ensure(ref)
        assert endpoint.instance_id == "pod-1"
        assert health_attempts["n"] == 3
        mini_paths = [p for p in seen if p in {"/healthz", "/configure", "/readyz", "/runs"}]
        assert mini_paths[:5] == ["/healthz", "/healthz", "/healthz", "/configure", "/readyz"]
        assert "/start" not in seen
    finally:
        await _stop_host(host, task)
        await control.close()


async def _control_answering(handler) -> Control:
    return await assemble(
        _config(), environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(handler),
    )


async def test_wait_mini_health_returns_the_instance_id_the_unit_reports() -> None:
    seen: list[httpx.Request] = []

    async def mini(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return _healthy("unit-instance-7")

    control = await _control_answering(mini)
    try:
        assert await control.wait_mini_health("http://unit:8080/", "ws", time.monotonic() + 5) == "unit-instance-7"
        assert str(seen[0].url) == "http://unit:8080/healthz"
    finally:
        await control.close()


async def test_wait_mini_health_refuses_a_unit_of_another_release_and_alarms(monkeypatch) -> None:
    import ach_runtime.observability as observability

    anomalies: list[str] = []
    monkeypatch.setattr(observability, "alarm", lambda kind, **_refs: anomalies.append(kind))

    async def mini(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "unit"}, json={"status": "ok", "release": "0.0.1-old"})

    control = await _control_answering(mini)
    try:
        with pytest.raises(WorkspaceAssemblyUnavailable) as raised:
            await control.wait_mini_health("http://unit", "ws", time.monotonic() + 5)
        assert "0.0.1-old" in str(raised.value) and __version__ in str(raised.value)
        assert anomalies == ["pod_unhealthy"]
    finally:
        await control.close()


async def test_wait_mini_health_retries_until_the_original_deadline() -> None:
    """Only connection failures and 408/429/500/502/503/504 repeat (every 0.1 s), bounded by
    the one monotonic deadline: a unit that never gets ready times out instead of hanging."""
    calls = {"n": 0}

    async def mini(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] % 2:
            raise httpx.ConnectError("refused")
        return httpx.Response(503, headers={"X-ACH-Instance-Id": "unit"})

    control = await _control_answering(mini)
    try:
        started = time.monotonic()
        with pytest.raises(WorkspaceAssemblyUnavailable, match="timed out"):
            await control.wait_mini_health("http://unit", "ws", time.monotonic() + 0.45)
        assert 0.4 <= time.monotonic() - started < 2
        assert 3 <= calls["n"] <= 6
    finally:
        await control.close()


@pytest.mark.parametrize("failure", ["unauthorized", "forbidden", "no-instance-id", "invalid-json", "no-release"])
async def test_wait_mini_health_fails_at_once_on_anything_else(failure) -> None:
    responses = {
        "unauthorized": httpx.Response(401, headers={"X-ACH-Instance-Id": "unit"}),
        "forbidden": httpx.Response(403, headers={"X-ACH-Instance-Id": "unit"}),
        "no-instance-id": httpx.Response(200, json={"status": "ok", "release": __version__}),
        "invalid-json": httpx.Response(200, headers={"X-ACH-Instance-Id": "unit"}, content=b"not json"),
        "no-release": httpx.Response(200, headers={"X-ACH-Instance-Id": "unit"}, json={"status": "ok"}),
    }
    calls = {"n": 0}

    async def mini(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return responses[failure]

    control = await _control_answering(mini)
    try:
        with pytest.raises(WorkspaceAssemblyUnavailable):
            await control.wait_mini_health("http://unit", "ws", time.monotonic() + 5)
        assert calls["n"] == 1, f"{failure} must fail explicitly, never retry"
    finally:
        await control.close()


async def test_prepare_workspace_exclusion_is_scoped(monkeypatch) -> None:
    entered_a = asyncio.Event()
    release_a = asyncio.Event()
    calls_a = 0

    async def hold_a(self, cfg, event, ticket, endpoint):
        nonlocal calls_a
        if ticket.workspace_ref == "a" * 64:
            calls_a += 1
            if calls_a == 1:
                entered_a.set()
                await release_a.wait()

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/configure":
            payload = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": request.headers["x-ach-instance-id"]},
                                  json={"configVersion": payload["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": request.headers["x-ach-instance-id"]},
                                  json={"ready": True})
        raise AssertionError(request.url)

    control = await assemble(
        _config(handoff=True), environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    monkeypatch.setattr("ach_runtime.harness.control.Control._run_handoff", hold_a)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)

    def inputs(key: str, pod: str):
        routed = RoutedEvent(Event("event-" + key[0], "gitlab-mr-review", "merge_request", "gitlab"), "wk", "sk")
        ticket = AdmissionTicket(key[0], control.config.agent.namespace, control.config.agent.uid,
                                 key, "b" * 64, routed, deadline,
                                 asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future())
        endpoint = WorkspaceEndpoint(WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, key),
                                     "workspace-" + pod, pod, "http://mini")
        return ticket, endpoint

    async def prepare(key: str, pod: str):
        ticket, endpoint = inputs(key, pod)
        return await control.prepare(ticket, endpoint), ticket

    try:
        first_a = asyncio.create_task(prepare("a" * 64, "pod-a"))
        await asyncio.wait_for(entered_a.wait(), timeout=2)
        others = [asyncio.create_task(prepare(key * 64, "pod-" + key)) for key in "bcd"]
        completed = await asyncio.wait_for(asyncio.gather(*others), timeout=2)
        assert all(item[1].deadline_at is deadline for item in completed)
        second_a = asyncio.create_task(prepare("a" * 64, "pod-a"))
        await asyncio.sleep(0)
        assert not second_a.done()
        release_a.set()
        first, second = await asyncio.wait_for(asyncio.gather(first_a, second_a), timeout=2)
        assert first[1].deadline_at is deadline and second[1].deadline_at is deadline
    finally:
        release_a.set()
        await asyncio.gather(*(task for task in (locals().get("first_a"), locals().get("second_a")) if task),
                             return_exceptions=True)
        await control.close()


async def test_handoff_static_destination_and_failure(monkeypatch) -> None:
    seen: list[httpx.Request] = []
    async def fake_handoff(cfg, event, ticket, workspace, *, environ):
        (workspace / "payload.txt").write_text("checkout")
    monkeypatch.setattr("ach_runtime.harness.control.run_handoff", fake_handoff)
    async def mini(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/configure":
            data = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": data["public"]["configVersion"]})
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"status": "imported"})

    control = await assemble(
        _config(handoff=True), environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    routed = RoutedEvent(Event("delivery", "gitlab-mr-review", "merge_request", "gitlab", payload={}), "w", "s")
    ticket = AdmissionTicket("i", control.config.agent.namespace, control.config.agent.uid,
                             "a" * 64, "b" * 64, routed, datetime.now(timezone.utc),
                             asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future())
    endpoint = WorkspaceEndpoint(WorkspaceRef(control.config.agent.namespace,
                                              control.config.agent.uid, "a" * 64),
                                "workspace", "pod-1", "http://mini")
    try:
        request = await control.prepare(ticket, endpoint)
        assert request.invocation_id == ticket.invocation_id
        assert (request.workspace_key, request.session_key) == ("w", "s")
        handoff = next(req for req in seen if req.url.path == "/execution/v1/workspace/handoff")
        assert dict(handoff.url.params) == {"destination": "handoff"}
        import gzip
        import io
        import tarfile
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(handoff.content)), mode="r:") as archive:
            assert "./payload.txt" in archive.getnames()
            assert archive.extractfile("./payload.txt").read() == b"checkout"
    finally:
        await control.close()


async def test_handoff_failure_prevents_started(monkeypatch) -> None:
    seen = []
    async def fail(*args, **kwargs):
        raise TimeoutError("hook timed out")
    monkeypatch.setattr("ach_runtime.harness.control.run_handoff", fail)
    async def mini(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/configure":
            data = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": data["public"]["configVersion"]})
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
    config = _config(handoff=True)
    control = await assemble(config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
                             ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini))
    routed = RoutedEvent(Event("event", "gitlab-mr-review", "merge_request", "gitlab"), "w", "s")
    ticket = AdmissionTicket("i", config.agent.namespace, config.agent.uid, "a"*64, "b"*64, routed,
                             datetime.now(timezone.utc)+timedelta(seconds=30),
                             asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future())
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a"*64),
                                 "workspace", "pod-1", "http://mini")
    async def endpoint_for_ticket(_ticket):
        return endpoint
    control.delivery._endpoint = endpoint_for_ticket
    try:
        with pytest.raises(TimeoutError):
            await control.delivery.start(ticket)
        assert "/start" not in seen
    finally:
        await control.close()


@pytest.mark.parametrize("state", ["queued", "preparing", "sending", "running", "unknown"])
async def test_recovered_control_inventory_and_no_replay(state: str) -> None:
    seen: list[str] = []
    config = _config()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    inventory = RunStatus(
        invocationId="running-invocation", channel="gitlab-mr-review",
        workspaceRef="a" * 64, sessionRef="b" * 64, runId="run-1",
        configVersion="f" * 64, deadlineAt=deadline, state=state,
    )

    async def mini(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
        if request.url.path == "/runs":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json=[inventory.model_dump(by_alias=True, mode="json")])
        if request.url.path == "/status/running-invocation":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json=inventory.model_dump(by_alias=True, mode="json"))
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                              json={})

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    try:
        _publish(control, endpoint)
        await control.reconnect_workspace(endpoint)
        recovered = control.admission.recovered_run(inventory.invocation_id)
        assert recovered is not None
        assert recovered.channel == inventory.channel
        assert recovered.workspace_ref == inventory.workspace_ref
        assert recovered.session_ref == inventory.session_ref
        assert recovered.config_version == inventory.config_version
        assert recovered.deadline_at == inventory.deadline_at
        assert recovered.instance_id == endpoint.instance_id
        assert recovered.run_id == inventory.run_id
        assert control.admission.queued_total == 1
        assert control.admission.has_work(inventory.workspace_ref, inventory.session_ref)
        assert recovered.started.done()
        assert isinstance(recovered.started.exception(), RuntimeError)
        await asyncio.sleep(0)
        assert seen == ["/configure", "/readyz", "/runs", "/status/running-invocation"]
        assert "/start" not in seen
        assert "/cancel" not in seen
        observer_task = control.admission._recovery_tasks[inventory.invocation_id]
        await control.reconnect_workspace(endpoint)
        assert control.admission._recovery_tasks[inventory.invocation_id] is observer_task
        assert control.admission.queued_total == 1
    finally:
        await control.close()


@pytest.mark.parametrize("already_expired", [False, True])
@pytest.mark.parametrize("cancel_fails", [False, True])
async def test_recovered_control_deadline_and_terminal_release(already_expired, cancel_fails, monkeypatch):
    config = _config()
    base = datetime(2030, 1, 1, tzinfo=timezone.utc)
    deadline = base + timedelta(seconds=30)
    now = [deadline if already_expired else base]
    cancel_payloads: list[dict] = []
    status_started = asyncio.Event()
    terminal_gate = asyncio.Event()
    control_module = __import__("ach_runtime.harness.control", fromlist=["_clock"])
    import ach_runtime.observability as observability

    anomaly_kinds: list[str] = []
    monkeypatch.setattr(observability, "alarm", lambda kind, **_refs: anomaly_kinds.append(kind))
    monkeypatch.setattr(control_module, "_clock", lambda: now[0])

    inventory = RunStatus(
        invocationId="deadline-run", channel="gitlab-mr-review", workspaceRef="a" * 64,
        sessionRef="b" * 64, runId="run-1", configVersion="f" * 64,
        deadlineAt=deadline, state="running",
    )

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/cancel":
            cancel_payloads.append(json.loads(request.content))
            return httpx.Response(503 if cancel_fails else 200,
                                  headers={"X-ACH-Instance-Id": "pod-1"}, json={})
        if request.url.path == "/status/deadline-run":
            status_started.set()
            if cancel_payloads:
                await terminal_gate.wait()
                body = inventory.model_copy(update={"state": "completed"})
            else:
                body = inventory
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json=body.model_dump(by_alias=True, mode="json"))
        raise AssertionError(request.url)

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    try:
        _publish(control, endpoint)
        owner = control.admission.recover(
            invocation_id=inventory.invocation_id, channel=inventory.channel,
            workspace_ref=inventory.workspace_ref, session_ref=inventory.session_ref,
            config_version=inventory.config_version, deadline_at=inventory.deadline_at,
            instance_id=endpoint.instance_id, run_id=inventory.run_id,
            observer=lambda: control._observe_run(endpoint, inventory),
        )
        await status_started.wait()
        if not already_expired:
            assert not cancel_payloads
            _unpublish(control, endpoint.ref.workspace_ref)
            now[0] = deadline
        for _ in range(100):
            if cancel_payloads:
                break
            await asyncio.sleep(0.01)
        assert cancel_payloads == [{"invocationId": inventory.invocation_id,
                                    "reason": "invocation deadline exceeded"}]
        assert anomaly_kinds.count("limit_reached") == 1
        assert control.admission.queued_total == 1
        assert control.admission.recovered_run(inventory.invocation_id) is owner
        _publish(control, endpoint)
        terminal_gate.set()
        for _ in range(100):
            if control.admission.queued_total == 0:
                break
            await asyncio.sleep(0.01)
        assert control.admission.queued_total == 0
        assert control.admission.recovered_run(inventory.invocation_id) is None
    finally:
        terminal_gate.set()
        await control.close()


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("recovered run observation failed: archive endpoint expired", "run_outcome_unknown"),
        ("recovered run observation failed: start was unhealthy", "run_outcome_unknown"),
        ("Harness session persistence failed", "persistence_failed"),
        ("run start failed", "start_failed"),
        ("Harness lifecycle tick failed", "lifecycle_tick_failed"),
        ("not a known reason: archive start unhealthy expired", "run_outcome_unknown"),
    ],
)
def test_alarm_uses_exact_known_reasons_and_ignores_recovery_exception_suffix(
    reason, expected, monkeypatch
):
    import ach_runtime.observability as observability

    kinds: list[str] = []
    monkeypatch.setattr(observability, "alarm", lambda kind, **_refs: kinds.append(kind))
    control_module = __import__("ach_runtime.harness.control", fromlist=["_alarm"])

    control_module._alarm(reason)

    assert kinds == [expected]


@pytest.mark.parametrize("expired", [False, True])
async def test_start_timeout_is_limit_only_after_original_deadline(expired, monkeypatch):
    from types import SimpleNamespace

    from ach_runtime.harness import control as control_module

    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(control_module, "_clock", lambda: now)
    kinds: list[str] = []
    monkeypatch.setattr(control_module, "_alarm", lambda reason, _ticket=None: kinds.append(reason))

    class TimedOutDelivery:
        async def start(self, _ticket):
            raise TimeoutError("transport timed out")

    control = Control.__new__(Control)
    control.delivery = TimedOutDelivery()
    control._prepared = {"inv-timeout": None}
    deadline = now - timedelta(seconds=1) if expired else now + timedelta(seconds=1)
    ticket = SimpleNamespace(invocation_id="inv-timeout", deadline_at=deadline)
    event = RoutedEvent(Event("evt-timeout", "channel", "kind", "source"), "workspace", "session")

    with pytest.raises(TimeoutError, match="transport timed out"):
        await control.run(event, ticket)

    assert kinds == ["invocation deadline exceeded" if expired else "run start failed"]


async def test_gateway_unavailable_start_failure_raises_the_gateway_alarm(monkeypatch):
    from types import SimpleNamespace

    from ach_runtime.harness import control as control_module
    from ach_runtime.harness.delivery import GatewayUnavailable
    import ach_runtime.observability as observability

    kinds: list[str] = []
    monkeypatch.setattr(observability, "alarm", lambda kind, **_refs: kinds.append(kind))

    class DownDelivery:
        async def start(self, _ticket):
            raise GatewayUnavailable("gateway_unavailable")

    control = Control.__new__(Control)
    control.delivery = DownDelivery()
    control._prepared = {"inv-gw": None}
    ticket = SimpleNamespace(
        invocation_id="inv-gw", workspace_ref="a" * 64, session_ref="s",
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    event = RoutedEvent(Event("evt-gw", "channel", "kind", "source"), "workspace", "session")

    with pytest.raises(GatewayUnavailable):
        await control.run(event, ticket)

    assert kinds == ["gateway_unavailable"]
    assert "inv-gw" not in control._prepared


@pytest.mark.parametrize("fault", [
    "401", "404", "pod", "unknown", "invocation_id", "channel", "workspace_ref",
    "session_ref", "config_version", "deadline_at", "run_id",
])
async def test_recovered_control_invalid_observation_holds_ownership(fault, monkeypatch):
    config = _config()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    inventory = RunStatus(
        invocationId="invalid-run", channel="gitlab-mr-review", workspaceRef="a" * 64,
        sessionRef="b" * 64, runId="run-1", configVersion="f" * 64,
        deadlineAt=deadline, state="running",
    )
    invalid_seen = asyncio.Event()
    allow_terminal = asyncio.Event()
    valid = False
    alarms: list[str] = []
    control_module = __import__("ach_runtime.harness.control", fromlist=["_alarm"])
    monkeypatch.setattr(control_module, "_alarm", lambda reason, _owner=None: alarms.append(reason))

    async def mini(request: httpx.Request) -> httpx.Response:
        nonlocal valid
        if request.url.path == "/status/invalid-run":
            if valid:
                await allow_terminal.wait()
                result = inventory.model_copy(update={"state": "completed"})
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                      json=result.model_dump(by_alias=True, mode="json"))
            invalid_seen.set()
            if fault in {"401", "404"}:
                return httpx.Response(int(fault), headers={"X-ACH-Instance-Id": "pod-1"}, json={})
            if fault == "pod":
                return httpx.Response(200, headers={"X-ACH-Instance-Id": "wrong-pod"},
                                      json=inventory.model_dump(by_alias=True, mode="json"))
            if fault == "unknown":
                bad = inventory.model_copy(update={"state": "unknown"})
            elif fault == "run_id":
                bad = inventory.model_copy(update={"run_id": "other-run"})
            elif fault == "deadline_at":
                bad = inventory.model_copy(update={"deadline_at": deadline + timedelta(seconds=1)})
            else:
                bad = inventory.model_copy(update={fault: "other"})
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json=bad.model_dump(by_alias=True, mode="json"))
        raise AssertionError(request.url)

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    try:
        _publish(control, endpoint)
        owner = control.admission.recover(
            invocation_id=inventory.invocation_id, channel=inventory.channel,
            workspace_ref=inventory.workspace_ref, session_ref=inventory.session_ref,
            config_version=inventory.config_version, deadline_at=inventory.deadline_at,
            instance_id=endpoint.instance_id, run_id=inventory.run_id,
            observer=lambda: control._observe_run(endpoint, inventory),
        )
        await invalid_seen.wait()
        for _ in range(100):
            if alarms:
                break
            await asyncio.sleep(0.01)
        assert alarms
        assert control.admission.queued_total == 1
        assert control.admission.recovered_run(inventory.invocation_id) is owner
        valid = True
        allow_terminal.set()
        for _ in range(100):
            if control.admission.queued_total == 0:
                break
            await asyncio.sleep(0.01)
        assert control.admission.queued_total == 0
    finally:
        allow_terminal.set()
        await control.close()


async def test_recovered_control_redelivery_unknown_and_idempotent_discovery(monkeypatch):
    from ach_runtime.harness.app import create_app
    from ach_runtime.harness.refs import invocation_digest

    config = _config()
    event_id = "recovered-delivery"
    invocation_id = invocation_digest(config.agent.uid, "gitlab-mr-review", event_id)
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    inventory = RunStatus(
        invocationId=invocation_id, channel="gitlab-mr-review", workspaceRef="a" * 64,
        sessionRef="b" * 64, runId="run-1", configVersion="f" * 64,
        deadlineAt=deadline, state="running",
    )
    finish = asyncio.Event()
    status_seen = asyncio.Event()
    paths: list[str] = []

    async def mini(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
        if request.url.path == "/runs":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json=[inventory.model_dump(by_alias=True, mode="json")])
        if request.url.path == f"/status/{invocation_id}":
            status_seen.set()
            await finish.wait()
            terminal = inventory.model_copy(update={"state": "completed"})
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json=terminal.model_dump(by_alias=True, mode="json"))
        raise AssertionError(request.url)

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    monkeypatch.setenv("ACH_SECRET_GITLAB_WEBHOOK", "hook-secret")
    alarms = []
    control.admission._alarm = lambda *args: alarms.append(args)
    _publish(control, endpoint)
    await control.reconnect_workspace(endpoint)
    await status_seen.wait()
    owner = control.admission.recovered_run(invocation_id)
    assert owner is not None
    observer = control.admission._recovery_tasks[invocation_id]
    await control.reconnect_workspace(endpoint)
    assert control.admission.recovered_run(invocation_id) is owner
    assert control.admission._recovery_tasks[invocation_id] is observer

    app = create_app(control)
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://control")
    payload = {
        "object_kind": "merge_request", "project": {"id": 77, "path_with_namespace": "team/project"},
        "user": {"username": "juancarlos.moreno"}, "object_attributes": {"iid": 4, "title": "recovered"},
        "title": "recovered",
    }
    headers = {"X-Gitlab-Event-UUID": event_id, "X-Gitlab-Token": "hook-secret"}
    try:
        first = await client.post("/channels/gitlab-mr-review/events", headers=headers, json=payload)
        assert first.status_code == 202
        assert control.admission.queued_total == 1
        assert "/start" not in paths
        finish.set()
        for _ in range(100):
            if control.admission.queued_total == 0:
                break
            await asyncio.sleep(0.01)
        assert control.admission.queued_total == 0
        duplicate = await client.post("/channels/gitlab-mr-review/events", headers=headers, json=payload)
        assert duplicate.status_code == 202
        assert control.admission.queued_total == 0
        assert "/start" not in paths
        assert len(alarms) >= 2
    finally:
        finish.set()
        await client.aclose()
        await control.close()


@pytest.mark.parametrize("started_before_loss", [False, True])
async def test_owner_loss_ordinary_waits_for_published_replacement(started_before_loss, monkeypatch):
    from ach_runtime.execution.wire import StartRequest, Started
    from ach_runtime.harness.delivery import StartOutcomeUnknown

    control = await assemble(
        _config(), environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
    )
    routed = RoutedEvent(Event("owner-loss-ordinary", "gitlab-mr-review", "merge_request", "gitlab"),
                         "owner-loss-ws", "main")
    endpoint = None
    request = None

    async def start(ticket):
        start_calls.append(ticket.invocation_id)
        if not started_before_loss:
            raise StartOutcomeUnknown("the one Start request may have reached the old owner")
        return Started(
            invocationId=ticket.invocation_id, sessionRef=ticket.session_ref,
            runId="run-old", configVersion=control.public.config_version,
        )

    control.delivery.start = start
    control.admission._runner = control.run

    ticket = control.admission.admit(routed)
    original_deadline = ticket.deadline_at
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, ticket.workspace_ref),
        "workspace", "pod-old", "http://mini",
    )
    replacement = WorkspaceEndpoint(endpoint.ref, "workspace", "pod-new", "http://mini")
    request = StartRequest(
        workspaceKey="wk", sessionKey="sk",
        invocationId=ticket.invocation_id, channel=routed.event.channel,
        workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref,
        configVersion=control.public.config_version, prompt="already sent once",
        deadlineAt=ticket.deadline_at,
    )
    _publish(control, endpoint)
    control._prepared[ticket.invocation_id] = (endpoint, request)
    start_calls = []
    status_seen = asyncio.Event()
    release_stale_status = asyncio.Event()
    status_calls = []
    lost_alarms = []
    control_module = __import__("ach_runtime.harness.control", fromlist=["_alarm"])
    monkeypatch.setattr(control_module, "_alarm", lambda reason, _owner=None: lost_alarms.append(reason))

    async def stale_terminal(observed, invocation_id):
        status_calls.append((observed.instance_id, invocation_id))
        status_seen.set()
        await release_stale_status.wait()
        return RunStatus(
            invocationId=ticket.invocation_id, channel=routed.event.channel,
            workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref,
            runId="run-old", configVersion=control.public.config_version,
            deadlineAt=original_deadline, state="running",
        )

    control.delivery.status = stale_terminal
    try:
        await asyncio.wait_for(status_seen.wait(), timeout=2)
        _unpublish(control, ticket.workspace_ref)
        release_stale_status.set()
        await asyncio.sleep(0.3)
        assert control.admission.queued_total == 1
        assert control.admission.has_work(ticket.workspace_ref, ticket.session_ref)
        assert not ticket.completed.done()
        assert len(status_calls) == 1
        assert control.admission.admit(routed) is ticket
        assert ticket.deadline_at is original_deadline
        assert start_calls == [ticket.invocation_id]

        _publish(control, replacement)
        with pytest.raises(RuntimeError) as lost:
            await asyncio.wait_for(asyncio.shield(ticket.completed), timeout=2)
        assert type(lost.value).__name__ == "WorkspaceOwnerLost"
        assert "unknown" in str(lost.value).lower() or "lost" in str(lost.value).lower()
        assert control.admission.queued_total == 0
        assert not control.admission.has_work(ticket.workspace_ref, ticket.session_ref)
        assert control.admission._dedup_key(control.config.agent.uid, routed) in control.admission._released
        assert ticket.deadline_at is original_deadline
        if started_before_loss:
            assert ticket.started.result() is None
        else:
            assert isinstance(ticket.started.exception(), RuntimeError)
            assert "WorkspaceOwnerLost" in type(ticket.started.exception()).__name__ or "owner" in str(ticket.started.exception()).lower()
        assert start_calls == [ticket.invocation_id]
        assert any("owner" in alarm.lower() or "lost" in alarm.lower() for alarm in lost_alarms)
    finally:
        release_stale_status.set()
        await control.close()


async def test_owner_loss_recovered_waits_for_published_replacement(monkeypatch):
    control = await assemble(
        _config(), environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
    )
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    inventory = RunStatus(
        invocationId="owner-loss-recovered", channel="gitlab-mr-review",
        workspaceRef="a" * 64, sessionRef="b" * 64, runId="run-old",
        configVersion="f" * 64, deadlineAt=deadline, state="running",
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, inventory.workspace_ref),
        "workspace", "pod-old", "http://mini",
    )
    replacement = WorkspaceEndpoint(endpoint.ref, "workspace", "pod-new", "http://mini")
    _publish(control, endpoint)
    status_seen = asyncio.Event()
    release_stale_status = asyncio.Event()
    status_calls = []
    alarms = []
    control.admission._alarm = lambda reason, owner: alarms.append(reason)
    control_module = __import__("ach_runtime.harness.control", fromlist=["_alarm"])
    monkeypatch.setattr(control_module, "_alarm", lambda reason, _owner=None: alarms.append(reason))

    async def stale_terminal(observed, invocation_id):
        status_calls.append((observed.instance_id, invocation_id))
        status_seen.set()
        await release_stale_status.wait()
        return inventory  # still running: only a terminal status survives the owner's loss (T09)

    control.delivery.status = stale_terminal
    try:
        owner = control.admission.recover(
            invocation_id=inventory.invocation_id, channel=inventory.channel,
            workspace_ref=inventory.workspace_ref, session_ref=inventory.session_ref,
            config_version=inventory.config_version, deadline_at=deadline,
            instance_id=endpoint.instance_id, run_id=inventory.run_id,
            observer=lambda: control._observe_run(endpoint, inventory),
        )
        await asyncio.wait_for(status_seen.wait(), timeout=2)
        _unpublish(control, inventory.workspace_ref)
        release_stale_status.set()
        await asyncio.sleep(0.3)
        assert control.admission.queued_total == 1
        assert control.admission.has_work(inventory.workspace_ref, inventory.session_ref)
        assert not owner.completed.done()
        assert control.admission.recovered_run(inventory.invocation_id) is owner
        assert len(status_calls) == 1

        _publish(control, replacement)
        with pytest.raises(RuntimeError) as lost:
            await asyncio.wait_for(asyncio.shield(owner.completed), timeout=2)
        assert type(lost.value).__name__ == "WorkspaceOwnerLost"
        assert "unknown" in str(lost.value).lower() or "lost" in str(lost.value).lower()
        assert control.admission.queued_total == 0
        assert not control.admission.has_work(inventory.workspace_ref, inventory.session_ref)
        assert control.admission.recovered_run(inventory.invocation_id) is None
        assert control.admission.recover(
            invocation_id=inventory.invocation_id, channel=inventory.channel,
            workspace_ref=inventory.workspace_ref, session_ref=inventory.session_ref,
            config_version=inventory.config_version, deadline_at=deadline,
            instance_id=endpoint.instance_id, run_id=inventory.run_id,
            observer=lambda: control._observe_run(endpoint, inventory),
        ) is owner
        assert owner.started.done() and isinstance(owner.started.exception(), RuntimeError)
        assert control.admission.queued_total == 0
    finally:
        release_stale_status.set()
        await control.close()


@pytest.mark.parametrize("invalid_inventory", ["malformed", "wrong_owner", "duplicate"])
async def test_recovered_control_invalid_inventory_fails_before_registration(invalid_inventory):
    config = _config()
    deadline = datetime.now(timezone.utc) + timedelta(minutes=5)
    valid = RunStatus(
        invocationId="first-run", channel="gitlab-mr-review", workspaceRef="a" * 64,
        sessionRef="b" * 64, runId="run-1", configVersion="f" * 64,
        deadlineAt=deadline, state="running",
    )
    if invalid_inventory == "malformed":
        inventory = [valid.model_dump(by_alias=True, mode="json"), {"not": "a run"}]
    elif invalid_inventory == "wrong_owner":
        other = valid.model_copy(update={"invocation_id": "second-run", "workspace_ref": "c" * 64})
        inventory = [valid.model_dump(by_alias=True, mode="json"), other.model_dump(by_alias=True, mode="json")]
    else:
        inventory = [valid.model_dump(by_alias=True, mode="json"), valid.model_dump(by_alias=True, mode="json")]

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
        if request.url.path == "/runs":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json=inventory)
        raise AssertionError(request.url)

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    try:
        with pytest.raises(WorkspaceAssemblyUnavailable):
            await control.reconnect_workspace(endpoint)
        assert control.admission.queued_total == 0
        assert not control.admission._recovered
    finally:
        await control.close()


async def test_reconnect_accepts_terminal_history_from_an_older_config_version() -> None:
    config = _config()

    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
        records = [RunStatus(
            invocationId=f"historical-{state}", channel="gitlab-mr-review",
            workspaceRef="a" * 64, sessionRef="b" * 64, runId=f"run-{state}",
            configVersion="f" * 64, deadlineAt=datetime.now(timezone.utc), state=state,
        ).model_dump(by_alias=True, mode="json") for state in ("completed", "failed", "cancelled", "skipped")]
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json=records)

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    try:
        await control.reconnect_workspace(endpoint)
        assert control.admission.queued_total == 0
    finally:
        await control.close()


async def test_assembled_facade_clients_preserve_streaming_timeout_policy() -> None:
    control = await assemble(
        _config(), environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
    )
    try:
        for client in (control.model_facade._client, control.mcp_facade._client):
            timeout = client.timeout
            assert timeout.connect == 30.0
            assert timeout.read is None
            assert timeout.write is None
            assert timeout.pool is None
    finally:
        await control.close()


async def test_real_admission_delivery_acks_admission_and_holds_workspace_permit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config()
    started_gate = asyncio.Event()
    start_entered = asyncio.Event()
    first_status_seen = asyncio.Event()
    finish_first = asyncio.Event()
    starts: list[dict] = []

    async def mini(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/healthz":
            return _healthy()
        if path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                                  json={"configVersion": body["public"]["configVersion"]})
        if path == "/readyz":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
        if path == "/runs":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json=[])
        if path == "/start":
            body = json.loads(request.content)
            starts.append(body)
            if len(starts) == 1:
                start_entered.set()
                await started_gate.wait()
            return httpx.Response(202, headers={"X-ACH-Instance-Id": "pod-1"}, json={
                "invocationId": body["invocationId"], "sessionRef": body["sessionRef"],
                "runId": f"run-{len(starts)}", "configVersion": body["configVersion"],
            })
        if path.startswith("/status/"):
            invocation_id = path.rsplit("/", 1)[1]
            body = next(item for item in starts if item["invocationId"] == invocation_id)
            run_number = starts.index(body) + 1
            if run_number == 1:
                first_status_seen.set()
                state = "completed" if finish_first.is_set() else "running"
            else:
                state = "completed"
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={
                "invocationId": body["invocationId"], "channel": body["channel"],
                "workspaceRef": body["workspaceRef"], "sessionRef": body["sessionRef"],
                "runId": f"run-{run_number}", "configVersion": body["configVersion"],
                "deadlineAt": body["deadlineAt"], "state": state,
            })
        raise AssertionError(f"unexpected mini request {request.url}")

    control = await assemble(
        config,
        environ={"ACH_SANDBOX_KEY": "harness-key", "ACH_SECRET_IDENTITY": "ek-secret",
                 "ACH_SECRET_GITLAB_WEBHOOK": "hook-secret"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    monkeypatch.setenv("ACH_SECRET_GITLAB_WEBHOOK", "hook-secret")
    host, host_task = await _start_host(control)

    from ach_runtime.harness.app import create_app
    app = create_app(control)
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://control")

    def payload(iid: int, title: str, *, note: bool = False) -> dict:
        body = {
            "object_kind": "note" if note else "merge_request",
        "project": {"id": 77, "path_with_namespace": "team/project"},
        "user": {"username": "juancarlos.moreno"},
            "object_attributes": {"iid": iid, "title": title},
            "title": title,
        }
        if note:
            body["object_attributes"].update({"noteable_type": "MergeRequest", "note": title})
            body["merge_request"] = {"iid": iid}
        return body

    async def post(event_id: str, body: dict) -> httpx.Response:
        return await client.post(
            "/channels/gitlab-mr-review/events",
            headers={"X-Gitlab-Event-UUID": event_id, "X-Gitlab-Token": "hook-secret"}, json=body,
        )

    try:
        first = asyncio.create_task(post("delivery-1", payload(4, "first")))
        try:
            await asyncio.wait_for(start_entered.wait(), timeout=10)
        except TimeoutError:
            if first.done():
                response = first.result()
                pytest.fail(f"ingress returned before Start: {response.status_code} {response.text}")
            pytest.fail(
                f"Start was not reached: queued={control.admission.queued_total}, "
                f"tickets={len(control.admission._tickets)}, active={control.manager._active}, "
                f"errors={control.manager._errors}"
            )
        first_response = await asyncio.wait_for(first, 1)
        assert first_response.status_code == 202
        assert not next(iter(control.admission._tickets.values())).started.done()
        started_gate.set()
        await first_status_seen.wait()

        duplicate = await post("delivery-1", payload(4, "first"))
        assert duplicate.status_code == 202
        successor = asyncio.create_task(post("delivery-2", payload(4, "successor", note=True)))
        competitor = asyncio.create_task(post("delivery-3", payload(5, "competitor")))
        await asyncio.sleep(0.05)
        assert (await asyncio.wait_for(successor, 1)).status_code == 202
        assert (await asyncio.wait_for(competitor, 1)).status_code == 202
        assert len(starts) == 1

        finish_first.set()
        assert (await successor).status_code == 202
        assert (await competitor).status_code == 202
        tickets = list(control.admission._tickets.values())
        await asyncio.wait_for(asyncio.gather(*(ticket.started for ticket in tickets)), 5)
        assert len(starts) == 3
        assert starts[0]["prompt"].startswith("Review first")
        assert any("Review successor" in item["prompt"] for item in starts)
        assert any("Review competitor" in item["prompt"] for item in starts)
        for _ in range(50):
            if control.admission.queued_total == 0:
                break
            await asyncio.sleep(0.02)
        assert control.admission.queued_total == 0
    finally:
        finish_first.set()
        await client.aclose()
        await _stop_host(host, host_task)
        await control.close()


@pytest.mark.parametrize(
    ("terminal_state", "terminal_text"),
    [("completed", "native reply"), ("failed", "provider unavailable")],
)
async def test_mounted_a2a_uses_real_admission_delivery_and_retained_get_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_state: str, terminal_text: str,
) -> None:
    from ach_runtime.channels.a2a import A2A_OUTPUT_INSTRUCTIONS
    from ach_runtime.harness.app import create_app

    config = _a2a_config()
    monkeypatch.setenv("A2A_TOKEN", "a2a-secret")
    start_entered = asyncio.Event()
    release_started = asyncio.Event()
    status_seen = asyncio.Event()
    start_requests: list[dict] = []

    async def mini(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        instance_id = request.headers.get("x-ach-instance-id", "pod-1")
        headers = {"X-ACH-Instance-Id": instance_id}
        if path == "/healthz":
            return _healthy(instance_id)
        if path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers=headers,
                                  json={"configVersion": body["public"]["configVersion"]})
        if path == "/readyz":
            return httpx.Response(200, headers=headers, json={"ready": True})
        if path == "/runs":
            return httpx.Response(200, headers=headers, json=[])
        if path == "/start":
            body = json.loads(request.content)
            start_requests.append(body)
            start_entered.set()
            await release_started.wait()
            return httpx.Response(202, headers=headers, json={
                "invocationId": body["invocationId"], "sessionRef": body["sessionRef"],
                "runId": "run-a2a", "configVersion": body["configVersion"],
            })
        if path.startswith("/status/"):
            invocation_id = path.rsplit("/", 1)[1]
            body = start_requests[0]
            status_seen.set()
            return httpx.Response(200, headers=headers, json={
                "invocationId": invocation_id, "channel": body["channel"],
                "workspaceRef": body["workspaceRef"], "sessionRef": body["sessionRef"],
                "runId": "run-a2a", "configVersion": body["configVersion"],
                "deadlineAt": body["deadlineAt"], "state": terminal_state,
                "error": terminal_text if terminal_state == "failed" else None,
                "result": {"text": '{"action":"a2a_reply","text":"native reply"}'}
                if terminal_state == "completed" else None,
            })
        raise AssertionError(f"unexpected mini request {request.method} {path}")

    control = await assemble(
        config,
        environ={"ACH_SANDBOX_KEY": "harness-key", "ACH_SECRET_IDENTITY": "ek-secret"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(mini),
    )
    host, host_task = await _start_host(control)

    app = create_app(control)
    app.state.ready = True
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control")
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {
                "messageId": "message-a2a", "contextId": "conversation-a2a",
                "role": "ROLE_USER", "parts": [{"text": "hello to native"}],
            },
            "configuration": {"returnImmediately": True},
        },
    }
    auth = {"x-a2a-custom-api-key": "a2a-secret", "a2a-version": "1.0"}
    request_task: asyncio.Task[httpx.Response] | None = None
    try:
        request_task = asyncio.create_task(client.post("/a2a/a2a-review/", json=payload, headers=auth))
        await asyncio.wait_for(start_entered.wait(), timeout=10)
        ticket = next(iter(control.admission._tickets.values()))
        assert not ticket.started.done()
        assert not request_task.done(), "SDK must not observe a working task before Started"
        assert len(start_requests) == 1
        assert not status_seen.is_set()

        release_started.set()
        send_response = await asyncio.wait_for(request_task, timeout=10)
        assert send_response.status_code == 200, send_response.text
        response_body = send_response.json()
        sdk_task = response_body["result"]["task"]
        assert sdk_task["status"]["state"] == "TASK_STATE_WORKING"
        assert sdk_task["id"]
        assert sdk_task["contextId"] == "conversation-a2a"
        await ticket.started
        await ticket.completed
        await asyncio.wait_for(status_seen.wait(), timeout=5)

        assert len(start_requests) == 1
        assert start_requests[0]["prompt"] == (
            "hello to native\n\n## Memory\n\nUnavailable (no endpoint).\n\n"
            + A2A_OUTPUT_INSTRUCTIONS
        )
        expected_state = "TASK_STATE_COMPLETED" if terminal_state == "completed" else "TASK_STATE_FAILED"
        saved_task = None
        for _ in range(100):
            get_response = await client.get(f"/a2a/a2a-review/tasks/{sdk_task['id']}", headers=auth)
            assert get_response.status_code == 200, get_response.text
            saved_task = get_response.json()
            if saved_task["status"]["state"] == expected_state:
                break
            await asyncio.sleep(0.01)
        assert saved_task is not None
        assert saved_task["id"] == sdk_task["id"]
        assert saved_task["contextId"] == sdk_task["contextId"]
        assert saved_task["status"]["state"] == expected_state
        expected_text = terminal_text
        assert saved_task["status"]["message"]["parts"][0]["text"] == expected_text
    finally:
        release_started.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await client.aclose()
        await _stop_host(host, host_task)
        await control.close()


async def test_harness_prepares_file_prompt_before_workspace_hydration() -> None:
    from ach_runtime.config.schema import SystemFile
    config = _config()
    config.prompt.system = SystemFile(type="file", file="system.md")
    async def mini(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/configure":
            data = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"configVersion": data["public"]["configVersion"]})
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
    control = await assemble(config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
                             ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini))
    routed = RoutedEvent(Event("unresolved", "gitlab-mr-review", "merge_request", "gitlab"), "w", "s")
    ticket = AdmissionTicket("i", config.agent.namespace, config.agent.uid, "a"*64, "b"*64, routed,
                             datetime.now(timezone.utc), asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future())
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a"*64), "workspace", "pod-1", "http://mini")
    try:
        request = await control.prepare(ticket, endpoint)
        assert request.config_version == control.public.config_version
        assert request.deadline_at == ticket.deadline_at
        assert ticket.invocation_id in control._prepared
    finally:
        await control.close()


async def test_harness_hydrates_prompt_tar_before_real_mini_engine_start(monkeypatch, tmp_path: Path) -> None:
    import io
    import tarfile

    from ach_runtime.config.schema import SystemAch
    from ach_runtime.engine.opencode import EngineReceipt
    from ach_runtime.execution.auth import create_authenticated_app
    from ach_runtime.execution.service import ExecutionService
    from ach_runtime.execution.sessions import SessionRegistry
    from ach_runtime.harness.delivery import DeliveryError
    from ach_runtime.harness.hydrate import ContextItem

    def archive(files: dict[str, bytes]) -> bytes:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w:gz") as tar:
            for name, content in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        return payload.getvalue()

    config = _config()
    config.prompt.system = SystemAch(type="ach", ach="review", file="system.md")
    payloads = {
        "https://ach.example/prompt": archive({"system.md": b"ACH system prompt \xe2\x9c\x93"}),
        "https://ach.example/skill": archive({"demo/SKILL.md": b"skill content"}),
    }

    async def ach(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json={
                "environment": "engineering-prod",
                "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                            "mcpServers": [], "a2aAgents": []},
                "context": {"skills": [], "prompts": [], "artifacts": []},
            })
        if request.url.path == "/v2/model/info":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url)

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "credential-must-not-cross-tar"},
        ach_transport=httpx.MockTransport(ach), mini_transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    control.catalog = type(control.catalog)(
        control.catalog.environment, control.catalog.model, control.catalog.mcp_servers,
        (ContextItem(name="demo", id="skill", downloadUrl="https://ach.example/skill"),),
        (ContextItem(name="review", id="prompt", downloadUrl="https://ach.example/prompt"),),
        control.catalog.artifacts,
    )

    class RecordingEngine:
        def __init__(self):
            self.invocations = []

        async def start(self, invocation, *, home, scratch, password=None):
            self.invocations.append(invocation)
            return EngineReceipt(native_session_id="native", input_id="input")

        async def wait_settled(self, invocation_id):
            return "completed"

        async def status(self, invocation_id):
            return "completed"

        async def cancel(self, invocation_id, reason, *, _halt_budget=None):
            return None

    engine = RecordingEngine()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"), workspace_dir=workspace,
        workspace_ref="a" * 64,
    )
    mini_app = create_authenticated_app(
        service, verify_key=control.verify_key, workspace_name="workspace", instance_id="pod-1",
    )
    hydration_archives: list[bytes] = []

    async def capture_hydration(scope, receive, send):
        async def with_instance_id(message):
            if message["type"] == "http.response.start" and not any(
                name.lower() == b"x-ach-instance-id" for name, _ in message.get("headers", [])
            ):
                message = dict(message)
                message["headers"] = [*message.get("headers", []), (b"x-ach-instance-id", b"pod-1")]
            await send(message)

        if scope.get("type") == "http" and scope.get("path") == "/execution/v1/sandbox/archive/hydration":
            messages = []
            chunks = []
            while True:
                message = await receive()
                messages.append(message)
                chunks.append(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            hydration_archives.append(b"".join(chunks))

            async def replay():
                if messages:
                    return messages.pop(0)
                return await receive()

            await mini_app(scope, replay, with_instance_id)
            return
        await mini_app(scope, receive, with_instance_id)

    asgi_transport = httpx.ASGITransport(app=capture_hydration)
    control.mini_client = httpx.AsyncClient(transport=asgi_transport, timeout=10)
    control.delivery.client = httpx.AsyncClient(transport=asgi_transport, timeout=None)
    real_async_client = httpx.AsyncClient

    async def downloads(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-ach-key"] == "credential-must-not-cross-tar"
        return httpx.Response(200, content=payloads[str(request.url)])

    def async_client(*args, **kwargs):
        if "transport" not in kwargs:
            kwargs["transport"] = httpx.MockTransport(downloads)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    routed = RoutedEvent(Event("hydrated", "gitlab-mr-review", "merge_request", "gitlab"), "w", "s")
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    ticket = AdmissionTicket(
        "hydrated-inv", config.agent.namespace, config.agent.uid, "a" * 64, "b" * 64,
        routed, deadline, asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future(),
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    async def endpoint_for_ticket(_ticket):
        return endpoint
    control.delivery._endpoint = endpoint_for_ticket
    try:
        control.public.prompt.system.file = "missing.md"
        with pytest.raises(DeliveryError):
            await control.delivery.start(ticket)
        assert not engine.invocations

        control.public.prompt.system.file = "system.md"
        success_event = RoutedEvent(
            Event("hydrated-success", "gitlab-mr-review", "merge_request", "gitlab"), "w", "s"
        )
        success_ticket = AdmissionTicket(
            "hydrated-inv-success", config.agent.namespace, config.agent.uid, "a" * 64, "b" * 64,
            success_event, deadline, asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future(),
        )
        started = await control.delivery.start(success_ticket)
        assert started.config_version == control.public.config_version
        assert started.session_ref == success_ticket.session_ref
        assert len(engine.invocations) == 1
        invocation = engine.invocations[0]
        assert invocation.resolved_system_prompt == "ACH system prompt ✓"
        assert invocation.prompt == (
            f"Review untitled for {config.agent.name}\n\n## Memory\n\nUnavailable (no endpoint)."
        )
        assert control._prepared[success_ticket.invocation_id][1].deadline_at == deadline
        assert len(hydration_archives) == 2
        assert all(b"credential-must-not-cross-tar" not in item for item in hydration_archives)
        assert (workspace / ".ach-state" / "skills" / "demo" / "SKILL.md").read_text() == "skill content"
    finally:
        await control.close()
        await service.close()


async def test_context_files_before_started(monkeypatch) -> None:
    from ach_runtime.harness.hydrate import ContextItem
    seen: list[str] = []
    async def stage(catalog, root, *, excluded_skills, client, ek):
        (root / "skills").mkdir(parents=True)
        (root / "skills" / "demo").write_text("skill")
    monkeypatch.setattr("ach_runtime.harness.control.stage_context", stage)
    async def mini(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/configure":
            data = json.loads(request.content)
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"configVersion": data["public"]["configVersion"]})
        if request.url.path == "/execution/v1/sandbox/archive/hydration":
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"status": "imported", "sessionRef": "b"*64})
        if request.url.path == "/start":
            body = json.loads(request.content)
            return httpx.Response(202, headers={"X-ACH-Instance-Id": "pod-1"}, json={
                "invocationId": body["invocationId"], "sessionRef": body["sessionRef"],
                "runId": "run", "configVersion": body["configVersion"]})
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={})
    control = await assemble(_config(), environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
                             ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini))
    control.catalog = type(control.catalog)(control.catalog.environment, control.catalog.model, control.catalog.mcp_servers,
                                              (ContextItem(name="demo", id="id", downloadUrl="https://ach/skill"),),
                                              control.catalog.prompts, control.catalog.artifacts)
    routed = RoutedEvent(Event("context-event", "gitlab-mr-review", "merge_request", "gitlab"), "w", "s")
    ticket = AdmissionTicket("context-inv", control.config.agent.namespace, control.config.agent.uid,
                             "a"*64, "b"*64, routed, datetime.now(timezone.utc)+timedelta(seconds=30),
                             asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future())
    endpoint = WorkspaceEndpoint(WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, "a"*64), "workspace", "pod-1", "http://mini")
    async def endpoint_for_ticket(_ticket):
        return endpoint
    control.delivery._endpoint = endpoint_for_ticket
    try:
        await control.delivery.start(ticket)
        assert seen.index("/execution/v1/sandbox/archive/hydration") < seen.index("/start")
    finally:
        await control.close()


async def test_control_close_joins_lifecycle_before_clients() -> None:
    from types import SimpleNamespace

    from ach_runtime.harness.control import Control

    closed = []

    def closer(name):
        async def close():
            closed.append(name)
        return close

    fake = SimpleNamespace(
        _stopping=False,
        admission=SimpleNamespace(close=closer("admission")),
        lifecycle=SimpleNamespace(close=closer("lifecycle")),
        a2a_facade=None,
        delivery=SimpleNamespace(close=closer("delivery")),
        source=SimpleNamespace(close=lambda: closed.append("source")),
        manager=SimpleNamespace(cancel_adoption=lambda: closed.append("adoption")),
        mini_client=SimpleNamespace(aclose=closer("mini")),
        facade_clients=[SimpleNamespace(aclose=closer("facade"))],
    )
    await Control.close(fake)
    assert closed.index("source") < closed.index("admission"), "pending host long-polls answer 503 first"
    assert closed.index("admission") < closed.index("lifecycle")
    assert closed.index("lifecycle") < min(closed.index("delivery"), closed.index("mini"), closed.index("facade"))


async def test_control_runs_script_ticket_without_workspace_assembly(tmp_path: Path) -> None:
    output = tmp_path / "control-script"
    script = ScriptBlock.model_validate({
        "script": 'printf "%s|%s|%s" "$ACH_EVENT_ID" "${ACH_SESSION_KEY-unset}" "$GITLAB_TOKEN" > "$OUT"',
        "env": {"OUT": str(output)},
        "secretEnv": {"GITLAB_TOKEN": {"env": "SCRIPT_TOKEN"}},
    })
    event = Event(
        "control-delivery", "gitlab-register-hook", "merge_request", "gitlab",
        context={"gitlab": {
            "projectId": "42", "projectPath": "team/service", "mergeRequestIid": "7",
        }},
    )
    ticket = ScriptAdmissionTicket(
        "inv-script", "ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", event,
        datetime.now(timezone.utc) + timedelta(seconds=10),
        asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future(),
    )
    control = type("ScriptControlFixture", (), {})()
    control.config = type("Config", (), {
        "channels": [type("Channel", (), {"name": event.channel, "type": "webhook-script", "script": script})()],
    })()
    control._private_env = {"PATH": "/bin:/usr/bin", "SCRIPT_TOKEN": "selected-token"}

    result = await Control.run(control, event, ticket)

    assert result is None
    assert ticket.started.done() and ticket.started.exception() is None
    assert output.read_text() == "control-delivery|unset|selected-token"


def _memory_ticket(config, invocation_id: str, *, session_ref: str = "b" * 64) -> AdmissionTicket:
    routed = RoutedEvent(
        Event(invocation_id, "gitlab-mr-review", "merge_request", "gitlab", payload={"title": "fix"}),
        "w", "s",
    )
    return AdmissionTicket(
        invocation_id, config.agent.namespace, config.agent.uid, "a" * 64, session_ref, routed,
        datetime.now(timezone.utc) + timedelta(seconds=30),
        asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future(),
    )


def _memory_endpoint(config) -> WorkspaceEndpoint:
    return WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64), "workspace", "pod-1", "http://mini",
    )


async def _memory_mini(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/configure":
        body = json.loads(request.content)
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"},
                              json={"configVersion": body["public"]["configVersion"]})
    if request.url.path == "/readyz":
        return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod-1"}, json={"ready": True})
    raise AssertionError(f"unexpected mini request {request.url}")


async def test_assembled_memory_context_reaches_start_and_managed_system_once(
    monkeypatch, tmp_path: Path
) -> None:
    """I7: one `prepare_ach_memory` call per non-script invocation, appended after the
    rendered user prompt; two independent Starts see two independently returned standing
    markers through the same private boot-resolved project/identity/auth; the final
    managed fixture composes persona + `## Memory Tools` + exact TOOLS_SPEC exactly once;
    no credential/header/backend URL ever reaches Start/public/catalog/system."""
    from ach_runtime.engine.config import to_fixture
    from ach_runtime.engine.opencode import EngineReceipt
    from ach_runtime.execution.auth import create_authenticated_app
    from ach_runtime.execution.service import ExecutionService
    from ach_runtime.execution.sessions import SessionRegistry
    from ach_runtime.facades import _memory_client
    from ach_runtime.facades._memory_client import TOOLS_SPEC

    calls: list[dict[str, object]] = []

    async def fake_call(endpoint, headers, tool, args, *, identity):
        calls.append({
            "endpoint": endpoint, "headers": dict(headers), "tool": tool,
            "args": dict(args), "identity": identity,
        })
        return json.dumps({"text": f"standing-marker-{len(calls)}"})

    monkeypatch.setattr(_memory_client, "call_ach_memory", fake_call)

    config = _config()
    config.memory.ach_memory.endpoint = "https://memory.example/mcp"
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek-value"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(_memory_mini),
    )

    class RecordingEngine:
        def __init__(self) -> None:
            self.invocations: list[object] = []

        async def start(self, invocation, *, home, scratch, password=None):
            self.invocations.append(invocation)
            return EngineReceipt(native_session_id=f"native-{len(self.invocations)}", input_id="input")

        async def wait_settled(self, invocation_id):
            return "completed"

        async def status(self, invocation_id):
            return "completed"

        async def cancel(self, invocation_id, reason, *, _halt_budget=None):
            return None

    engine = RecordingEngine()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"), workspace_dir=workspace,
        workspace_ref="a" * 64,
    )
    mini_app = create_authenticated_app(
        service, verify_key=control.verify_key, workspace_name="workspace", instance_id="pod-1",
    )
    asgi_transport = httpx.ASGITransport(app=mini_app)
    control.mini_client = httpx.AsyncClient(transport=asgi_transport, timeout=10)
    control.delivery.client = httpx.AsyncClient(transport=asgi_transport, timeout=None)
    endpoint = _memory_endpoint(config)

    async def endpoint_for_ticket(_ticket):
        return endpoint

    control.delivery._endpoint = endpoint_for_ticket
    try:
        await control.delivery.start(_memory_ticket(config, "inv-1", session_ref="b" * 64))
        await control.delivery.start(_memory_ticket(config, "inv-2", session_ref="c" * 64))
        assert len(engine.invocations) == 2
        inv1, inv2 = engine.invocations

        assert "standing-marker-1" in inv1.prompt and "standing-marker-2" in inv2.prompt
        assert inv1.prompt != inv2.prompt
        assert inv1.prompt == "Review fix for gitlab-reviewer\n\n## Memory\n\nstanding-marker-1"

        # Same private boot-resolved project/identity/auth both times.
        assert calls[0]["endpoint"] == calls[1]["endpoint"] == "https://memory.example/mcp"
        assert calls[0]["identity"] is calls[1]["identity"]
        assert calls[0]["headers"] == calls[1]["headers"] == {"x-ach-key": "ek-value"}
        assert calls[0]["args"]["project_slug"] == calls[1]["args"]["project_slug"]
        assert calls[0]["args"]["project_slug"]

        for inv in (inv1, inv2):
            assert inv.catalog["mcp"].get("memory") is not None
            fixture = to_fixture(
                inv.public, managed_agent=inv.managed_agent, catalog=inv.catalog,
                resolved_system_prompt=inv.resolved_system_prompt,
            )
            assert fixture["systemPrompt"].count("## Memory Tools") == 1
            assert fixture["systemPrompt"].endswith(f"## Memory Tools\n{TOOLS_SPEC}")

        public_dump = json.dumps(control.public.model_dump(by_alias=True, mode="json"))
        assert "memory.example" not in public_dump and "ek-value" not in public_dump
        catalog = control._catalog_for(endpoint)
        catalog_dump = json.dumps(catalog)
        assert "memory.example" not in catalog_dump and "ek-value" not in catalog_dump
        assert catalog["memoryToolsSpec"] == TOOLS_SPEC
        assert "ek-value" not in inv1.prompt and "memory.example" not in inv1.prompt
    finally:
        await control.close()


async def test_assembled_memory_absent_and_degraded_match_legacy(monkeypatch) -> None:
    """I7: absent config adds no section/spec/call/availability decision; a missing
    endpoint uses the exact existing no-endpoint note; an unresolved bearer leaves the
    facade absent and follows the copied helper's own load/degraded behavior (no second
    startup probe, no auth value ever in public state); a genuine backend failure yields
    the exact existing unavailable section without failing the run; an empty context
    gives the exact "No standing context yet." text. Active TOOLS_SPEC stays present, and
    the raw backend MCP id stays excluded, in every configured/degraded case."""
    from ach_runtime.config.schema import AchMemoryAuthBearer
    from ach_runtime.facades import _memory_client

    async def succeed(ticket_label: str, config, expected_prompt: str, *, expect_available) -> None:
        control = await assemble(
            config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
            ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(_memory_mini),
        )
        try:
            endpoint = _memory_endpoint(config)
            request = await control.prepare(_memory_ticket(config, ticket_label), endpoint)
            assert request.prompt == expected_prompt
            assert request.memory_available is expect_available
            catalog = control._catalog_for(endpoint)
            if config.memory is None:
                assert "memoryToolsSpec" not in catalog
            else:
                assert catalog["memoryToolsSpec"]  # active TOOLS_SPEC remains present
                assert "ach-memory" not in catalog["mcp"]  # raw backend id stays excluded
        finally:
            await control.close()

    # 1) absent config: no memory block at all.
    absent_config = _config()
    absent_config.memory = None
    await succeed("inv-absent", absent_config, "Review fix for gitlab-reviewer", expect_available=None)

    # 2) configured but endpoint missing: exact legacy no-endpoint note.
    missing_endpoint_config = _config()
    await succeed(
        "inv-missing", missing_endpoint_config,
        "Review fix for gitlab-reviewer\n\n## Memory\n\nUnavailable (no endpoint).",
        expect_available=False,
    )

    # 3) unresolved bearer: facade absent, but prepare still follows the helper's own
    # degraded behavior (no inventing a startup probe, no auth value in public state).
    bearer_config = _config()
    bearer_config.memory.ach_memory.endpoint = "https://memory.example/mcp"
    bearer_config.memory.ach_memory.auth = AchMemoryAuthBearer(
        type="bearer", env="ACH_MEMORY_BEARER_NEVER_SET"
    )

    async def fail_without_headers(endpoint, headers, tool, args, *, identity):
        if not headers:
            raise RuntimeError("unauthenticated")
        return json.dumps({"text": "should not be reached"})

    monkeypatch.setattr(_memory_client, "call_ach_memory", fail_without_headers)
    control = await assemble(
        bearer_config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(_memory_mini),
    )
    try:
        assert control.memory_facade is None
    finally:
        await control.close()
    await succeed(
        "inv-bearer", bearer_config,
        "Review fix for gitlab-reviewer\n\n## Memory\n\nUnavailable (context load failed).",
        expect_available=False,
    )

    # 4) backend failure: exact unavailable section, never fails the run.
    failing_config = _config()
    failing_config.memory.ach_memory.endpoint = "https://memory.example/mcp"

    async def fail_call(endpoint, headers, tool, args, *, identity):
        raise RuntimeError("backend unreachable")

    monkeypatch.setattr(_memory_client, "call_ach_memory", fail_call)
    await succeed(
        "inv-fail", failing_config,
        "Review fix for gitlab-reviewer\n\n## Memory\n\nUnavailable (context load failed).",
        expect_available=False,
    )

    # 5) empty context: exact "No standing context yet."
    empty_config = _config()
    empty_config.memory.ach_memory.endpoint = "https://memory.example/mcp"

    async def empty_call(endpoint, headers, tool, args, *, identity):
        return json.dumps({"text": ""})

    monkeypatch.setattr(_memory_client, "call_ach_memory", empty_call)
    await succeed(
        "inv-empty", empty_config,
        "Review fix for gitlab-reviewer\n\n## Memory\n\nNo standing context yet.",
        expect_available=True,
    )


async def test_concurrent_memory_availability_is_captured_per_start(monkeypatch, tmp_path: Path) -> None:
    """I7: one false and one true Start across two sessions; only the false one lacks
    `catalog.mcp.memory`, both retain their own appropriate user section/spec, and every
    other MCP entry is unchanged either way."""
    from ach_runtime.engine.opencode import EngineReceipt
    from ach_runtime.execution.auth import create_authenticated_app
    from ach_runtime.execution.service import ExecutionService
    from ach_runtime.execution.sessions import SessionRegistry
    from ach_runtime.facades import _memory_client

    call_count = {"n": 0}

    async def alternating_call(endpoint, headers, tool, args, *, identity):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("backend unreachable for the first Start")
        return json.dumps({"text": "standing context"})

    monkeypatch.setattr(_memory_client, "call_ach_memory", alternating_call)

    config = _config()
    config.memory.ach_memory.endpoint = "https://memory.example/mcp"
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(_memory_mini),
    )

    class RecordingEngine:
        def __init__(self) -> None:
            self.invocations: list[object] = []

        async def start(self, invocation, *, home, scratch, password=None):
            self.invocations.append(invocation)
            return EngineReceipt(native_session_id=f"native-{len(self.invocations)}", input_id="input")

        async def wait_settled(self, invocation_id):
            return "completed"

        async def status(self, invocation_id):
            return "completed"

        async def cancel(self, invocation_id, reason, *, _halt_budget=None):
            return None

    engine = RecordingEngine()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"), workspace_dir=workspace,
        workspace_ref="a" * 64,
    )
    mini_app = create_authenticated_app(
        service, verify_key=control.verify_key, workspace_name="workspace", instance_id="pod-1",
    )
    asgi_transport = httpx.ASGITransport(app=mini_app)
    control.mini_client = httpx.AsyncClient(transport=asgi_transport, timeout=10)
    control.delivery.client = httpx.AsyncClient(transport=asgi_transport, timeout=None)
    endpoint = _memory_endpoint(config)

    async def endpoint_for_ticket(_ticket):
        return endpoint

    control.delivery._endpoint = endpoint_for_ticket
    try:
        await control.delivery.start(_memory_ticket(config, "inv-false", session_ref="b" * 64))
        await control.delivery.start(_memory_ticket(config, "inv-true", session_ref="c" * 64))
        assert len(engine.invocations) == 2
        false_inv, true_inv = engine.invocations

        assert false_inv.prompt.endswith("## Memory\n\nUnavailable (context load failed).")
        assert true_inv.prompt.endswith("## Memory\n\nstanding context")

        assert "memory" not in false_inv.catalog["mcp"]
        assert true_inv.catalog["mcp"]["memory"] is not None

        other_servers_false = {k: v for k, v in false_inv.catalog["mcp"].items() if k != "memory"}
        other_servers_true = {k: v for k, v in true_inv.catalog["mcp"].items() if k != "memory"}
        assert other_servers_false == other_servers_true
    finally:
        await control.close()


async def test_start_failure_logs_redacted_reason(monkeypatch, caplog):
    import logging
    from types import SimpleNamespace

    from ach_runtime.harness import control as control_module

    monkeypatch.setattr(control_module, "_alarm", lambda reason, _ticket=None: None)
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"

    class FailingDelivery:
        async def start(self, _ticket):
            raise ConnectionError(
                f"pod unreachable https://user:hunter2@pod.local/x Authorization: Bearer {secret} " + "x " * 400
            )

    control = Control.__new__(Control)
    control.delivery = FailingDelivery()
    control._prepared = {"inv-fail": None}
    ticket = SimpleNamespace(invocation_id="inv-fail", deadline_at=datetime(2999, 1, 1, tzinfo=timezone.utc))
    event = RoutedEvent(Event("evt-fail", "channel", "kind", "source"), "workspace", "session")

    with caplog.at_level(logging.ERROR, logger=control_module.__name__):
        with pytest.raises(ConnectionError):
            await control.run(event, ticket)

    record = next(r for r in caplog.records if r.getMessage().startswith("run start failed"))
    assert record.getMessage().endswith(" (channel channel, event evt-fail)")
    assert record.reason.startswith("ConnectionError: pod unreachable")
    assert "hunter2" not in record.reason and secret not in record.reason
    assert len(record.reason) <= 303


def test_failure_reason_keeps_workspace_name_and_redacts_tokens():
    from ach_runtime.harness.control import _failure_reason
    from ach_runtime.harness.workspaces import WorkspaceConflict

    name = "agent-gitlab-reviewer-0123456789abcdef0123"
    reason = _failure_reason(WorkspaceConflict(f"{name}: the Workspace is already hosted"))
    assert reason == f"WorkspaceConflict: {name}: the Workspace is already hosted"

    short_ek, long_sk = "ek-private", "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    reason = _failure_reason(RuntimeError(f"{name} rejected {short_ek} and {long_sk}"))
    assert name in reason
    assert short_ek not in reason and long_sk not in reason


async def test_start_failure_reason_reaches_real_control_log_output(monkeypatch, capsys):
    """Render through the control process's real path: configure_logging() plus the root
    logger with no handlers (stdlib lastResort), not caplog -- which sees `extra` fields
    that the production output never prints."""
    import logging
    from types import SimpleNamespace

    from ach_runtime.harness import control as control_module
    from ach_runtime.observability.runs import configure_logging

    monkeypatch.setattr(control_module, "_alarm", lambda reason, _ticket=None: None)
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"

    class FailingDelivery:
        async def start(self, _ticket):
            raise ConnectionError(f"pod unreachable Authorization: Bearer {secret}")

    control = Control.__new__(Control)
    control.delivery = FailingDelivery()
    control._prepared = {}
    ticket = SimpleNamespace(invocation_id="inv-fail", deadline_at=datetime(2999, 1, 1, tzinfo=timezone.utc))
    event = RoutedEvent(Event("evt-fail", "channel", "kind", "source"), "workspace", "session")

    root = logging.getLogger()
    saved = root.handlers[:]
    for handler in saved:
        root.removeHandler(handler)
    try:
        configure_logging()
        capsys.readouterr()
        with pytest.raises(ConnectionError):
            await control.run(event, ticket)
    finally:
        for handler in saved:
            root.addHandler(handler)
    err = capsys.readouterr().err
    assert "run start failed: ConnectionError: pod unreachable" in err
    assert secret not in err


async def test_observe_run_backs_off_for_long_runs_and_resets_on_state_change(monkeypatch) -> None:
    from ach_runtime.harness import control as control_module

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    now = [datetime(2026, 10, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(control_module, "_clock", lambda: now[0])
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        if delay > 0:
            sleeps.append(delay)
            now[0] += timedelta(seconds=delay)
        await real_sleep(0)

    monkeypatch.setattr(control_module.asyncio, "sleep", fake_sleep)
    # 23 x preparing (past the 5 s fast window), then a state change, then terminal.
    states = ["preparing"] * 23 + ["running"] * 6 + ["completed"]

    def status_for(state: str) -> RunStatus:
        return RunStatus(
            invocationId="observe-backoff", channel="gitlab-mr-review",
            workspaceRef="a" * 64, sessionRef="b" * 64, runId="run-1",
            configVersion=control.public.config_version,
            deadlineAt=datetime(2026, 10, 1, 1, tzinfo=timezone.utc), state=state,
        )

    async def status(*_args):
        return status_for(states.pop(0))

    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
        "workspace", "pod-1", "http://mini",
    )
    _publish(control, endpoint)
    control.delivery.status = status
    try:
        result = await control._observe_run(endpoint, status_for("preparing"))
        assert result.state == "completed"
    finally:
        await control.close()
    # 0.25 s for the first 5 s of the run, then back off to at most 1 s.
    assert sleeps[:20] == [0.25] * 20
    assert sleeps[20:23] == [0.5, 1.0, 1.0]
    assert max(sleeps) == 1.0
    # The preparing -> running change resets the cadence to 0.25 s.
    assert sleeps[23] == 0.25


async def test_observe_run_sleep_never_overshoots_the_deadline(monkeypatch) -> None:
    from ach_runtime.harness import control as control_module

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    monkeypatch.setattr(control_module, "_alarm", lambda reason, _ticket=None: None)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    deadline = start + timedelta(seconds=30.1)
    now = [start]
    monkeypatch.setattr(control_module, "_clock", lambda: now[0])
    sleeps: list[float] = []
    cancels: list[datetime] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        now[0] += timedelta(seconds=delay)
        await real_sleep(0)

    monkeypatch.setattr(control_module.asyncio, "sleep", fake_sleep)

    def status_for(state: str) -> RunStatus:
        return RunStatus(
            invocationId="observe-deadline", channel="gitlab-mr-review",
            workspaceRef="a" * 64, sessionRef="b" * 64, runId="run-1",
            configVersion=control.public.config_version, deadlineAt=deadline, state=state,
        )

    async def status(*_args):
        return status_for("cancelled" if cancels else "running")

    async def cancel(*_args):
        cancels.append(now[0])

    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
        "workspace", "pod-1", "http://mini",
    )
    _publish(control, endpoint)
    control.delivery.status = status
    control.delivery.cancel = cancel
    try:
        await control._observe_run(endpoint, status_for("running"))
    finally:
        await control.close()
    assert cancels == [deadline]


def _workspace_hook_mini(calls: list[tuple[str, object]], *, hook_status: int = 200):
    async def mini(request: httpx.Request) -> httpx.Response:
        headers = {"X-ACH-Instance-Id": "pod-1"}
        body = json.loads(request.content) if request.content else None
        calls.append((request.url.path, body))
        if request.url.path == "/configure":
            return httpx.Response(200, headers=headers,
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers=headers, json={})
        if request.url.path.startswith("/workspace/hooks/"):
            if hook_status != 200:
                return httpx.Response(hook_status, headers=headers,
                                      json={"type": "SessionHookFailed", "message": "hook exited 4"})
            return httpx.Response(200, headers=headers, json={"status": "ok"})
        raise AssertionError(f"unexpected mini request {request.url}")
    return mini


def _hook_endpoint(config) -> WorkspaceEndpoint:
    return WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                             "workspace", "pod-1", "http://mini")


async def test_workspace_start_hook_configures_then_runs() -> None:
    config = _config(hooks={"workspaceStart": {"script": "true", "timeoutSeconds": 30}})
    calls: list[tuple[str, object]] = []
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(_workspace_hook_mini(calls)),
    )
    try:
        await control.run_workspace_hook(_hook_endpoint(config), "start", restored=True)
        paths = [path for path, _ in calls]
        assert paths == ["/configure", "/readyz", "/workspace/hooks/start"]
        assert calls[-1][1] == {"restored": True}
    finally:
        await control.close()


async def test_workspace_stop_hook_runs_without_configure() -> None:
    config = _config(hooks={"workspaceStop": {"script": "true"}})
    calls: list[tuple[str, object]] = []
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(_workspace_hook_mini(calls)),
    )
    try:
        await control.run_workspace_hook(_hook_endpoint(config), "stop")
        assert [path for path, _ in calls] == ["/workspace/hooks/stop"]
    finally:
        await control.close()


async def test_workspace_hook_not_configured_makes_no_call() -> None:
    config = _config()
    calls: list[tuple[str, object]] = []
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(_workspace_hook_mini(calls)),
    )
    try:
        await control.run_workspace_hook(_hook_endpoint(config), "start")
        await control.run_workspace_hook(_hook_endpoint(config), "stop")
        assert calls == []
    finally:
        await control.close()


async def test_workspace_hook_failure_raises() -> None:
    config = _config(hooks={"workspaceStop": {"script": "exit 4"}})
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(_workspace_hook_mini([], hook_status=502)),
    )
    try:
        with pytest.raises(WorkspaceAssemblyUnavailable, match="workspaceStop hook failed"):
            await control.run_workspace_hook(_hook_endpoint(config), "stop")
    finally:
        await control.close()


async def test_workspace_hook_gets_the_hook_timeout_and_facade_ownership(monkeypatch) -> None:
    """The hook may call the facades (ach-mcp) while its workspace is not published in
    the manager's active endpoints, and the mini call must not inherit the client's 10 s read timeout."""
    config = _config(hooks={"workspaceStart": {"script": "true", "timeoutSeconds": 300}})
    endpoint = _hook_endpoint(config)
    seen: dict[str, object] = {}
    control_ref: list = []

    async def mini(request: httpx.Request) -> httpx.Response:
        headers = {"X-ACH-Instance-Id": "pod-1"}
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers=headers,
                                  json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/readyz":
            return httpx.Response(200, headers=headers, json={})
        seen["timeout"] = request.extensions["timeout"]["read"]
        seen["owned_during_hook"] = await control_ref[0]._owned_live(endpoint.name)
        return httpx.Response(200, headers=headers, json={"status": "ok"})

    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    control_ref.append(control)
    try:
        assert control.manager.active_endpoint(endpoint.ref.workspace_ref) is None
        await control.run_workspace_hook(endpoint, "start")
        assert seen == {"timeout": 310, "owned_during_hook": True}
        assert not await control._owned_live(endpoint.name)
    finally:
        await control.close()


@pytest.mark.parametrize("hooks,expected", [
    (None, {"sessionStart", "sessionRestore", "sessionSuspend"}),
    ({"workspaceStop": {"script": "true"}},
     {"sessionStart", "sessionRestore", "sessionSuspend", "workspaceStop"}),
])
async def test_configure_payload_stays_readable_by_older_execution_pods(hooks, expected) -> None:
    """A warm workspace pod keeps its older image until it closes and rejects unknown
    /configure keys, so hook keys added later are only sent when set."""
    config = _config(hooks=hooks)
    calls: list[tuple[str, object]] = []
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(_workspace_hook_mini(calls)),
    )
    try:
        await control.configure_workspace(_hook_endpoint(config))
        public = calls[0][1]["public"]
        assert set(public["hooks"]) == expected
        assert "maxStallSeconds" not in public["limits"]
        assert "denyTools" not in public["engine"]
        # An execution pod on this version still parses what it receives.
        assert PublicConfig.model_validate(public).config_version == control.public.config_version
    finally:
        await control.close()


async def test_configure_payload_sends_deny_tools_when_set() -> None:
    config = _config()
    config = config.model_copy(
        update={"engine": config.engine.model_copy(update={"deny_tools": ["question"]})}
    )
    calls: list[tuple[str, object]] = []
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler),
        mini_transport=httpx.MockTransport(_workspace_hook_mini(calls)),
    )
    try:
        await control.configure_workspace(_hook_endpoint(config))
        assert calls[0][1]["public"]["engine"]["denyTools"] == ["question"]
    finally:
        await control.close()


async def _reconnect_with_registry(writer: str | None, *, persistence: bool = True) -> list:
    """Reconnect a pod `pod-1` while the S3 registry records `writer` for its workspace."""
    from types import SimpleNamespace

    async def mini(request: httpx.Request) -> httpx.Response:
        headers = {"X-ACH-Instance-Id": "pod-1"}
        if request.url.path == "/configure":
            body = json.loads(request.content)
            return httpx.Response(200, headers=headers, json={"configVersion": body["public"]["configVersion"]})
        if request.url.path == "/runs":
            return httpx.Response(200, headers=headers, json=[])
        return httpx.Response(200, headers=headers, json={})

    config = _config()
    control = await assemble(
        config, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_catalog_handler), mini_transport=httpx.MockTransport(mini),
    )
    restored: list = []

    async def restore_workspace(endpoint) -> None:
        restored.append(endpoint)

    async def current_writer(ref):
        return writer

    control.config = _config(persistence=persistence)
    control.storage = SimpleNamespace(current_writer=current_writer)
    control.lifecycle = SimpleNamespace(
        workspace_state_ref=lambda endpoint: ("workspace", endpoint.ref.workspace_ref),
        restore_workspace=restore_workspace,
    )
    endpoint = WorkspaceEndpoint(WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
                                 "workspace", "pod-1", "http://mini")
    try:
        _publish(control, endpoint)
        await control.reconnect_workspace(endpoint)
        assert restored in ([], [endpoint])
        return restored
    finally:
        control.lifecycle = None
        await control.close()


async def test_reconnect_restores_a_pod_the_harness_did_not_restore() -> None:
    assert len(await _reconnect_with_registry("old-pod")) == 1


async def test_reconnect_keeps_the_pod_it_restored() -> None:
    assert await _reconnect_with_registry("pod-1") == []


async def test_reconnect_without_registry_does_not_restore() -> None:
    assert await _reconnect_with_registry(None) == []
    assert await _reconnect_with_registry("old-pod", persistence=False) == []

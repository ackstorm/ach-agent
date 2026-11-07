from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from ach_runtime.channels.a2a import (
    A2AAgentExecutorBridge,
    A2A_OUTPUT_INSTRUCTIONS,
    RESTARTING_QUEUED,
    RESTARTING_RUNNING,
    build_a2a_app,
    extract_terminal,
    make_a2a_agent_card,
    validate_a2a_reply,
)
from ach_runtime.channels.event import Event
from ach_runtime.channels.routing import route
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.execution.wire import RunStatus
from ach_runtime.harness.admission import Admission
from ach_runtime.harness.refs import session_ref, workspace_ref
from ach_runtime.harness.app import create_app

SECRET = "test-a2a-secret"


def _channel(name: str = "review", routing: dict | None = None) -> ChannelConfig:
    raw = {"name": name, "type": "a2a", "a2a": {"auth": {"env": "A2A_TEST_SECRET"}}}
    if routing is not None:
        raw["routing"] = routing
    return ChannelConfig.model_validate(raw)


class _Queue:
    def __init__(self) -> None:
        self.events = []

    async def enqueue_event(self, event) -> None:
        self.events.append(event)


class _Context:
    def __init__(self, task_id: str, context_id: str = "ctx", text: str = "hello") -> None:
        self.task_id, self.context_id, self.text = task_id, context_id, text
        self.call_context = SimpleNamespace(state={"headers": {"x-a2a-custom-api-key": SECRET}})

    def get_user_input(self) -> str:
        return self.text


def _admission(runner) -> Admission:
    return Admission(
        namespace="ach", agent_uid="agent", max_concurrent_invocations=10,
        max_queued_total=10, idempotency_window_seconds=60, max_invocation_seconds=60,
        workspace_max_concurrent_sessions=10, channel_concurrency=None,
        clock=lambda: datetime.now(UTC), runner=runner,
    )


def _control(config, runner) -> SimpleNamespace:
    return SimpleNamespace(config=SimpleNamespace(channels=[config], agent=SimpleNamespace(name="test-agent"), identity_environment="test-env"), admission=_admission(runner), _stopping=False, progress={}, storage=None)


def _completed(ticket, text: str = "answer") -> RunStatus:
    ticket.mark_started()
    return RunStatus(
        invocationId=ticket.invocation_id, channel=ticket.routed_event.channel,
        workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref, runId="run-1",
        configVersion="a" * 64, deadlineAt=ticket.deadline_at, state="completed",
        result={"text": '{"action":"a2a_reply","text":"' + text + '"}'},
    )


def test_sdk_card_has_legacy_aliases_and_native_interface() -> None:
    app = FastAPI()
    bridge = A2AAgentExecutorBridge(_control(_channel(), lambda *_: None), _channel())
    app.mount("/a2a/review", build_a2a_app(make_a2a_agent_card("reviewer", "review"), bridge))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in ("agent-card.json", "agent.json"):
                response = await client.get(f"/a2a/review/.well-known/{path}")
                card = response.json()
                assert response.status_code == 200
                assert card["url"].endswith("/a2a/review")
                assert card["name"] == "reviewer"
                assert [skill["id"] for skill in card["skills"]] == ["review"]
                assert card["supportedInterfaces"][0]["url"] == card["url"]
                assert card["defaultInputModes"] == ["text"]
                assert card["defaultOutputModes"] == ["text"]
                assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
            head = await client.head("/a2a/review/.well-known/agent-card.json")
            assert head.status_code != 401

    asyncio.run(scenario())


def test_sdk_auth_guard_protects_all_task_operations_and_exact_exemptions(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    control = _control(config, lambda *_: None)
    bridge = A2AAgentExecutorBridge(control, config)
    app = FastAPI()
    app.mount("/a2a/review", build_a2a_app(make_a2a_agent_card("reviewer", "review"), bridge))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for method, path, body in [
                ("POST", "/a2a/review/", {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {}}),
                ("POST", "/a2a/review/", {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {}}),
                ("POST", "/a2a/review/", {"jsonrpc": "2.0", "id": 1, "method": "ListTasks", "params": {}}),
                ("POST", "/a2a/review/", {"jsonrpc": "2.0", "id": 1, "method": "CancelTask", "params": {}}),
                ("POST", "/a2a/review/message:send", {}),
                ("GET", "/a2a/review/tasks", None),
                ("GET", "/a2a/review/tasks/missing", None),
                ("POST", "/a2a/review/tasks/missing:cancel", {}),
                ("GET", "/a2a/review/private/.well-known/agent-card.json", None),
                ("POST", "/a2a/review/.well-known/agent-card.json", {}),
            ]:
                response = await client.request(method, path, json=body)
                assert response.status_code == 401, (method, path, response.text)
            for path in ("agent-card.json", "agent.json"):
                assert (await client.get(f"/a2a/review/.well-known/{path}")).status_code == 200
                assert (await client.head(f"/a2a/review/.well-known/{path}")).status_code != 401
            auth = {"x-a2a-custom-api-key": SECRET, "a2a-version": "1.0"}
            assert (await client.post("/a2a/review/", json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {}}, headers=auth)).status_code != 401
            missing = await client.get("/a2a/review/tasks/missing", headers=auth)
            assert missing.status_code == 404

    asyncio.run(scenario())
    asyncio.run(control.admission.close())


def test_a2a_routing_defaults_and_independent_overrides() -> None:
    event = Event(
        event_id="a2a:task-1", channel="review", type="a2a", source="a2a",
        payload={"text": "hello", "task_id": "task-1", "context_id": "context-1"},
        context={"a2a": {"taskId": "task-1", "contextId": "context-1"}},
    )
    cfg = _channel()
    routed = route(event, cfg)
    assert (routed.workspace_key, routed.session_key) == ("a2a:review", "context-1")
    fallback = route(event, _channel(routing={"workspaceKey": "custom:{{ payload.task_id }}"}))
    assert (fallback.workspace_key, fallback.session_key) == ("custom:task-1", "context-1")


def test_ingress_mounts_a2a_sdk_and_uses_live_readiness(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    control = _control(config, lambda *_: None)
    app = create_app(control)
    assert set(app.state.a2a_bridges) == {"review"}
    mounted = next(item for item in app.routes if getattr(item, "path", None) == "/a2a/review")
    assert mounted.app is not None

    def request(request_id: int) -> dict:
        return {
            "jsonrpc": "2.0", "id": request_id, "method": "SendMessage", "params": {
                "message": {
                    "messageId": f"message-{request_id}", "contextId": "readiness-context",
                    "role": "ROLE_USER", "parts": [{"text": "hello"}],
                },
            },
        }

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            assert (await client.get("/a2a/review/.well-known/agent-card.json")).status_code == 200
            app.state.ready = False
            response = await client.post(
                "/a2a/review/",
                headers={"x-a2a-custom-api-key": SECRET, "a2a-version": "1.0"},
                json=request(1),
            )
            assert response.status_code == 200, response.text
            failed = response.json()["result"]["task"]
            assert failed["status"]["state"] == "TASK_STATE_FAILED"
            assert failed["status"]["message"]["parts"][0]["text"] == "Control is not ready"
            assert control.admission.queued_total == 0
            app.state.ready = True
            control._stopping = True
            draining = await client.post(
                "/a2a/review/",
                headers={"x-a2a-custom-api-key": SECRET, "a2a-version": "1.0"},
                json=request(2),
            )
            assert draining.status_code == 200, draining.text
            failed = draining.json()["result"]["task"]
            assert failed["status"]["state"] == "TASK_STATE_FAILED"
            assert failed["status"]["message"]["parts"][0]["text"] == "Control is not ready"
            assert control.admission.queued_total == 0

    asyncio.run(scenario())
    asyncio.run(control.admission.close())


def test_mounted_sdk_send_message_uses_real_admission_future(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    deliveries = []

    async def runner(routed, ticket):
        deliveries.append((routed, ticket))
        ticket.mark_started()
        return _completed(ticket, "sdk-answer")

    control = _control(config, runner)
    app = create_app(control)
    app.state.ready = True
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {
            "message": {
                "messageId": "message-1", "contextId": "sdk-context",
                "role": "ROLE_USER", "parts": [{"text": "hello from SDK"}],
            },
        },
    }

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/a2a/review/", json=request,
                headers={"x-a2a-custom-api-key": SECRET, "a2a-version": "1.0"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert "result" in body, body
        task = body["result"]["task"]
        assert task["id"]
        assert task["contextId"] == "sdk-context"
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert task["status"]["message"]["parts"][0]["text"] == "sdk-answer"
        assert len(deliveries) == 1
        assert deliveries[0][0].event_id == f"a2a:{task['id']}"
        assert deliveries[0][0].event.payload["text"] == "hello from SDK"

    asyncio.run(scenario())
    asyncio.run(control.admission.close())


def test_mounted_sdk_stream_delivers_progress_then_the_reply(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()

    async def runner(routed, ticket):
        ticket.mark_started()
        await asyncio.sleep(0)
        for emit in tuple(control.progress[ticket.invocation_id]):
            await emit("Reading the repository")
        return _completed(ticket, "streamed-answer")

    control = _control(config, runner)
    app = create_app(control)
    app.state.ready = True
    request = {
        "jsonrpc": "2.0", "id": 1, "method": "SendStreamingMessage", "params": {
            "message": {
                "messageId": "message-1", "contextId": "sdk-context",
                "role": "ROLE_USER", "parts": [{"text": "hello"}],
            },
        },
    }

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            card = (await client.get("/a2a/review/.well-known/agent-card.json")).json()
            assert card["capabilities"]["streaming"] is True
            response = await client.post(
                "/a2a/review/", json=request,
                headers={"x-a2a-custom-api-key": SECRET, "a2a-version": "1.0"},
            )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        updates = [
            json.loads(line[len("data:"):])["result"]["statusUpdate"]["status"]
            for line in response.text.splitlines()
            if line.startswith("data:") and "statusUpdate" in line
        ]
        seen = [(u["state"], u.get("message", {}).get("parts", [{}])[0].get("text")) for u in updates]
        assert sorted(seen[:-1], key=str) == [
            ("TASK_STATE_WORKING", "Reading the repository"),
            ("TASK_STATE_WORKING", None),
        ]
        assert seen[-1] == ("TASK_STATE_COMPLETED", "streamed-answer")
        assert control.progress == {}

    asyncio.run(scenario())
    asyncio.run(control.admission.close())


def test_terminal_extraction_and_strict_a2a_reply_validation() -> None:
    cases = [
        ('```json\n{"action":"a2a_reply","text":"pretty {\\"x\\"}"}\n```', "pretty {\"x\"}"),
        ('preface\n{\n "action": "a2a_reply", "text": "escaped \\\"quote\\\""\n} trailing prose { "action"', 'escaped "quote"'),
    ]
    for raw, expected in cases:
        obj = extract_terminal(raw)
        assert obj is not None
        assert validate_a2a_reply(obj)["text"] == expected
    for value in (
        {"action": "none", "text": "x"},
        {"action": "a2a_reply", "text": "  "},
        {"action": "a2a_reply", "text": "x", "extra": True},
        {"action": "a2a_reply", "text": 1},
    ):
        assert validate_a2a_reply(value) is None
    assert A2A_OUTPUT_INSTRUCTIONS.startswith("<output_format>")


def test_bridge_waits_for_started_then_relays_admitted_result(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    entered, finish = asyncio.Event(), asyncio.Event()
    config = _channel()

    async def runner(routed, ticket):
        entered.set()
        await finish.wait()
        return _completed(ticket)

    control = _control(config, runner)
    bridge = A2AAgentExecutorBridge(control, config)
    queue = _Queue()

    async def scenario() -> None:
        task = asyncio.create_task(bridge.execute(_Context("task-1", "shared", "hello"), queue))
        await entered.wait()
        await asyncio.sleep(0)
        assert queue.events == []
        finish.set()
        await task
        assert [event.status.state for event in queue.events] == [2, 3]  # working, completed
        assert queue.events[-1].status.message.parts[0].text == "answer"

    asyncio.run(scenario())
    asyncio.run(control.admission.close())


def test_same_context_tasks_and_duplicate_waiters_use_independent_shared_futures(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    release_first = asyncio.Event()
    release_second = asyncio.Event()
    calls = []
    config = _channel()

    async def runner(routed, ticket):
        calls.append((routed.event_id, routed.session_key, ticket))
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
            await release_second.wait()
        return _completed(ticket, routed.event_id)

    control = _control(config, runner)
    bridge = A2AAgentExecutorBridge(control, config)
    q1, q1dup, q2 = _Queue(), _Queue(), _Queue()

    async def scenario() -> None:
        one = _Context("one", "same")
        duplicate = _Context("one", "same")
        two = _Context("two", "same")
        t1 = asyncio.create_task(bridge.execute(one, q1))
        t1dup = asyncio.create_task(bridge.execute(duplicate, q1dup))
        t2 = asyncio.create_task(bridge.execute(two, q2))
        await first_started.wait()
        await asyncio.sleep(0)
        assert {item[0] for item in calls} == {"a2a:one"}
        assert {item[1] for item in calls} == {"same"}
        assert bridge._waiter_counts == {"one": 2, "two": 1}
        release_first.set()
        await second_started.wait()
        assert {item[0] for item in calls} == {"a2a:one", "a2a:two"}
        assert len({id(item[2]) for item in calls}) == 2
        release_second.set()
        await asyncio.gather(t1, t1dup, t2)
        assert q1.events[-1].status.message.parts[0].text == "a2a:one"
        assert q1dup.events[-1].status.message.parts[0].text == "a2a:one"
        assert q2.events[-1].status.message.parts[0].text == "a2a:two"

    asyncio.run(scenario())
    asyncio.run(control.admission.close())


def test_cancel_or_caller_cancellation_does_not_cancel_shared_futures(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    start_gate = asyncio.Event()
    release = asyncio.Event()

    async def runner(_routed, ticket):
        start_gate.set()
        await release.wait()
        return None

    control = _control(config, runner)
    bridge = A2AAgentExecutorBridge(control, config)
    queue = _Queue()

    async def scenario() -> None:
        ctx = _Context("waiter", "ctx")
        task = asyncio.create_task(bridge.execute(ctx, queue))
        await start_gate.wait()
        ticket = next(iter(control.admission._tickets.values()))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not ticket.started.cancelled()
        assert not ticket.completed.cancelled()
        second = _Queue()
        wait_start = asyncio.create_task(bridge.execute(_Context("shutdown-wait", "ctx"), second))
        await asyncio.sleep(0)
        await bridge.shutdown()
        await wait_start
        assert all(not item.started.cancelled() and not item.completed.cancelled()
                   for item in control.admission._tickets.values())
        release.set()

    # Caller cancellation and source shutdown both leave Admission's shared futures intact.
    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(control.admission.close())


def test_bridge_rejects_not_ready_and_draining_without_admission(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    control = _control(config, lambda *_: None)
    bridge = A2AAgentExecutorBridge(control, config, ready=lambda: False)
    queue = _Queue()
    asyncio.run(bridge.execute(_Context("not-ready"), queue))
    assert queue.events[-1].status.state == 4
    assert control.admission.queued_total == 0
    bridge = A2AAgentExecutorBridge(control, config)
    control._stopping = True
    queue = _Queue()
    asyncio.run(bridge.execute(_Context("draining"), queue))
    assert queue.events[-1].status.state == 4
    assert control.admission.queued_total == 0
    asyncio.run(control.admission.close())


def test_missing_task_identifier_fails_before_admission(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    control = _control(config, lambda *_: None)
    queue = _Queue()
    asyncio.run(A2AAgentExecutorBridge(control, config).execute(_Context(""), queue))
    assert queue.events[-1].status.state == 4
    assert queue.events[-1].status.message.parts[0].text == "Missing task identifier"
    assert control.admission.queued_total == 0
    asyncio.run(control.admission.close())


def test_channel_secret_is_resolved_again_for_each_request(monkeypatch) -> None:
    config = _channel()

    async def runner(routed, ticket):
        return _completed(ticket, "auth-ok")

    control = _control(config, runner)
    bridge = A2AAgentExecutorBridge(control, config)

    async def scenario() -> None:
        monkeypatch.setenv("A2A_TEST_SECRET", "first-secret")
        first = _Queue()
        first_context = _Context("auth-first")
        first_context.call_context.state["headers"]["x-a2a-custom-api-key"] = "first-secret"
        await bridge.execute(first_context, first)
        assert first.events[-1].status.state == 3
        monkeypatch.setenv("A2A_TEST_SECRET", "rotated-secret")
        old = _Context("auth-old")
        old.call_context.state["headers"]["x-a2a-custom-api-key"] = "first-secret"
        rejected = _Queue()
        await bridge.execute(old, rejected)
        assert rejected.events[-1].status.state == 4
        current = _Context("auth-current")
        current.call_context.state["headers"]["x-a2a-custom-api-key"] = "rotated-secret"
        accepted = _Queue()
        await bridge.execute(current, accepted)
        assert accepted.events[-1].status.state == 3

    asyncio.run(scenario())
    asyncio.run(control.admission.close())


@pytest.mark.parametrize(
    "state,result",
    [
        ("failed", {"text": '{"action":"a2a_reply","text":"no"}'}),
        ("cancelled", {"text": '{"action":"a2a_reply","text":"no"}'}),
        ("unknown", {"text": '{"action":"a2a_reply","text":"no"}'}),
        ("completed", None),
        ("completed", {"text": "not JSON"}),
        ("completed", {"text": '{"action":"a2a_reply","text":"  "}'}),
    ],
)
def test_noncompleted_or_invalid_native_result_fails(state, result, monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()

    async def runner(routed, ticket):
        ticket.mark_started()
        return RunStatus(
            invocationId=ticket.invocation_id, channel=routed.channel,
            workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref, runId="run-1",
            configVersion="a" * 64, deadlineAt=ticket.deadline_at, state=state, result=result,
        )

    control = _control(config, runner)
    queue = _Queue()
    asyncio.run(A2AAgentExecutorBridge(control, config).execute(_Context(f"task-{state}-{result}"), queue))
    assert queue.events[-1].status.state == 4
    assert len(queue.events) == 2
    asyncio.run(control.admission.close())


def test_failed_native_status_preserves_the_provider_error(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()

    async def runner(routed, ticket):
        ticket.mark_started()
        return RunStatus(
            invocationId=ticket.invocation_id, channel=routed.channel,
            workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref, runId="run-1",
            configVersion="a" * 64, deadlineAt=ticket.deadline_at, state="failed",
            error="provider unavailable",
        )

    control = _control(config, runner)
    queue = _Queue()

    async def scenario() -> None:
        await A2AAgentExecutorBridge(control, config).execute(_Context("provider-error"), queue)
        assert queue.events[-1].status.state == 4
        assert queue.events[-1].status.message.parts[0].text == "provider unavailable"
        await control.admission.close()

    asyncio.run(scenario())


def test_skipped_run_is_rejected_with_reason(monkeypatch) -> None:
    from a2a.types.a2a_pb2 import TASK_STATE_REJECTED

    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()

    async def runner(routed, ticket):
        ticket.mark_started()
        return RunStatus(
            invocationId=ticket.invocation_id, channel=routed.channel,
            workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref, runId="run-1",
            configVersion="a" * 64, deadlineAt=ticket.deadline_at, state="skipped",
        )

    control = _control(config, runner)
    queue = _Queue()

    async def scenario() -> None:
        await A2AAgentExecutorBridge(control, config).execute(_Context("skipped"), queue)
        assert queue.events[-1].status.state == TASK_STATE_REJECTED
        assert queue.events[-1].status.message.parts[0].text == "skipped by sessionStart"
        await control.admission.close()

    asyncio.run(scenario())


def test_recovered_ambiguous_task_fails_without_replaying_runner(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    calls = []

    async def runner(*args):
        calls.append(args)
        return None

    control = _control(config, runner)
    bridge = A2AAgentExecutorBridge(control, config)
    event = Event(
        event_id="a2a:recovered", channel="review", type="a2a", source="a2a",
        payload={"text": "hello", "task_id": "recovered", "context_id": "ctx"},
        context={"a2a": {"taskId": "recovered", "contextId": "ctx"}},
    )
    routed = route(event, config)
    invocation_id = control.admission._dedup_key("agent", routed)
    ws_ref = workspace_ref("ach", "agent", routed.workspace_key)
    ss_ref = session_ref(ws_ref, routed.session_key)
    observer = asyncio.Event()

    async def observe():
        await observer.wait()

    async def scenario() -> None:
        control.admission.recover(
            invocation_id=invocation_id, channel="review", workspace_ref=ws_ref,
            session_ref=ss_ref, config_version="a" * 64,
            deadline_at=datetime.now(UTC) + timedelta(minutes=1), instance_id="pod-old",
            run_id="run-old", observer=observe,
        )
        queue = _Queue()
        await bridge.execute(_Context("recovered", "ctx"), queue)
        assert len(queue.events) == 1
        assert queue.events[0].status.state == 4
        assert invocation_id in control.admission._recovered
        assert calls == []
        observer.set()
        await control.admission.close()

    asyncio.run(scenario())


def test_cancel_while_started_is_pending_wakes_task_waiters(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    entered = asyncio.Event()
    release = asyncio.Event()
    config = _channel()

    async def runner(_routed, _ticket):
        entered.set()
        await release.wait()
        return None

    control = _control(config, runner)
    bridge = A2AAgentExecutorBridge(control, config)
    result_queue, cancel_queue = _Queue(), _Queue()

    async def scenario() -> None:
        context = _Context("pending-start", "ctx")
        execution = asyncio.create_task(bridge.execute(context, result_queue))
        await entered.wait()
        ticket = next(iter(control.admission._tickets.values()))
        await bridge.cancel(context, cancel_queue)
        await execution
        assert result_queue.events == []
        assert cancel_queue.events[-1].status.state == 5
        assert not ticket.started.cancelled()
        assert not ticket.completed.cancelled()
        release.set()
        await control.admission.close()
        for future in (ticket.started, ticket.completed):
            if future.done() and not future.cancelled():
                future.exception()

    asyncio.run(scenario())


def test_headers_reach_routing_without_credentials(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()
    seen = []

    async def runner(routed, ticket):
        seen.append(routed)
        return _completed(ticket)

    control = _control(config, runner)
    context = _Context("task-h")
    context.call_context.state["headers"].update(
        {"authorization": "Bearer x", "Cookie": "c", "X-LiteLLM-User-Id": "alice"}
    )
    asyncio.run(A2AAgentExecutorBridge(control, config).execute(context, _Queue()))
    assert seen[0].event.context["headers"] == {"x-litellm-user-id": "alice"}
    asyncio.run(control.admission.close())


def test_routing_error_fails_the_task_instead_of_raising(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel(routing={"sessionKey": "{{ event.headers.x-litellm-user-id }}"})
    control = _control(config, lambda *_: None)
    queue = _Queue()
    asyncio.run(A2AAgentExecutorBridge(control, config).execute(_Context("task-r"), queue))
    assert queue.events[-1].status.state == 4  # failed
    assert "x-litellm-user-id" in queue.events[-1].status.message.parts[0].text
    assert control.admission.queued_total == 0
    asyncio.run(control.admission.close())


async def _stop_run(*, outlive: bool, started: bool, stop: str):
    """Run one task against a runner that never finishes, then stop it with `stop`."""
    release = asyncio.Event()
    running = asyncio.Event()

    async def runner(_routed, ticket):
        if started:
            ticket.mark_started()
        running.set()
        await release.wait()
        return None

    config = _channel()
    control = _control(config, runner)
    bridge = A2AAgentExecutorBridge(control, config)
    bridge.runs_outlive_control = outlive
    queue = _Queue()
    task = asyncio.create_task(bridge.execute(_Context("t1", "c1"), queue))
    await running.wait()
    await asyncio.sleep(0.01)
    if stop == "cancel":
        bridge._cancel_events["t1"].set()
    else:
        await bridge.shutdown()
    await task
    release.set()
    await control.admission.close()
    return queue


def test_shutdown_tells_the_caller_a_started_run_keeps_running(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    queue = asyncio.run(_stop_run(outlive=True, started=True, stop="shutdown"))
    last = queue.events[-1]
    assert last.status.state == 2  # TASK_STATE_WORKING
    assert last.status.message.parts[0].text == RESTARTING_RUNNING


def test_shutdown_fails_a_queued_run_so_the_caller_resends(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    queue = asyncio.run(_stop_run(outlive=True, started=False, stop="shutdown"))
    assert queue.events[-1].status.state == 4
    assert queue.events[-1].status.message.parts[0].text == RESTARTING_QUEUED


@pytest.mark.parametrize("outlive,stop", [(True, "cancel"), (False, "shutdown")])
def test_client_cancel_and_in_process_shutdown_stay_silent(outlive, stop, monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    queue = asyncio.run(_stop_run(outlive=outlive, started=True, stop=stop))
    assert len(queue.events) == 1  # only the plain "working" event


# -- recovery ---------------------------------------------------------------

from a2a.types.a2a_pb2 import Task, TaskStatus  # noqa: E402
from ach_runtime.channels.a2a import RESUME_PROMPT, RUN_LOST  # noqa: E402


class _Store:
    """The StorageTaskStore surface recovery uses, in memory."""

    def __init__(self, *tasks) -> None:
        self.tasks = {t.id: t for t in tasks}
        self.log: list[tuple] = []

    async def prune(self, older_than) -> None: ...

    async def pending(self):
        return list(self.tasks.values())

    async def finish(self, task, state, text) -> None:
        self.log.append(("finish", state, text))

    async def annotate(self, task, metadata) -> None:
        self.log.append(("annotate", dict(metadata)))


def _stored_task(**meta) -> Task:
    task = Task(id="t1", context_id="c1", status=TaskStatus(state=2))
    task.metadata.update({"invocationId": "inv-1", "workspaceKey": "w", "sessionKey": "s", **meta})
    return task


def _status(state: str, text: str | None = "ok", error: str | None = None) -> RunStatus:
    return RunStatus(
        invocationId="inv-1", channel="review", workspaceRef="a" * 64, sessionRef="b" * 64,
        runId="r", configVersion="a" * 64, deadlineAt=datetime.now(UTC), state=state, error=error,
        result={"text": '{"action":"a2a_reply","text":"' + text + '"}'} if state == "completed" else None,
    )


def _recovering(admit_result=None, recovered=None, finished=None, live=()):
    """A bridge on a fake control; `admit` returns tickets whose outcome is `admit_result`."""
    admitted = []

    def admit(routed):
        loop = asyncio.get_running_loop()
        ticket = SimpleNamespace(invocation_id="inv-2", completed=loop.create_future())
        ticket.completed.set_result(admit_result)
        admitted.append(routed)
        return ticket

    control = SimpleNamespace(
        config=SimpleNamespace(workspace=SimpleNamespace(persistence=SimpleNamespace(retention_days=1))),
        admission=SimpleNamespace(
            recovered_run=lambda i: recovered, admit=admit, has_ticket=lambda i: i in live,
        ),
        finished_runs=finished if finished is not None else {},
    )
    return A2AAgentExecutorBridge(control, _channel()), admitted



def test_recovery_writes_a_recovered_runs_reply_into_its_task() -> None:
    async def scenario() -> _Store:
        done = asyncio.get_running_loop().create_future()
        done.set_result(None)  # Admission resolves a recovered run's future with None
        bridge, _ = _recovering(
            recovered=SimpleNamespace(completed=done), finished={"inv-1": _status("completed", "the report")},
        )
        store = _Store(_stored_task())
        await bridge.recover(store)
        return store

    assert asyncio.run(scenario()).log == [("finish", "completed", "the report")]


def test_recovery_uses_a_run_that_finished_before_adoption() -> None:
    async def scenario() -> _Store:
        bridge, _ = _recovering(finished={"inv-1": _status("completed", "early")})
        store = _Store(_stored_task())
        await bridge.recover(store)
        return store

    assert asyncio.run(scenario()).log == [("finish", "completed", "early")]


def test_recovery_fails_a_task_whose_run_is_unknown() -> None:
    async def scenario() -> _Store:
        bridge, admitted = _recovering()
        store = _Store(_stored_task())
        await bridge.recover(store)
        assert admitted == []
        return store

    assert asyncio.run(scenario()).log == [("finish", "failed", RUN_LOST)]


def test_recovery_resumes_a_failed_run_once_in_the_same_session() -> None:
    async def scenario() -> tuple[_Store, list]:
        bridge, admitted = _recovering(
            admit_result=_status("completed", "resumed reply"), finished={"inv-1": _status("failed", error="boom")},
        )
        store = _Store(_stored_task())
        await bridge.recover(store)
        return store, admitted

    store, admitted = asyncio.run(scenario())
    assert len(admitted) == 1
    assert (admitted[0].workspace_key, admitted[0].session_key) == ("w", "s")
    assert admitted[0].event.payload["text"] == RESUME_PROMPT
    assert store.log == [
        ("annotate", {"invocationId": "inv-2", "resumed": "1"}),
        ("finish", "completed", "resumed reply"),
    ]


def test_recovery_does_not_resume_twice() -> None:
    async def scenario() -> tuple[_Store, list]:
        bridge, admitted = _recovering(finished={"inv-1": _status("failed", error="boom")})
        store = _Store(_stored_task(resumed="1"))
        await bridge.recover(store)
        return store, admitted

    store, admitted = asyncio.run(scenario())
    assert admitted == []
    assert store.log == [("finish", "failed", "boom")]


def test_recovery_reports_a_failed_resume_without_a_second_one() -> None:
    async def scenario() -> tuple[_Store, list]:
        bridge, admitted = _recovering(
            admit_result=_status("failed", error="again"), finished={"inv-1": _status("failed", error="boom")},
        )
        store = _Store(_stored_task())
        await bridge.recover(store)
        return store, admitted

    store, admitted = asyncio.run(scenario())
    assert len(admitted) == 1
    assert store.log[-1] == ("finish", "failed", "again")


def test_execute_stamps_the_run_identity_on_the_first_working_event(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    config = _channel()

    async def runner(routed, ticket):
        return _completed(ticket)

    control = _control(config, runner)
    queue = _Queue()
    asyncio.run(A2AAgentExecutorBridge(control, config).execute(_Context("t1", "c1"), queue))
    stamped = next(e for e in queue.events if e.metadata)
    assert set(stamped.metadata) == {"invocationId", "workspaceKey", "sessionKey"}
    assert stamped.metadata["invocationId"]
    asyncio.run(control.admission.close())


# -- runtime status: queued, starting, heartbeat ------------------------------


def _kinds(queue, kind: str) -> list[str]:
    return [
        e.status.message.parts[0].text for e in queue.events
        if e.metadata and dict(e.metadata).get("ach") == kind
    ]


async def _status_run(monkeypatch, *, start: bool, busy: bool, between):
    """Run one task on a runner the test finishes itself; `between(queue, control)` acts mid-run."""
    from ach_runtime.channels import a2a

    monkeypatch.setattr(a2a, "STARTING_AFTER_SECONDS", 0.05)
    monkeypatch.setattr(a2a, "HEARTBEAT_SECONDS", 0.1)
    release = asyncio.Event()

    async def runner(_routed, ticket):
        if start:
            ticket.mark_started()
        await release.wait()
        return _completed(ticket)

    config = _channel()
    control = _control(config, runner)
    control.admission.session_busy = lambda _ticket: busy
    queue = _Queue()
    task = asyncio.create_task(A2AAgentExecutorBridge(control, config).execute(_Context("t1", "c1"), queue))
    await between(queue, control)
    release.set()
    await task
    await control.admission.close()
    return queue


async def _until(predicate, limit: float = 2.0) -> None:
    for _ in range(int(limit / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


def test_a_run_that_starts_at_once_gets_no_starting_message(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)

    async def between(queue, control):
        await asyncio.sleep(0.08)

    queue = asyncio.run(_status_run(monkeypatch, start=True, busy=False, between=between))
    assert not _kinds(queue, "starting") and not _kinds(queue, "queued")


def test_waiting_behind_the_same_session_says_queued(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    from ach_runtime.channels import a2a

    async def between(queue, control):
        await _until(lambda: _kinds(queue, "queued"))

    queue = asyncio.run(_status_run(monkeypatch, start=False, busy=True, between=between))
    assert _kinds(queue, "queued") == [a2a.QUEUED_TEXT]


def test_a_slow_start_without_a_busy_session_says_starting(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)
    from ach_runtime.channels import a2a

    async def between(queue, control):
        await _until(lambda: _kinds(queue, "starting"))

    queue = asyncio.run(_status_run(monkeypatch, start=False, busy=False, between=between))
    assert _kinds(queue, "starting") == [a2a.STARTING_TEXT]


def test_heartbeat_after_silence_and_progress_resets_it(monkeypatch) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", SECRET)

    async def between(queue, control):
        await _until(lambda: _kinds(queue, "heartbeat"))
        beats = len(_kinds(queue, "heartbeat"))
        (emit,) = next(iter(control.progress.values()))
        for _ in range(4):  # inform_user every 0.04 s, under the 0.1 s heartbeat
            await emit("Reading the bill")
            await asyncio.sleep(0.04)
        assert len(_kinds(queue, "heartbeat")) == beats

    queue = asyncio.run(_status_run(monkeypatch, start=True, busy=False, between=between))
    assert queue.events[-1].status.state == 3  # completed is the last event


def test_recovery_leaves_a_request_this_control_admitted_alone() -> None:
    async def scenario() -> _Store:
        bridge, admitted = _recovering(live={"inv-1"})
        store = _Store(_stored_task())
        await bridge.recover(store)
        assert admitted == []
        return store

    assert asyncio.run(scenario()).log == []

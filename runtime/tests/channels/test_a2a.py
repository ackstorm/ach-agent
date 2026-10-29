from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from ach_runtime.channels.a2a import (
    A2AAgentExecutorBridge,
    A2A_OUTPUT_INSTRUCTIONS,
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
    return SimpleNamespace(config=SimpleNamespace(channels=[config]), admission=_admission(runner), _stopping=False)


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
    app.mount("/a2a/review", build_a2a_app(make_a2a_agent_card("review"), bridge))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for path in ("agent-card.json", "agent.json"):
                response = await client.get(f"/a2a/review/.well-known/{path}")
                card = response.json()
                assert response.status_code == 200
                assert card["url"].endswith("/a2a/review")
                assert card["skills"] == []
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
    app.mount("/a2a/review", build_a2a_app(make_a2a_agent_card("review"), bridge))

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

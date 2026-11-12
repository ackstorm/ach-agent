from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount

from ach_runtime.channels.event import Event
from ach_runtime.channels.openai import (
    Continuation,
    FingerprintStore,
    RequestError,
    build_openai_app,
    chunk,
    completion,
    error_body,
    fingerprint_store,
    parse_request,
    prompt_text,
    run_outcome,
    sse,
    _stream,
)
from ach_runtime.channels.routing import route
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.execution.wire import RunStatus
from ach_runtime.harness.admission import Admission
from ach_runtime.harness.refs import session_ref, workspace_ref

PDF = base64.b64encode(b"%PDF-1.4 hello").decode()


def _user(content):
    return {"role": "user", "content": content}


def test_parse_takes_last_user_message_and_ignores_sampling_fields() -> None:
    request = parse_request({
        "model": "anything", "stream": True, "temperature": 0.2, "n": 3,
        "tools": [{"type": "function", "function": {"name": "f"}}],
        "messages": [{"role": "system", "content": "be nice"}, _user("first"),
                     {"role": "assistant", "content": "ok"}, _user([{"type": "text", "text": "second"}])],
    }, max_file_bytes=None)
    assert (request.text, request.model, request.stream, request.attachments) == ("second", "anything", True, [])
    assert len(request.messages) == 4


@pytest.mark.parametrize("body, status, message", [
    ({}, 400, "messages"),
    ({"messages": [{"role": "assistant", "content": "x"}]}, 400, "role user"),
    ({"messages": [_user("")]}, 400, "empty"),
    ({"messages": [_user([{"type": "input_audio", "input_audio": {}}])]}, 400, "unsupported"),
    ({"messages": [_user([{"type": "file", "file": {"file_data": PDF, "filename": "a.pdf"}}])]}, 400, "does not accept files"),
    ({"messages": [_user(5), _user("hi")]}, 400, "message content"),
])
def test_parse_rejects(body, status, message) -> None:
    with pytest.raises(RequestError, match=message) as caught:
        parse_request(body, max_file_bytes=None)
    assert caught.value.status == status


def test_parse_decodes_files_and_images() -> None:
    request = parse_request({"messages": [_user([
        {"type": "text", "text": "read these"},
        {"type": "file", "file": {"filename": "a.pdf", "file_data": f"data:application/pdf;base64,{PDF}"}},
        {"type": "file", "file": {"filename": "b.pdf", "file_data": PDF}},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PDF}"}},
        {"type": "file", "file": {"filename": "c.txt", "file_data": f"data:text/plain;charset=utf-8;base64,{PDF}"}},
    ])]}, max_file_bytes=1024)
    assert [(a.name, a.mime) for a in request.attachments] == [
        ("a.pdf", "application/pdf"), ("b.pdf", "application/pdf"), ("image-3.png", "image/png"),
        ("c.txt", "text/plain")]
    assert request.attachments[0].data == b"%PDF-1.4 hello"


@pytest.mark.parametrize("part, status, message", [
    ({"type": "file", "file": {"file_id": "file-1"}}, 400, "file_id"),
    ({"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}, 400, "data URI"),
    ({"type": "file", "file": {"filename": "a.pdf", "file_data": "!!!"}}, 400, "base64"),
    ({"type": "file", "file": {"filename": "a.pdf", "file_data": PDF}}, 413, "exceeds"),
    ({"type": "file", "file": {"filename": "a" * 256, "file_data": "aGk="}}, 400, "255"),
    ({"type": "file", "file": {"filename": "a", "file_data": "data:text/plain\u00e9;base64,aGk="}}, 400, "MIME"),
    ({"type": "file", "file": {"filename": "a", "file_data": f"data:text/{'x' * 128};base64,aGk="}}, 400, "MIME"),
    ({"type": "image_url", "image_url": {"url": "data:image;base64,aGk="}}, 400, "MIME"),
])
def test_parse_rejects_files(part, status, message) -> None:
    with pytest.raises(RequestError, match=message) as caught:
        parse_request({"messages": [_user([part])]}, max_file_bytes=4)
    assert caught.value.status == status


class Records:
    """The S3 store's record API, in memory."""

    def __init__(self) -> None:
        self.records: dict[str, tuple[bytes, datetime]] = {}
        self.fail = False

    async def put_record(self, name: str, data: bytes) -> None:
        if self.fail:
            raise OSError("s3 down")
        self.records[name] = (data, datetime.now(UTC))

    async def get_record(self, name: str) -> bytes | None:
        if self.fail:
            raise OSError("s3 down")
        found = self.records.get(name)
        return None if found is None else found[0]

    async def list_records(self, prefix: str) -> list[tuple[str, datetime]]:
        return [(name, at) for name, (_, at) in self.records.items() if name.startswith(prefix)]

    async def delete_record(self, name: str) -> None:
        self.records.pop(name, None)


ONE = Continuation(session_key="session-1", workspace_ref="w" * 64, instance_id="pod-1")
FIRST = [{"role": "system", "content": "s"}, _user("hi")]
RESENT = [{"role": "system", "content": "s"}, _user([{"type": "text", "text": "hi"}]),
          {"role": "assistant", "content": "hello there"}]


@pytest.mark.parametrize("storage", [None, Records()], ids=["memory", "s3"])
async def test_fingerprint_continues_once_and_tolerates_content_shape(storage) -> None:
    store = FingerprintStore(storage, agent="a", channel="chat", retention_days=30)
    await store.remember(FIRST, "hello there\n", ONE)
    assert await store.take(RESENT) == ONE
    assert await store.take(RESENT) is None  # a regenerate forks into a new session


async def test_s3_records_outlive_the_store_and_stay_per_agent() -> None:
    records = Records()
    await FingerprintStore(records, agent="a", channel="chat", retention_days=30).remember(FIRST, "hello there", ONE)
    assert all(name.startswith("openai/a/chat/") for name in records.records)
    assert await FingerprintStore(records, agent="b", channel="chat", retention_days=30).take(RESENT) is None
    assert await FingerprintStore(records, agent="a", channel="chat", retention_days=30).take(RESENT) == ONE


async def test_expired_records_miss_and_prune() -> None:
    from datetime import timedelta

    records = Records()
    late = datetime.now(UTC) + timedelta(days=31)
    await FingerprintStore(records, agent="a", channel="chat", retention_days=30).remember(FIRST, "hello there", ONE)
    assert await FingerprintStore(records, agent="a", channel="chat", retention_days=30, clock=lambda: late).take(RESENT) is None
    await FingerprintStore(records, agent="a", channel="chat", retention_days=30).remember(FIRST, "hello there", ONE)
    await FingerprintStore(records, agent="a", channel="chat", retention_days=30, clock=lambda: late).prune()
    assert records.records == {}


async def test_storage_errors_are_misses() -> None:
    records = Records()
    store = FingerprintStore(records, agent="a", channel="chat", retention_days=30)
    records.fail = True
    await store.remember(FIRST, "hello there", ONE)  # logged, not raised
    assert await store.take(RESENT) is None


async def test_memory_store_evicts_oldest() -> None:
    store = FingerprintStore(None, agent="a", channel="chat", retention_days=None, limit=1)
    await store.remember([_user("a")], "1", ONE)
    await store.remember([_user("b")], "2", ONE)
    assert await store.take([_user("a"), {"role": "assistant", "content": "1"}]) is None
    assert await store.take([_user("b"), {"role": "assistant", "content": "2"}]) == ONE


def test_prompt_text_replays_history_only_on_a_miss() -> None:
    request = parse_request({"messages": [
        {"role": "system", "content": "s"}, _user("one"), {"role": "assistant", "content": "two"}, _user("three"),
    ]}, max_file_bytes=None)
    assert prompt_text(request, continued=True) == "three"
    replay = prompt_text(request, continued=False)
    assert replay.startswith("The conversation so far:\n<conversation>\n[system]\ns\n\n[user]\none\n\n[assistant]\ntwo\n</conversation>")
    assert replay.endswith("\n\nthree")
    files_only = parse_request({"messages": [_user([{"type": "file", "file": {"filename": "a.pdf", "file_data": PDF}}])]},
                               max_file_bytes=1024)
    assert prompt_text(files_only, continued=False) == "The user sent the attached files."


def _status(state: str, result=None, error=None) -> RunStatus:
    return RunStatus(invocationId="i", channel="chat", workspaceRef="a" * 64, sessionRef="b" * 64,
                     runId="r", configVersion="c" * 64, deadlineAt=datetime.now(UTC), state=state,
                     error=error, result=result)


def test_run_outcome() -> None:
    assert run_outcome(_status("completed", {"text": "answer"})) == (True, "answer")
    assert run_outcome(_status("completed", {"text": "  "})) == (False, "the agent returned no text")
    assert run_outcome(_status("failed", error="boom")) == (False, "boom")
    assert run_outcome(object()) == (False, "Invocation failed")


def test_response_shapes() -> None:
    body = completion("chatcmpl-1", "m", "hi")
    assert body["object"] == "chat.completion"
    assert body["choices"] == [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}]
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    part = chunk("chatcmpl-1", "m", {"content": "x"})
    assert part["object"] == "chat.completion.chunk"
    assert part["choices"] == [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]
    assert sse("[DONE]") == "data: [DONE]\n\n"
    assert sse({"a": "é"}) == 'data: {"a": "é"}\n\n'
    assert error_body("bad", 400) == {"error": {"message": "bad", "type": "invalid_request_error", "code": None}}
    assert error_body("down", 503)["error"]["type"] == "server_error"


KEY = "test-openai-key"
AUTH = {"Authorization": f"Bearer {KEY}"}
WORKSPACE = workspace_ref("ach", "agent", "openai:chat")  # what Admission derives for this channel


def _admission(runner) -> Admission:
    return Admission(
        namespace="ach", agent_uid="agent", max_concurrent_invocations=10,
        max_queued_total=10, idempotency_window_seconds=60, max_invocation_seconds=60,
        workspace_max_concurrent_sessions=10, channel_concurrency=None,
        clock=lambda: datetime.now(UTC), runner=runner,
    )


def _done(ticket, text: str, state: str = "completed") -> RunStatus:
    return RunStatus(
        invocationId=ticket.invocation_id, channel="chat", workspaceRef=ticket.workspace_ref,
        sessionRef=ticket.session_ref, runId="r", configVersion="a" * 64,
        deadlineAt=ticket.deadline_at, state=state, error=None if state == "completed" else "boom",
        result={"text": text},
    )


def _client(runner, *, files: bool = True, persistence: str = "none", storage=None,
            monkeypatch) -> tuple[httpx.AsyncClient, SimpleNamespace]:
    monkeypatch.setenv("CHAT_KEY", KEY)
    channel = ChannelConfig.model_validate({"name": "chat", "type": "openai", "openai": {"auth": {"env": "CHAT_KEY"}}})
    endpoints = {"w": SimpleNamespace(ref=SimpleNamespace(workspace_ref=WORKSPACE), instance_id="pod-1")}
    control = SimpleNamespace(
        config=SimpleNamespace(
            agent=SimpleNamespace(name="test-agent"), channels=[channel],
            artifacts=SimpleNamespace(max_artifact_bytes=1024),
            capability=SimpleNamespace(filter=SimpleNamespace(exclude=SimpleNamespace(tools=[]))),
            workspace=SimpleNamespace(persistence=SimpleNamespace(type=persistence), session_retention_days=30),
        ),
        admission=None, progress={}, storage=storage if storage is not None else (object() if files else None),
        artifact_facade=object() if files else None,
        manager=SimpleNamespace(active_endpoints=lambda: dict(endpoints)), endpoints=endpoints,
        session_status=200, session_lookups=[],
    )

    async def mini_json(endpoint, method, path):
        control.session_lookups.append((endpoint.instance_id, method, path))
        return control.session_status, {}

    control._mini_json = mini_json
    control.admission = _admission(lambda routed, ticket: runner(control, routed, ticket))
    app = Starlette(routes=[Mount("/openai/chat", app=build_openai_app(
        control, channel, lambda: True, fingerprint_store(control, channel)))])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), control


async def test_models_requires_the_key_and_lists_the_agent(monkeypatch) -> None:
    async def runner(control, routed, ticket):
        raise AssertionError("no run")

    client, _ = _client(runner, monkeypatch=monkeypatch)
    async with client:
        assert (await client.get("/openai/chat/v1/models")).status_code == 401
        listed = (await client.get("/openai/chat/v1/models", headers=AUTH)).json()
    assert [m["id"] for m in listed["data"]] == ["test-agent"]


async def test_completion_continues_the_session_with_the_last_message(monkeypatch) -> None:
    seen = []

    async def runner(control, routed, ticket):
        ticket.mark_started()
        seen.append((routed.session_key, routed.event.payload["text"]))
        return _done(ticket, f"reply-{len(seen)}")

    client, _ = _client(runner, monkeypatch=monkeypatch)
    async with client:
        first = await client.post("/openai/chat/v1/chat/completions", headers=AUTH,
                                  json={"model": "x", "messages": [_user("one")]})
        second = await client.post("/openai/chat/v1/chat/completions", headers=AUTH, json={"messages": [
            _user("one"), {"role": "assistant", "content": "reply-1"}, _user("two")]})
    assert first.json()["choices"][0]["message"]["content"] == "reply-1"
    assert first.json()["model"] == "x"
    assert second.json()["model"] == "test-agent"
    assert seen[1] == (seen[0][0], "two")


async def test_unknown_history_replays_in_a_new_session(monkeypatch) -> None:
    seen = []

    async def runner(control, routed, ticket):
        ticket.mark_started()
        seen.append((routed.session_key, routed.event.payload["text"]))
        return _done(ticket, "ok")

    client, _ = _client(runner, monkeypatch=monkeypatch)
    async with client:
        await client.post("/openai/chat/v1/chat/completions", headers=AUTH, json={"messages": [_user("a")]})
        await client.post("/openai/chat/v1/chat/completions", headers=AUTH, json={"messages": [
            _user("a"), {"role": "assistant", "content": "edited"}, _user("b")]})
    assert seen[0][0] != seen[1][0]
    assert seen[1][1].startswith("The conversation so far:")


async def _first_turn(client) -> None:
    await client.post("/openai/chat/v1/chat/completions", headers=AUTH, json={"messages": [_user("one")]})


async def _second_turn(client) -> None:
    await client.post("/openai/chat/v1/chat/completions", headers=AUTH, json={"messages": [
        _user("one"), {"role": "assistant", "content": "reply-1"}, _user("two")]})


def _recording_runner(seen):
    async def runner(control, routed, ticket):
        ticket.mark_started()
        seen.append((routed.session_key, routed.event.payload["text"]))
        return _done(ticket, f"reply-{len(seen)}")
    return runner


async def test_persistence_none_replays_once_the_sandbox_instance_changed(monkeypatch) -> None:
    seen = []
    client, control = _client(_recording_runner(seen), monkeypatch=monkeypatch)
    async with client:
        await _first_turn(client)
        control.endpoints["w"] = SimpleNamespace(ref=SimpleNamespace(workspace_ref=WORKSPACE), instance_id="pod-2")
        await _second_turn(client)
    assert seen[0][0] != seen[1][0]
    assert seen[1][1].startswith("The conversation so far:")


async def test_persistence_none_replays_once_the_session_is_retired(monkeypatch) -> None:
    seen = []
    client, control = _client(_recording_runner(seen), monkeypatch=monkeypatch)
    async with client:
        await _first_turn(client)
        control.session_status = 404  # idle close or eviction deleted the session HOME
        await _second_turn(client)
    assert control.session_lookups == [("pod-1", "GET", f"/sessions/{session_ref(WORKSPACE, seen[0][0])}/info")]
    assert seen[0][0] != seen[1][0]
    assert seen[1][1].startswith("The conversation so far:")


async def test_durable_sessions_continue_after_a_control_restart(monkeypatch) -> None:
    seen = []
    records = Records()
    first, control = _client(_recording_runner(seen), persistence="s3", storage=records, monkeypatch=monkeypatch)
    async with first:
        await _first_turn(first)
    restarted, _ = _client(_recording_runner(seen), persistence="s3", storage=records, monkeypatch=monkeypatch)
    async with restarted:
        await _second_turn(restarted)
    assert seen[1] == (seen[0][0], "two")
    assert len(records.records) == 1  # the first entry was taken; the one left ends with reply-2


async def test_stream_sends_progress_answer_and_done(monkeypatch) -> None:
    async def runner(control, routed, ticket):
        ticket.mark_started()
        for _ in range(1000):  # bounded (~1 s): wait until the stream listens
            if control.progress.get(ticket.invocation_id):
                break
            await asyncio.sleep(0.001)
        for emit in list(control.progress.get(ticket.invocation_id, [])):
            await emit("reading the repo")
        return _done(ticket, "the answer")

    client, control = _client(runner, monkeypatch=monkeypatch)
    async with client:
        response = await client.post("/openai/chat/v1/chat/completions", headers=AUTH,
                                     json={"stream": True, "messages": [_user("go")]})
    events = [line[len("data: "):] for line in response.text.split("\n\n") if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    deltas = [json.loads(e)["choices"][0] for e in events[:-1]]
    assert deltas[0]["delta"] == {"role": "assistant", "content": ""}
    assert {"reasoning_content": "reading the repo\n"} in [d["delta"] for d in deltas]
    assert deltas[-2]["delta"] == {"content": "the answer"}
    assert deltas[-1] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    assert control.progress == {}


async def test_failed_run_is_an_openai_error(monkeypatch) -> None:
    async def runner(control, routed, ticket):
        ticket.mark_started()
        return _done(ticket, "", state="failed")

    client, _ = _client(runner, monkeypatch=monkeypatch)
    async with client:
        plain = await client.post("/openai/chat/v1/chat/completions", headers=AUTH, json={"messages": [_user("x")]})
        streamed = await client.post("/openai/chat/v1/chat/completions", headers=AUTH,
                                     json={"stream": True, "messages": [_user("y")]})
    assert plain.status_code == 500 and plain.json()["error"]["message"] == "boom"
    assert '"error"' in streamed.text and "[DONE]" not in streamed.text


async def test_files_need_artifacts_and_reach_the_payload(monkeypatch) -> None:
    payloads = []

    async def runner(control, routed, ticket):
        ticket.mark_started()
        payloads.append(routed.event.payload)
        return _done(ticket, "ok")

    message = {"messages": [_user([{"type": "file", "file": {"filename": "a.pdf", "file_data": PDF}}])]}
    without, _ = _client(runner, files=False, monkeypatch=monkeypatch)
    async with without:
        rejected = await without.post("/openai/chat/v1/chat/completions", headers=AUTH, json=message)
    with_files, _ = _client(runner, monkeypatch=monkeypatch)
    async with with_files:
        accepted = await with_files.post("/openai/chat/v1/chat/completions", headers=AUTH, json=message)
    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert payloads[0]["attachments"] == [{"name": "a.pdf", "mime": "application/pdf", "data": b"%PDF-1.4 hello"}]


async def test_stream_disconnect_leaves_the_run_running(monkeypatch) -> None:
    release = asyncio.Event()

    async def runner(control, routed, ticket):
        ticket.mark_started()
        await release.wait()
        return _done(ticket, "late")

    client, control = _client(runner, monkeypatch=monkeypatch)
    async with client:
        channel = control.config.channels[0]
        ticket = control.admission.admit(route(Event(
            event_id="openai:1", channel="chat", type="openai", source="openai", payload={"text": "go"},
            context={"openai": {"conversation": "c1", "stream": True}}), channel))

        async def answered(text: str) -> None:
            raise AssertionError("nobody is listening")

        stream = _stream(control, ticket, "chatcmpl-1", "m", answered)
        await anext(stream)  # the role chunk
        reading = asyncio.ensure_future(anext(stream))
        await asyncio.sleep(0.01)
        reading.cancel()  # what Starlette does when the client disconnects
        with pytest.raises(asyncio.CancelledError):
            await reading
        release.set()
        status = await asyncio.wait_for(ticket.completed, 1)
    assert not ticket.completed.cancelled() and status.state == "completed"
    assert control.progress == {}

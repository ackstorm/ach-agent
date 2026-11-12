from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from ach_runtime.channels.openai import (
    Continuation,
    FingerprintStore,
    RequestError,
    chunk,
    completion,
    error_body,
    parse_request,
    prompt_text,
    run_outcome,
    sse,
)
from ach_runtime.execution.wire import RunStatus

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
    ])]}, max_file_bytes=1024)
    assert [(a.name, a.mime) for a in request.attachments] == [
        ("a.pdf", "application/pdf"), ("b.pdf", "application/pdf"), ("image-3.png", "image/png")]
    assert base64.b64decode(request.attachments[0].data) == b"%PDF-1.4 hello"


@pytest.mark.parametrize("part, status, message", [
    ({"type": "file", "file": {"file_id": "file-1"}}, 400, "file_id"),
    ({"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}, 400, "data URI"),
    ({"type": "file", "file": {"filename": "a.pdf", "file_data": "!!!"}}, 400, "base64"),
    ({"type": "file", "file": {"filename": "a.pdf", "file_data": PDF}}, 413, "exceeds"),
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

# SPDX-License-Identifier: Apache-2.0
"""OpenAI Chat Completions channel: the Agent answers `POST /v1/chat/completions` as a model.

The client sends the whole conversation on every request and no conversation id. A request
continues a session when the messages before its last user message hash to a reply this
channel gave; otherwise it starts a new session with the visible conversation in the prompt.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import mimetypes
import re
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import structlog

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from ach_runtime.channels.auth import verify_header_token
from ach_runtime.channels.event import Event
from ach_runtime.channels.metrics import CHANNEL_INBOUND
from ach_runtime.channels.routing import RoutingError, route
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.execution.wire import RunStatus
from ach_runtime.harness.admission import (
    AdmissionClosedError,
    AdmissionQueueFullError,
    AdmissionRateLimitedError,
    AdmissionTicket,
)
from ach_runtime.harness.refs import session_ref
from ach_runtime.harness.workspaces import WorkspaceAssemblyUnavailable

log = structlog.get_logger(__name__)

MAX_BODY_BYTES = 32 * 1024 * 1024  # ponytail: fixed cap; each file is also bound by artifacts.maxArtifactBytes
KEEPALIVE_SECONDS = 15.0  # under the 60 s idle timeout of common load balancers
FINGERPRINTS_MAX = 10_000  # in-memory store only

OPENAI_PROGRESS_INSTRUCTIONS = (
    "<progress_updates>\n"
    "The caller waits while you work. Before slow work (a long analysis, a heavy tool run, a "
    "multi-step change), call the `inform_user` tool with one short sentence on what you are "
    "doing. Use it for milestones only, not for every step. Your final reply is what the "
    "caller reads.\n"
    "</progress_updates>"
)


class RequestError(ValueError):
    """The request cannot be served; `status` is the HTTP status of the OpenAI error body."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Attachment:
    name: str
    mime: str
    data: bytes


@dataclass(frozen=True, slots=True)
class ChatRequest:
    messages: list[dict[str, Any]]
    text: str
    attachments: list[Attachment]
    model: str
    stream: bool


def _parts(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list) and all(isinstance(part, dict) for part in content):
        return content
    raise RequestError("message content must be a string or a list of parts")


def _text(content: Any) -> str:
    return "\n".join(
        str(part.get("text", "")) for part in _parts(content) if part.get("type") == "text"
    ).strip()


def _decode(value: str, default_mime: str) -> tuple[str, bytes]:
    """`data:<mime>;base64,<data>` or bare base64 -> (mime, bytes)."""
    mime, data = default_mime, value
    if value.startswith("data:"):
        header, separator, data = value.partition(",")
        if not separator or not header.endswith(";base64"):
            raise RequestError("file data must be a base64 data URI")
        mime = header[len("data:"):-len(";base64")].partition(";")[0] or default_mime
    try:
        return mime, base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RequestError("file data is not valid base64") from exc


def _attachment(part: dict[str, Any], index: int, max_bytes: int) -> Attachment:
    if part.get("type") == "image_url":
        url = (part.get("image_url") or {}).get("url")
        if not isinstance(url, str) or not url.startswith("data:"):
            raise RequestError("image_url must be a base64 data URI")
        mime, raw = _decode(url, "application/octet-stream")
        name = f"image-{index}{mimetypes.guess_extension(mime) or ''}"
    else:
        file = part.get("file") or {}
        if "file_id" in file:
            raise RequestError("file_id is not supported: send file_data")
        data = file.get("file_data")
        if not isinstance(data, str):
            raise RequestError("a file part requires file_data")
        name = str(file.get("filename") or f"file-{index}")
        if len(name) > 255:
            raise RequestError("a filename exceeds 255 characters")
        mime, raw = _decode(data, mimetypes.guess_type(name)[0] or "application/octet-stream")
    # Name and MIME go into the artifact's S3 user metadata, capped at 2 KB.
    if len(mime) > 127 or not re.fullmatch(r"[\w.+-]+/[\w.+-]+", mime, re.ASCII):
        raise RequestError(f"{mime[:127]!r} is not a MIME type")
    if len(raw) > max_bytes:
        raise RequestError(f"{name} exceeds {max_bytes} bytes", status=413)
    return Attachment(name=name, mime=mime, data=raw)


def parse_request(body: Any, *, max_file_bytes: int | None) -> ChatRequest:
    """Validate a chat completion request. `max_file_bytes` None: files are not accepted."""
    if not isinstance(body, dict):
        raise RequestError("the request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise RequestError("messages must be a non-empty list of objects")
    for message in messages:
        _parts(message.get("content"))
    last = messages[-1]
    if last.get("role") != "user":
        raise RequestError("the last message must have role user")
    attachments: list[Attachment] = []
    for part in _parts(last.get("content")):
        kind = part.get("type")
        if kind == "text":
            continue
        if kind not in ("file", "image_url"):
            raise RequestError(f"unsupported content part {kind!r}")
        if max_file_bytes is None:
            raise RequestError("this agent does not accept files")
        attachments.append(_attachment(part, len(attachments) + 1, max_file_bytes))
    text = _text(last.get("content"))
    if not text and not attachments:
        raise RequestError("the last user message is empty")
    model = body.get("model")
    return ChatRequest(
        messages=messages, text=text, attachments=attachments,
        model=model if isinstance(model, str) else "", stream=body.get("stream") is True,
    )


def _canonical(message: dict[str, Any]) -> list[str]:
    parts = [
        str(part.get("text", "")).strip() if part.get("type") == "text"
        else hashlib.sha256(json.dumps(part, sort_keys=True).encode()).hexdigest()
        for part in _parts(message.get("content"))
    ]
    return [str(message.get("role", "")), *parts]


def fingerprint(messages: list[dict[str, Any]]) -> str:
    canonical = json.dumps([_canonical(m) for m in messages], ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Continuation:
    """The session that gave a reply, and the sandbox instance that held it."""

    session_key: str
    workspace_ref: str
    instance_id: str


class FingerprintStore:
    """Conversation fingerprint -> the Continuation of the reply that ends it. Each entry
    continues once, so a regenerated or edited reply forks into a new session.

    With `storage`, one record per entry at `openai/<agent>/<channel>/<fingerprint>.json`
    outlives a control restart; the prefix is shared by every Agent in the namespace, so the
    agent name keeps them apart. Without it, an in-memory LRU. Every failure is a miss: the
    caller then replays the visible conversation, which is always correct."""

    def __init__(
        self, storage: Any | None, *, agent: str, channel: str, retention_days: int | None,
        limit: int = FINGERPRINTS_MAX, clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._storage = storage
        self._dir = f"openai/{quote(agent, safe='')}/{quote(channel, safe='')}/"
        self._retention = None if retention_days is None else timedelta(days=retention_days)
        self._memory: OrderedDict[str, Continuation] = OrderedDict()
        self._limit = limit
        self._clock = clock

    def _name(self, key: str) -> str:
        return f"{self._dir}{key}.json"

    async def take(self, messages: list[dict[str, Any]]) -> Continuation | None:
        key = fingerprint(messages)
        if self._storage is None:
            return self._memory.pop(key, None)
        try:
            raw = await self._storage.get_record(self._name(key))
            if raw is None:
                return None
            await self._storage.delete_record(self._name(key))
            doc = json.loads(raw)
            if self._retention is not None and datetime.fromisoformat(doc["at"]) < self._clock() - self._retention:
                return None
            return Continuation(doc["session"], doc["workspace"], doc["instance"])
        except Exception as exc:
            log.warning("openai fingerprint lookup failed", error=str(exc))
            return None

    async def remember(self, messages: list[dict[str, Any]], reply: str, continuation: Continuation) -> None:
        key = fingerprint([*messages, {"role": "assistant", "content": reply}])
        if self._storage is None:
            self._memory[key] = continuation
            while len(self._memory) > self._limit:
                self._memory.popitem(last=False)
            return
        doc = {"session": continuation.session_key, "workspace": continuation.workspace_ref,
               "instance": continuation.instance_id, "at": self._clock().isoformat()}
        try:
            await self._storage.put_record(self._name(key), json.dumps(doc).encode())
        except Exception as exc:
            log.warning("openai fingerprint save failed", error=str(exc))

    async def prune(self) -> None:
        """Delete the records older than the session retention."""
        if self._storage is None or self._retention is None:
            return
        try:
            for name, modified in await self._storage.list_records(self._dir):
                if modified < self._clock() - self._retention:
                    await self._storage.delete_record(name)
        except Exception as exc:
            log.warning("openai fingerprint prune failed", error=str(exc))


def prompt_text(request: ChatRequest, *, continued: bool) -> str:
    """The last user message; on a new session, preceded by the visible conversation."""
    text = request.text or "The user sent the attached files."
    history = request.messages[:-1]
    if continued or not history:
        return text
    turns = []
    for message in history:
        body = _text(message.get("content"))
        files = sum(1 for part in _parts(message.get("content")) if part.get("type") != "text")
        if files:
            body = f"{body}\n[{files} file(s) of this earlier message are not available]".strip()
        turns.append(f"[{message.get('role', '')}]\n{body}")
    conversation = "\n\n".join(turns)
    return f"The conversation so far:\n<conversation>\n{conversation}\n</conversation>\n\n{text}"


def run_outcome(result: Any) -> tuple[bool, str]:
    """(answered, the answer or the error message) of a finished run."""
    if not isinstance(result, RunStatus):
        return False, "Invocation failed"
    if result.state != "completed":
        return False, result.error or f"the run ended {result.state}"
    text = result.result.get("text") if isinstance(result.result, dict) else None
    if not isinstance(text, str) or not text.strip():
        return False, "the agent returned no text"
    return True, text


def completion(request_id: str, model: str, text: str) -> dict[str, Any]:
    return {
        "id": request_id, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def chunk(request_id: str, model: str, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    return {
        "id": request_id, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def sse(data: dict[str, Any] | str) -> str:
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n"


def error_body(message: str, status: int) -> dict[str, Any]:
    kind = "invalid_request_error" if status < 500 else "server_error"
    return {"error": {"message": message, "type": kind, "code": None}}


def _fail(message: str, status: int) -> JSONResponse:
    return JSONResponse(error_body(message, status), status_code=status)


def _retrieve(future: asyncio.Future[Any]) -> None:
    """Admission owns the outcome; mark a failure retrieved so asyncio does not log it."""
    if not future.cancelled():
        future.exception()


def _settled(future: asyncio.Future[Any]) -> tuple[bool, str]:
    if future.cancelled() or future.exception() is not None:
        return False, "Invocation failed"
    return run_outcome(future.result())


def _files_limit(control: Any) -> int | None:
    """Bytes per file, or None when the agent cannot read files."""
    if control.artifact_facade is None or control.storage is None:
        return None
    if "read_artifact" in control.config.capability.filter.exclude.tools:
        return None
    return control.config.artifacts.max_artifact_bytes


def fingerprint_store(control: Any, channel: ChannelConfig) -> FingerprintStore:
    """In the Agent's S3 store when session HOMEs outlive the sandbox and the store exists;
    in memory otherwise."""
    workspace = control.config.workspace
    durable = workspace.persistence.type != "none" and control.storage is not None
    return FingerprintStore(
        control.storage if durable else None, agent=control.config.agent.name,
        channel=channel.name, retention_days=workspace.session_retention_days,
    )


def _endpoint(control: Any, workspace_ref: str) -> Any | None:
    """The active sandbox endpoint of a Workspace, None when none is active."""
    return next((endpoint for endpoint in control.manager.active_endpoints().values()
                 if endpoint.ref.workspace_ref == workspace_ref), None)


async def _continues(control: Any, found: Continuation | None) -> bool:
    """With persistence none a session's history lives in its HOME on the sandbox instance that
    ran it, until the session is retired (idle close, eviction); continue only while it is there."""
    if found is None:
        return False
    if control.config.workspace.persistence.type != "none":
        return True
    endpoint = _endpoint(control, found.workspace_ref)
    if endpoint is None or not found.instance_id or endpoint.instance_id != found.instance_id:
        return False
    try:
        status, _ = await control._mini_json(
            endpoint, "GET", f"/sessions/{session_ref(found.workspace_ref, found.session_key)}/info")
    except Exception as exc:
        log.warning("openai session lookup failed", error=str(exc))
        return False
    return status == 200


async def _stream(
    control: Any, ticket: AdmissionTicket, request_id: str, model: str,
    answered: Callable[[str], Awaitable[None]],
) -> AsyncIterator[str]:
    progress: asyncio.Queue[str] = asyncio.Queue()

    async def emit(message: str) -> None:
        progress.put_nowait(message)

    control.progress.setdefault(ticket.invocation_id, []).append(emit)
    try:
        yield sse(chunk(request_id, model, {"role": "assistant", "content": ""}))
        while not ticket.completed.done():
            getter = asyncio.ensure_future(progress.get())
            try:
                done, _ = await asyncio.wait(
                    {getter, ticket.completed}, timeout=KEEPALIVE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                if not getter.done():
                    getter.cancel()
            if getter.done() and not getter.cancelled():
                yield sse(chunk(request_id, model, {"reasoning_content": getter.result() + "\n"}))
            elif not done:
                yield ": keepalive\n\n"
        ok, text = _settled(ticket.completed)
        if not ok:
            yield sse(error_body(text, 500))
            return
        await answered(text)
        yield sse(chunk(request_id, model, {"content": text}))
        yield sse(chunk(request_id, model, {}, "stop"))
        yield sse("[DONE]")
    finally:
        emitters = control.progress.get(ticket.invocation_id, [])
        if emit in emitters:
            emitters.remove(emit)
        if not emitters:
            control.progress.pop(ticket.invocation_id, None)


def build_openai_app(
    control: Any, channel: ChannelConfig, ready: Callable[[], bool], store: FingerprintStore,
) -> Starlette:
    """`GET /v1/models` and `POST /v1/chat/completions` for one `openai` channel."""
    assert channel.openai is not None
    env = channel.openai.auth.env
    agent_name = control.config.agent.name

    def authorized(request: Request) -> bool:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        return scheme.lower() == "bearer" and verify_header_token(token.strip(), env)

    async def models(request: Request) -> Response:
        if not authorized(request):
            return _fail("invalid API key", 401)
        return JSONResponse({"object": "list", "data": [
            {"id": agent_name, "object": "model", "created": 0, "owned_by": "ach-agent"}]})

    async def chat(request: Request) -> Response:
        if not authorized(request):
            return _fail("invalid API key", 401)
        if not ready():
            return _fail("the agent is not ready", 503)
        body = bytearray()
        async for part in request.stream():
            body.extend(part)
            if len(body) > MAX_BODY_BYTES:
                return _fail(f"the request body exceeds {MAX_BODY_BYTES} bytes", 413)
        try:
            parsed = parse_request(json.loads(body), max_file_bytes=_files_limit(control))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _fail("the request body is not JSON", 400)
        except RequestError as exc:
            return _fail(str(exc), exc.status)
        found = await store.take(parsed.messages[:-1])
        continued = await _continues(control, found)
        session_key = found.session_key if continued and found is not None else uuid.uuid4().hex
        payload: dict[str, Any] = {"text": prompt_text(parsed, continued=continued)}
        if parsed.attachments:
            payload["attachments"] = [
                {"name": a.name, "mime": a.mime, "data": a.data} for a in parsed.attachments]
        event = Event(
            event_id=f"openai:{uuid.uuid4().hex}", channel=channel.name, type="openai", source="openai",
            payload=payload, context={"openai": {"conversation": session_key, "stream": parsed.stream}},
        )
        try:
            routed = route(event, channel)
            CHANNEL_INBOUND.labels(channel=channel.name, type="openai").inc()
            ticket = control.admission.admit(routed)
        except RoutingError as exc:
            return _fail(str(exc), 400)
        except AdmissionRateLimitedError as exc:
            return _fail(str(exc), 429)
        except (AdmissionQueueFullError, AdmissionClosedError, WorkspaceAssemblyUnavailable):
            return _fail("the request could not be admitted", 503)
        if not isinstance(ticket, AdmissionTicket):
            return _fail("the request could not be admitted", 503)
        ticket.started.add_done_callback(_retrieve)
        ticket.completed.add_done_callback(_retrieve)
        request_id = f"chatcmpl-{uuid.uuid4().hex}"
        model = parsed.model or agent_name

        async def answered(text: str) -> None:
            endpoint = _endpoint(control, ticket.workspace_ref)
            await store.remember(parsed.messages, text, Continuation(
                session_key, ticket.workspace_ref, endpoint.instance_id if endpoint is not None else ""))

        if parsed.stream:
            return StreamingResponse(
                _stream(control, ticket, request_id, model, answered),
                media_type="text/event-stream", headers={"Cache-Control": "no-cache"},
            )
        # asyncio.wait never cancels the run's future when this request is dropped.
        await asyncio.wait({ticket.completed})
        ok, text = _settled(ticket.completed)
        if not ok:
            return _fail(text, 500)
        await answered(text)
        return JSONResponse(completion(request_id, model, text))

    return Starlette(routes=[
        Route("/v1/models", models, methods=["GET"]),
        Route("/v1/chat/completions", chat, methods=["POST"]),
    ])

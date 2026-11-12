# SPDX-License-Identifier: Apache-2.0
"""OpenAI Chat Completions channel: the Agent answers `POST /v1/chat/completions` as a model.

The client sends the whole conversation on every request and no conversation id. A request
continues a session when the messages before its last user message hash to a reply this
channel gave; otherwise it starts a new session with the visible conversation in the prompt.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import structlog

from ach_runtime.execution.wire import RunStatus

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
    data: str  # base64


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
        mime = header[len("data:"):-len(";base64")] or default_mime
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
        mime, raw = _decode(data, mimetypes.guess_type(name)[0] or "application/octet-stream")
    if len(raw) > max_bytes:
        raise RequestError(f"{name} exceeds {max_bytes} bytes", status=413)
    return Attachment(name=name, mime=mime, data=base64.b64encode(raw).decode("ascii"))


def parse_request(body: Any, *, max_file_bytes: int | None) -> ChatRequest:
    """Validate a chat completion request. `max_file_bytes` None: files are not accepted."""
    if not isinstance(body, dict):
        raise RequestError("the request body must be a JSON object")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages or not all(isinstance(m, dict) for m in messages):
        raise RequestError("messages must be a non-empty list of objects")
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

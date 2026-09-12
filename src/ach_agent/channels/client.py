"""Signed HTTP client implementing the existing channel handler seam."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from ach_agent.channels.envelopes import (
    Admission,
    Completion,
    EventEnvelope,
    EventRef,
    Submission,
)
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.signing import (
    NONCE_HEADER,
    REQUEST_HEADER,
    RESPONSE_HEADER,
    TIMESTAMP_HEADER,
    request_mac,
    verify_response_mac,
)
from ach_agent.router.router import RouterAdmitResult

MAX_WIRE_BODY_BYTES = 1 * 1024 * 1024


class SubmissionFailed(RuntimeError):
    """The harness submission could not be authenticated or completed."""


class ChannelsClient:
    """Remote channel adapter with no retained per-event state."""

    def __init__(
        self,
        base_url: str,
        key: bytes,
        *,
        agent: str = "default",
        channel_name: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        poll_interval: float = 0.25,
        wait_timeout: float | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.key = key
        self.agent = agent
        self.channel_name = channel_name
        self._clock = clock
        self._nonce_factory = nonce_factory
        self._poll_interval = poll_interval
        self._wait_timeout = wait_timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    @property
    def completion_port(self) -> ChannelsClient:
        return self

    async def close(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def ref_for(self, event: MessageEvent) -> EventRef:
        return EventRef(
            agent=self.agent, channel_name=event.channel_name, idempotency_key=event.idempotency_key
        )

    def sinks(self, ref: EventRef) -> tuple[None, None]:
        return None, None

    def register_sinks(
        self,
        ref: EventRef,
        *,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[object], None] | None = None,
    ) -> None:
        # Progress callbacks intentionally remain local to the harness process.
        return None

    def discard_sinks(self, ref: EventRef) -> None:
        return None

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        """Adapt the remote admission response to the existing channel contract."""
        envelope = EventEnvelope.from_message_event(event)
        submission = await self._request_submission(envelope)
        return {
            Admission.ACCEPTED: RouterAdmitResult.ACCEPTED,
            Admission.DUPLICATE: RouterAdmitResult.DUPLICATE,
            Admission.FULL_QUEUE: RouterAdmitResult.FULL_QUEUE,
        }[submission.admission]

    async def submit(self, envelope: EventEnvelope) -> Submission:
        submission = await self._request_submission(envelope)
        if submission.completion is None and submission.admission is not Admission.FULL_QUEUE:
            raise SubmissionFailed("submission response did not include completion")
        return submission

    async def wait(self, ref: EventRef) -> Completion:
        if ref.agent != self.agent:
            raise SubmissionFailed("result scope mismatch: agent")
        deadline = self._clock() + self._wait_timeout if self._wait_timeout is not None else None
        while True:
            body = self._json_bytes(
                {"agent": self.agent, "ref": ref.model_dump(mode="json"), "wait": False}
            )
            response_body, status = await self._post("/internal/v1/results", body)
            payload = self._parse_object(response_body)
            if payload.get("kind") != "completion":
                raise SubmissionFailed(str(payload.get("error") or "malformed result response"))
            completion = self._parse_completion(payload.get("completion"))
            if completion.ref != ref:
                raise SubmissionFailed("result correlation mismatch")
            if status >= 400:
                raise SubmissionFailed(str(completion.error or "result lookup rejected"))
            if completion.state in {"completed", "failed", "outcome_unavailable"}:
                return completion
            if deadline is not None and self._clock() >= deadline:
                raise SubmissionFailed("result wait timed out")
            await asyncio.sleep(self._poll_interval)

    async def _request_submission(
        self, envelope: EventEnvelope
    ) -> Submission:
        if self.channel_name is not None and envelope.channel_name != self.channel_name:
            raise SubmissionFailed("submission scope mismatch: channel")
        ref = envelope.event_ref(self.agent)
        body = self._json_bytes({"agent": self.agent, "event": envelope.model_dump(mode="json")})
        response_body, status = await self._post("/internal/v1/events", body)
        payload = self._parse_object(response_body)
        if payload.get("kind") != "submission":
            raise SubmissionFailed(str(payload.get("error") or "malformed submission response"))
        admission_value = payload.get("admission")
        if not isinstance(admission_value, str):
            raise SubmissionFailed("unknown submission admission")
        try:
            admission = Admission(admission_value)
        except ValueError:
            if status >= 400 and payload.get("error"):
                raise SubmissionFailed(str(payload["error"])) from None
            raise SubmissionFailed("unknown submission admission") from None
        completion_value = payload.get("completion")
        completion = (
            self._parse_completion(completion_value) if completion_value is not None else None
        )
        if completion is not None and completion.ref != ref:
            raise SubmissionFailed("submission correlation mismatch")
        if status >= 400 and admission is not Admission.FULL_QUEUE:
            raise SubmissionFailed(str(payload.get("error") or "submission rejected"))
        return Submission(admission=admission, completion=completion)

    async def _post(self, target: str, body: bytes) -> tuple[bytes, int]:
        if len(body) > MAX_WIRE_BODY_BYTES:
            raise SubmissionFailed("request body too large")
        timestamp = int(self._clock())
        nonce = self._nonce_factory()
        headers = {
            "content-type": "application/json",
            TIMESTAMP_HEADER: str(timestamp),
            NONCE_HEADER: nonce,
            REQUEST_HEADER: request_mac(self.key, "POST", target, timestamp, nonce, body),
        }
        try:
            async with self._http.stream("POST", target, content=body, headers=headers) as response:
                chunks: list[bytes] = []
                response_size = 0
                async for chunk in response.aiter_bytes():
                    response_size += len(chunk)
                    if response_size > MAX_WIRE_BODY_BYTES:
                        raise SubmissionFailed("response body too large")
                    chunks.append(chunk)
                response_body = b"".join(chunks)
                status = response.status_code
                signature = response.headers.get(RESPONSE_HEADER, "")
        except (httpx.HTTPError, OSError) as exc:
            raise SubmissionFailed(f"channel HTTP request failed: {exc}") from exc
        if not signature or not verify_response_mac(
            self.key, nonce, status, response_body, signature
        ):
            raise SubmissionFailed("invalid channel response authentication")
        return response_body, status

    @staticmethod
    def _json_bytes(value: object) -> bytes:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    @staticmethod
    def _parse_object(body: bytes) -> dict[str, Any]:
        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SubmissionFailed("malformed channel response JSON") from exc
        if not isinstance(value, dict):
            raise SubmissionFailed("channel response must be an object")
        return value

    @staticmethod
    def _parse_completion(value: object) -> Completion:
        if not isinstance(value, dict):
            raise SubmissionFailed("submission response missing completion")
        try:
            return Completion.model_validate(value)
        except Exception as exc:  # pydantic validation is part of transport decoding
            raise SubmissionFailed("malformed completion response") from exc

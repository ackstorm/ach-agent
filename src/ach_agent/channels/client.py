# SPDX-License-Identifier: Apache-2.0
"""HTTP-over-Unix-socket client implementing the existing channel handler seam."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

from ach_agent.channels.envelopes import (
    Admission,
    ChannelInputs,
    Completion,
    EventEnvelope,
    EventRef,
    Submission,
)
from ach_agent.channels.message_event import MessageEvent
from ach_agent.boot.ipc import channel_socket_path
from ach_agent.router.router import RouterAdmitResult

MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024
# A retained completion may itself be exactly 1 MiB. The HTTP result envelope
# needs a fixed finite margin for its correlation and admission fields.
MAX_RESPONSE_BODY_BYTES = MAX_REQUEST_BODY_BYTES + 64 * 1024


class SubmissionFailed(RuntimeError):
    """The harness submission could not be transported or completed."""


class ChannelsClient:
    """Remote channel adapter with no retained per-event state."""

    def __init__(
        self,
        socket_path: str | None = None,
        *,
        agent: str = "default",
        channel_name: str | None = None,
        timeout: float = 30.0,
        http_client: httpx.AsyncClient | None = None,
        poll_interval: float = 0.25,
        wait_timeout: float | None = None,
    ) -> None:
        self.base_url = "http://ach-internal"
        self._socket_path = socket_path or str(channel_socket_path())
        self.agent = agent
        self.channel_name = channel_name
        self._poll_interval = poll_interval
        self._wait_timeout = wait_timeout
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=httpx.AsyncHTTPTransport(uds=self._socket_path),
        )
        self._closed = False
        self._operations: set[asyncio.Task[Any]] = set()

    @property
    def completion_port(self) -> ChannelsClient:
        return self

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        pending = [task for task in self._operations if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_client:
            await self._http.aclose()

    def _begin_operation(self) -> asyncio.Task[Any]:
        if self._closed:
            raise SubmissionFailed("channel client is closed")
        task = asyncio.current_task()
        if task is None:
            raise SubmissionFailed("channel operation requires an asyncio task")
        self._operations.add(task)
        return task

    def _end_operation(self, task: asyncio.Task[Any]) -> None:
        self._operations.discard(task)

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
        operation = self._begin_operation()
        try:
            envelope = EventEnvelope.from_message_event(event)
            submission = await self._request_submission(envelope)
            return {
                Admission.ACCEPTED: RouterAdmitResult.ACCEPTED,
                Admission.DUPLICATE: RouterAdmitResult.DUPLICATE,
                Admission.FULL_QUEUE: RouterAdmitResult.FULL_QUEUE,
            }[submission.admission]
        finally:
            self._end_operation(operation)

    async def submit(self, envelope: EventEnvelope) -> Submission:
        operation = self._begin_operation()
        try:
            return await self._request_submission(envelope)
        finally:
            self._end_operation(operation)

    async def wait(self, ref: EventRef) -> Completion:
        operation = self._begin_operation()
        try:
            return await self._wait(ref)
        finally:
            self._end_operation(operation)

    async def probe_harness(self, channel_name: str = "") -> bool:
        """Verify connectivity to H without submitting work."""
        del channel_name
        body = self._json_bytes({"agent": self.agent})
        response_body, status = await self._post("/internal/v1/readyz", body)
        payload = self._parse_object(response_body)
        return status == 200 and payload.get("kind") == "ready" and payload.get("status") is True

    async def fetch_config(self) -> ChannelInputs:
        """Fetch the typed source-only projection before starting adapters."""
        operation = self._begin_operation()
        try:
            try:
                async with self._http.stream("GET", "/internal/v1/config") as response:
                    chunks: list[bytes] = []
                    response_size = 0
                    async for chunk in response.aiter_bytes():
                        response_size += len(chunk)
                        if response_size > MAX_RESPONSE_BODY_BYTES:
                            raise SubmissionFailed("response body too large")
                        chunks.append(chunk)
                    response_body = b"".join(chunks)
                    status = response.status_code
            except (httpx.HTTPError, OSError) as exc:
                raise SubmissionFailed(f"channel configuration request failed: {exc}") from exc
            if status != 200:
                raise SubmissionFailed("channel configuration fetch failed")
            try:
                return ChannelInputs.model_validate_json(response_body)
            except Exception as exc:
                raise SubmissionFailed("malformed channel configuration") from exc
        finally:
            self._end_operation(operation)

    async def _wait(self, ref: EventRef) -> Completion:
        if ref.agent != self.agent:
            raise SubmissionFailed("result scope mismatch: agent")
        deadline = time.monotonic() + self._wait_timeout if self._wait_timeout is not None else None
        invocation_id: str | None = None
        while True:
            body = self._json_bytes(
                {"agent": self.agent, "ref": ref.model_dump(mode="json"), "wait": False}
            )
            response_body, status = await self._post("/internal/v1/results", body)
            payload = self._parse_object(response_body)
            if payload.get("kind") != "completion":
                raise SubmissionFailed(str(payload.get("error") or "malformed result response"))
            completion = self._parse_completion(payload.get("completion"))
            self._validate_completion(completion, ref)
            if status != 200:
                raise SubmissionFailed(str(completion.error or "result lookup rejected"))
            if completion.invocation_id:
                if invocation_id is None:
                    invocation_id = completion.invocation_id
                elif completion.invocation_id != invocation_id:
                    raise SubmissionFailed("result invocation correlation mismatch")
            if completion.state in {"completed", "failed", "outcome_unavailable"}:
                return completion
            if deadline is not None and time.monotonic() >= deadline:
                raise SubmissionFailed("result wait timed out")
            await asyncio.sleep(self._poll_interval)

    async def _request_submission(self, envelope: EventEnvelope) -> Submission:
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
        expected_status = {
            Admission.ACCEPTED: 202,
            Admission.DUPLICATE: 200,
            Admission.FULL_QUEUE: 503,
        }[admission]
        if status != expected_status:
            raise SubmissionFailed("submission admission/status mismatch")
        if admission is Admission.FULL_QUEUE:
            if completion is not None:
                raise SubmissionFailed("full queue response included a completion")
        elif completion is None:
            raise SubmissionFailed("submission response did not include completion")
        else:
            self._validate_completion(completion, ref)
        return Submission(admission=admission, completion=completion)

    async def _post(self, target: str, body: bytes) -> tuple[bytes, int]:
        if len(body) > MAX_REQUEST_BODY_BYTES:
            raise SubmissionFailed("request body too large")
        headers = {"content-type": "application/json"}
        try:
            async with self._http.stream("POST", target, content=body, headers=headers) as response:
                chunks: list[bytes] = []
                response_size = 0
                async for chunk in response.aiter_bytes():
                    response_size += len(chunk)
                    if response_size > MAX_RESPONSE_BODY_BYTES:
                        raise SubmissionFailed("response body too large")
                    chunks.append(chunk)
                response_body = b"".join(chunks)
                status = response.status_code
        except (httpx.HTTPError, OSError) as exc:
            raise SubmissionFailed(f"channel HTTP request failed: {exc}") from exc
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

    @staticmethod
    def _validate_completion(completion: Completion, ref: EventRef) -> None:
        if completion.ref != ref:
            raise SubmissionFailed("submission correlation mismatch")
        if completion.state != "outcome_unavailable" and not completion.invocation_id:
            raise SubmissionFailed("completion missing invocation correlation")

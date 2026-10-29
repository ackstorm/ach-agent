# SPDX-License-Identifier: MIT
"""Bounded ordinary-HTTP delivery to an authenticated Workspace mini-harness."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx
from pydantic import ValidationError

from ach_runtime.config.public import PublicConfig
from ach_runtime.execution.wire import CancelRequest, RunStatus, StartRequest, Started
from ach_runtime.harness.admission import AdmissionTicket, RecoveredRun
from ach_runtime.harness.tokens import engine_bearer
from ach_runtime.harness.workspaces import (
    WorkspaceAssemblyUnavailable,
    WorkspaceEndpoint,
    WorkspaceManager,
    WorkspaceRef,
    instance_changed,
)

MAX_RESPONSE_BYTES = 1024 * 1024
Prepare = Callable[[AdmissionTicket, WorkspaceEndpoint], Awaitable[StartRequest]]
Clock = Callable[[], datetime]
Alarm = Callable[[str, AdmissionTicket | RecoveredRun], None]


# A connect that hangs must fail as httpx.ConnectTimeout (nothing sent) before the overall
# asyncio deadline fires as a bare TimeoutError (ambiguous).
_CONNECT_TIMEOUT_SECONDS = 10.0


class DeliveryError(RuntimeError):
    """The remote HTTP operation was rejected or returned an invalid contract record."""


class GatewayUnavailable(DeliveryError):
    """No Start was sent: the workspace was unreachable until the deadline while the host
    reported its gateway down."""


class StartOutcomeUnknown(DeliveryError):
    """A Start may have reached the mini, but exact engine acceptance was not proven."""


class _StartRejected(DeliveryError):
    """The mini returned a definite non-202 response to Start."""


class Delivery:
    def __init__(
        self,
        manager: WorkspaceManager,
        *,
        key: bytes,
        public: PublicConfig,
        prepare: Prepare | None,
        clock: Clock,
        alarm: Alarm,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not key:
            raise ValueError("Harness key must not be empty")
        if prepare is None:
            raise WorkspaceAssemblyUnavailable("Workspace preparation callback is required")
        self.manager = manager
        self.key = key
        self.public = public
        self.prepare = prepare
        self.clock = clock
        self.alarm = alarm
        self.client = httpx.AsyncClient(transport=transport, timeout=None)
        self._closed = False

    async def _endpoint(self, ticket: AdmissionTicket) -> WorkspaceEndpoint:
        return await self.manager.ensure(
            WorkspaceRef(ticket.namespace, ticket.agent_uid, ticket.workspace_ref),
            deadline_at=ticket.deadline_at,
        )

    def _remaining(self, deadline_at: datetime) -> float:
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise DeliveryError("Delivery clock must return an aware datetime")
        remaining = (deadline_at - now).total_seconds()
        if remaining <= 0:
            raise TimeoutError("invocation deadline expired")
        return remaining

    def _headers(self, endpoint: WorkspaceEndpoint) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {engine_bearer(self.key, endpoint.name)}",
            "X-ACH-Instance-Id": endpoint.instance_id,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        endpoint: WorkspaceEndpoint,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        deadline_at: datetime | None = None,
    ) -> tuple[int, bytes]:
        if self._closed:
            raise DeliveryError("delivery is closed")
        timeout = self._remaining(deadline_at) if deadline_at is not None else 10.0
        request = self.client.build_request(
            method, endpoint.base_url.rstrip("/") + path,
            headers=self._headers(endpoint), json=payload,
            timeout=httpx.Timeout(timeout, connect=min(timeout, _CONNECT_TIMEOUT_SECONDS)),
        )
        response: httpx.Response | None = None
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.send(request, stream=True)
                if response.headers.get("X-ACH-Instance-Id") != endpoint.instance_id:
                    if instance_changed(response, endpoint):
                        self.manager.report_lost(endpoint)
                    raise DeliveryError("Workspace response instance ID did not match endpoint")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise DeliveryError("Workspace response exceeds 1 MiB")
                    chunks.append(chunk)
                return response.status_code, b"".join(chunks)
        finally:
            if response is not None:
                await response.aclose()

    @staticmethod
    def _json(raw: bytes):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DeliveryError("Workspace response was not complete valid JSON") from exc

    async def _started(
        self, endpoint: WorkspaceEndpoint, payload: dict, deadline_at: datetime
    ) -> Started:
        status, raw = await self._request(
            endpoint, "POST", "/start", payload=payload, deadline_at=deadline_at
        )
        if status != 202:
            raise _StartRejected(f"Workspace start returned HTTP {status}")
        try:
            result = Started.model_validate(self._json(raw))
        except ValidationError as exc:
            raise DeliveryError("Workspace returned an invalid Started receipt") from exc
        if (
            result.invocation_id != payload["invocationId"]
            or result.session_ref != payload["sessionRef"]
            or result.config_version != payload["configVersion"]
            or not result.run_id
        ):
            raise DeliveryError("Workspace Started receipt identity did not match request")
        return result

    async def start(self, ticket: AdmissionTicket) -> Started:
        try:
            public = self.public
            config_version = public.config_version
            if ticket.namespace != public.agent.namespace or ticket.agent_uid != public.agent.uid:
                raise DeliveryError("ticket identity does not match PublicConfig")
            remaining = self._remaining(ticket.deadline_at)
            if not self.manager.gateway_available() and not await self.manager.wait_gateway(
                time.monotonic() + remaining
            ):
                # Nothing has been sent, so waiting for the gateway leaves no ambiguity.
                raise GatewayUnavailable("gateway_unavailable")
            endpoint = await asyncio.wait_for(self._endpoint(ticket), self._remaining(ticket.deadline_at))
            self._remaining(ticket.deadline_at)
            request = await asyncio.wait_for(
                self.prepare(ticket, endpoint), self._remaining(ticket.deadline_at)
            )
            if not isinstance(request, StartRequest):
                raise WorkspaceAssemblyUnavailable("prepare must return a validated StartRequest")
            if (
                request.invocation_id != ticket.invocation_id
                or request.session_ref != ticket.session_ref
                or request.workspace_ref != ticket.workspace_ref
                or request.deadline_at != ticket.deadline_at
                or request.config_version != config_version
            ):
                raise DeliveryError("prepared StartRequest does not match immutable ticket/config")
            payload = request.model_dump(by_alias=True, mode="json")
            delay = 0.5
            while True:
                try:
                    return await self._started(endpoint, payload, ticket.deadline_at)
                except _StartRejected:
                    raise
                except DeliveryError as rejection:
                    self.alarm(str(rejection), ticket)
                    raise StartOutcomeUnknown("could not prove exact Started receipt") from rejection
                except (httpx.TransportError, TimeoutError) as first_error:
                    if isinstance(
                        first_error, (httpx.ConnectError, httpx.ConnectTimeout)
                    ) and not self.manager.gateway_available():
                        # The connection was refused while the host reports its gateway
                        # down: nothing was sent, so retrying is safe until the deadline.
                        if self._remaining(ticket.deadline_at) <= delay:
                            raise GatewayUnavailable("gateway_unavailable") from first_error
                        await asyncio.sleep(delay)
                        delay = min(delay * 2, 5.0)
                        continue
                    # An ambiguous first send may only be resolved against the same pod's
                    # retained run record, followed by an identical idempotent Start.
                    try:
                        current_endpoint = await asyncio.wait_for(
                            self._endpoint(ticket), self._remaining(ticket.deadline_at)
                        )
                        if current_endpoint.instance_id != endpoint.instance_id:
                            raise StartOutcomeUnknown("Workspace instance ID changed after ambiguous Start")
                        status_code, raw = await self._request(
                            current_endpoint, "GET", f"/status/{ticket.invocation_id}",
                            deadline_at=ticket.deadline_at,
                        )
                        if status_code != 200:
                            raise StartOutcomeUnknown("no retained run record proves Start outcome")
                        run_status = RunStatus.model_validate(self._json(raw))
                        self._validate_status(run_status, request)
                        # preparing/sending can have a run ID before native acceptance;
                        # replaying the exact body to the same pod waits for cached Started.
                        # Re-read ownership again immediately before the only permitted retry.
                        retry_endpoint = await asyncio.wait_for(
                            self._endpoint(ticket), self._remaining(ticket.deadline_at)
                        )
                        if retry_endpoint.instance_id != endpoint.instance_id:
                            raise StartOutcomeUnknown("Workspace instance ID changed before cached retry")
                        return await self._started(retry_endpoint, payload, ticket.deadline_at)
                    except Exception as recovery_error:
                        self.alarm(str(recovery_error), ticket)
                        raise StartOutcomeUnknown("could not prove exact Started receipt") from first_error
        except StartOutcomeUnknown:
            raise

    @staticmethod
    def _validate_status(result: RunStatus, expected: StartRequest | RunStatus) -> None:
        if (
            result.invocation_id != expected.invocation_id
            or result.channel != expected.channel
            or result.workspace_ref != expected.workspace_ref
            or result.session_ref != expected.session_ref
            or result.config_version != expected.config_version
            or result.deadline_at != expected.deadline_at
            or (isinstance(expected, RunStatus) and expected.run_id is not None
                and result.run_id != expected.run_id)
        ):
            raise DeliveryError("Workspace RunStatus identity did not match expected run")

    async def status(self, endpoint: WorkspaceEndpoint, invocation_id: str) -> RunStatus:
        code, raw = await self._request(endpoint, "GET", f"/status/{invocation_id}")
        if code != 200:
            raise DeliveryError(f"Workspace status returned HTTP {code}")
        try:
            return RunStatus.model_validate(self._json(raw))
        except ValidationError as exc:
            raise DeliveryError("Workspace returned invalid RunStatus") from exc

    async def list_runs(self, endpoint: WorkspaceEndpoint) -> list[RunStatus]:
        code, raw = await self._request(endpoint, "GET", "/runs")
        if code != 200:
            raise DeliveryError(f"Workspace runs returned HTTP {code}")
        try:
            payload = self._json(raw)
            if not isinstance(payload, list):
                raise ValueError("expected a list")
            return [RunStatus.model_validate(item) for item in payload]
        except (ValidationError, ValueError) as exc:
            raise DeliveryError("Workspace returned invalid run inventory") from exc

    async def cancel(self, endpoint: WorkspaceEndpoint, request: CancelRequest) -> None:
        code, _ = await self._request(
            endpoint, "POST", "/cancel", payload=request.model_dump(by_alias=True, mode="json")
        )
        if code != 200:
            raise DeliveryError(f"Workspace cancel returned HTTP {code}")

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await self.client.aclose()

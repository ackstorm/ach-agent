"""Concrete HTTP client for the native execution mini-harness.

The client keeps the controller ownership stream separate from invocation streams and
uses a small control pool for acknowledgements, cancellation and cleanup.  A long turn
therefore cannot consume the connection needed to stop it or to release the session-ready
gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any

import httpx

from ach_agent.engine import trace
from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.base.events import (
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ToolState,
    ToolStateCompleted,
    ToolStateError,
    ToolStateRunning,
)
from ach_agent.engine.workspace import workspace_dir
from ach_agent.execution.service import MAX_NDJSON_RECORD_BYTES
from ach_agent.execution.state import MAX_MIGRATION_ROWS, LegacySessionRow
from ach_agent.execution.wire import (
    AcquireRequest,
    ControllerHello,
    ExecutionEvent,
    ExecutionHandle,
    ReleaseRequest,
    SessionImportRequest,
    SessionImportRow,
    SessionOperation,
    SessionReadyRequest,
    TurnRequest,
    WorkspaceCancelRequest,
    WorkspaceCleanupAckRequest,
    WorkspaceHandoffRequest,
    WorkspaceOperationFailure,
    WorkspacePrepareRequest,
    WorkspaceStoppedEvent,
)

# Cancellation is a control-plane operation and must retain a finite safety bound
# even when the caller did not provide an operation deadline.  The service owns a
# ten-second native cleanup bound; this small margin covers HTTP response delivery.
WORKSPACE_CANCEL_TIMEOUT_SECONDS = 15.0
NATIVE_CLEANUP_TIMEOUT_SECONDS = 10.0
HOOK_CLEANUP_MARGIN_SECONDS = 5.0


class ExecutionClientError(RuntimeError):
    """An HTTP or malformed execution response."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class WorkspaceOperationFailed(ExecutionClientError):
    """A completed workspace operation failed without invalidating controller admission."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        confirmed: bool = False,
        rejection: bool = False,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.confirmed = confirmed
        self.rejection = rejection


class ExecutionClientLaunchFailed(ExecutionClientError):
    """The execution service could not launch the requested native engine."""


async def _bounded_lines(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Split NDJSON incrementally without allowing an unterminated record to grow."""
    buffer = bytearray()
    async for chunk in chunks:
        start = 0
        while True:
            newline = chunk.find(b"\n", start)
            part = chunk[start:] if newline < 0 else chunk[start:newline]
            if len(buffer) + len(part) > MAX_NDJSON_RECORD_BYTES:
                raise ExecutionClientError("NDJSON record exceeds 1 MiB")
            buffer.extend(part)
            if newline < 0:
                break
            yield bytes(buffer).rstrip(b"\r")
            buffer.clear()
            start = newline + 1
    if buffer:
        yield bytes(buffer).rstrip(b"\r")


def _json_record(line: bytes) -> ExecutionEvent:
    if len(line) > MAX_NDJSON_RECORD_BYTES:
        raise ExecutionClientError("NDJSON record exceeds 1 MiB")
    try:
        value = json.loads(line)
    except (TypeError, ValueError) as exc:
        raise ExecutionClientError("invalid execution NDJSON record") from exc
    try:
        return ExecutionEvent.model_validate(value)
    except Exception as exc:  # pydantic's ValidationError is deliberately a wire detail
        raise ExecutionClientError("invalid execution event") from exc


def _usage_from_wire(value: Any) -> OpenCodeUsage | None:
    if isinstance(value, OpenCodeUsage):
        return value
    if not isinstance(value, dict):
        return None
    try:
        return OpenCodeUsage(
            session_id=str(value.get("session_id", "")),
            message_id=str(value.get("message_id", "")),
            input_tokens=int(value.get("input_tokens", 0) or 0),
            output_tokens=int(value.get("output_tokens", 0) or 0),
            cache_read=int(value.get("cache_read", 0) or 0),
            cache_write=int(value.get("cache_write", 0) or 0),
            cost=float(value.get("cost", 0.0) or 0.0),
            duration_ms=int(value.get("duration_ms", 0) or 0),
        )
    except (TypeError, ValueError):
        return None


def _tool_from_wire(value: Any) -> OpenCodeToolUpdate | None:
    if isinstance(value, OpenCodeToolUpdate):
        return value
    if not isinstance(value, dict):
        return None
    state_value = value.get("state")
    if not isinstance(state_value, dict):
        return None
    status = state_value.get("status")
    if status == "running":
        state: ToolState = ToolStateRunning(
            input=state_value.get("input"), title=str(state_value.get("title", ""))
        )
    elif status == "completed":
        state = ToolStateCompleted(
            output=str(state_value.get("output", "")),
            input=state_value.get("input"),
            title=str(state_value.get("title", "")),
        )
    elif status == "error":
        state = ToolStateError(
            error=str(state_value.get("error", "")), input=state_value.get("input")
        )
    else:
        return None
    return OpenCodeToolUpdate(
        session_id=str(value.get("session_id", "")),
        part_id=str(value.get("part_id", "")),
        message_id=str(value.get("message_id", "")),
        tool_name=str(value.get("tool_name", "")),
        call_id=str(value.get("call_id", "")),
        state=state,
    )


class ExecutionClient:
    """HTTP implementation of the execution service's operation contract."""

    def __init__(
        self,
        base_url: str,
        *,
        controller_id: str,
        instance_id: str | None = None,
        timeout: float | None = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        limits = httpx.Limits(max_connections=8, max_keepalive_connections=8)
        control_limits = httpx.Limits(max_connections=2, max_keepalive_connections=2)
        controller_limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        self.controller_id = controller_id
        self.instance_id = instance_id
        self.timeout = timeout or 30.0
        self.base_url = base_url.rstrip("/")
        self.controller_client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=None,
            limits=controller_limits,
            transport=transport,
        )
        self.control_client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            limits=control_limits,
            transport=transport,
        )
        # Long release/cancel responses may wait for native and hook cleanup. Keep
        # them away from both the short priority ACKs and ordinary control calls.
        self.cleanup_client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=None,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            transport=transport,
        )
        # Keep acknowledgement/cancellation capacity independent of session
        # operations and release calls, which may wait on native cleanup.
        self.priority_client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
            transport=transport,
        )
        # Acquisition can wait for a cold native launch.  Keep it out of the two
        # connections reserved for session-ready/cancel/release control traffic.
        self.acquire_client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=None,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
            transport=transport,
        )
        self.stream_client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=None,
            limits=limits,
            transport=transport,
        )
        self._controller_response: httpx.Response | None = None
        self._controller_iterator: AsyncIterator[bytes] | None = None
        self._controller_monitor: asyncio.Task[None] | None = None
        self._controller_lost = False
        self._failed = False
        self._failure_reason: str | None = None
        self._owned_tasks: set[asyncio.Task[Any]] = set()
        self._owned_responses: set[httpx.Response] = set()
        self._cancelled_invocations: set[str] = set()
        # A turn can confirm cancellation before the runner's outer failure path
        # reaches its finally block.  Retain that confirmation only until the
        # runner finalizes the same already-acquired handle.
        self._confirmed_cancellations: dict[str, ExecutionHandle] = {}
        self._active_turns: set[str] = set()
        self._controller_events: asyncio.Queue[WorkspaceStoppedEvent] = asyncio.Queue(maxsize=64)
        self._controller_event_waiters: set[asyncio.Future[WorkspaceStoppedEvent]] = set()
        self._handles: dict[str, ExecutionHandle] = {}
        self._cleanup_budgets: dict[str, float] = {}
        self._turn_ids: dict[str, itertools.count[int]] = {}
        self._closed = False

    async def connect(self) -> ControllerHello:
        """Claim the mini-harness and retain its held controller connection."""
        if self._controller_response is not None:
            raise ExecutionClientError("controller is already connected")
        if self.instance_id is None:
            response = await self.control_client.send(
                self.control_client.build_request("GET", "/execution/v1/health"), stream=True
            )
            body = await self._bounded_response(response)
            if response.status_code != 200:
                raise ExecutionClientError(
                    "execution health check failed", status_code=response.status_code
                )
            try:
                self.instance_id = str(json.loads(body)["instance_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ExecutionClientError("invalid execution health response") from exc
        request = self.controller_client.build_request(
            "POST",
            "/execution/v1/controller",
            json={
                "version": 1,
                "instance_id": self.instance_id,
                "controller_id": self.controller_id,
            },
        )
        try:
            response = await asyncio.wait_for(
                self.controller_client.send(request, stream=True), timeout=self.timeout
            )
        except TimeoutError as exc:
            raise ExecutionClientError("execution controller claim timed out") from exc
        if response.status_code != 200:
            await response.aclose()
            raise ExecutionClientError(
                "execution controller claim failed", status_code=response.status_code
            )
        try:
            iterator = _bounded_lines(response.aiter_bytes())
            line = await asyncio.wait_for(iterator.__anext__(), timeout=self.timeout)
            hello = ControllerHello.model_validate(json.loads(line))
        except ExecutionClientError:
            await response.aclose()
            raise
        except (StopAsyncIteration, TypeError, ValueError) as exc:
            await response.aclose()
            raise ExecutionClientError("invalid execution controller hello") from exc
        except TimeoutError as exc:
            await response.aclose()
            raise ExecutionClientError("execution controller hello timed out") from exc
        if (
            hello.version != 1
            or hello.instance_id != self.instance_id
            or hello.controller_id != self.controller_id
        ):
            await response.aclose()
            raise ExecutionClientError("execution controller hello identity mismatch")
        self._controller_response = response
        self._controller_iterator = iterator
        self._controller_monitor = asyncio.create_task(self._monitor_controller(iterator))
        return hello

    async def _monitor_controller(self, iterator: AsyncIterator[bytes]) -> None:
        try:
            async for line in iterator:
                if not line:
                    continue
                try:
                    event = WorkspaceStoppedEvent.model_validate(json.loads(line))
                except (TypeError, ValueError) as exc:
                    raise ExecutionClientError("invalid controller event") from exc
                if (
                    event.controller_id != self.controller_id
                    or event.instance_id != self.instance_id
                ):
                    raise ExecutionClientError("controller event identity mismatch")
                delivered = False
                while self._controller_event_waiters:
                    waiter = next(iter(self._controller_event_waiters))
                    self._controller_event_waiters.remove(waiter)
                    if waiter.done():
                        continue
                    waiter.set_result(event)
                    delivered = True
                    break
                if delivered:
                    continue
                try:
                    self._controller_events.put_nowait(event)
                except asyncio.QueueFull as exc:
                    raise ExecutionClientError("controller event buffer is full") from exc
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            self._controller_lost = True
            await self._fail_admission(exc)
            await self._close_owned_transport()
            return
        else:
            self._controller_lost = True
            await self._fail_admission("execution controller connection is lost")
            await self._close_owned_transport()
            return

    async def claim_controller(self) -> ControllerHello:
        """Compatibility spelling matching ``ExecutionService.claim_controller``."""
        return await self.connect()

    async def graceful_stop(self) -> None:
        """Ask E to finish warm cleanup while the controller event pump is live."""
        self._assert_controller_live()
        await self._json_request(
            "POST",
            "/execution/v1/controller/stop",
            {"controller_id": self.controller_id},
        )

    @property
    def controller_lost(self) -> bool:
        return self._controller_lost or self._failed

    async def next_controller_event(self) -> WorkspaceStoppedEvent:
        """Wait for a correlated engine lifecycle event from the held controller stream."""
        self._assert_controller_live()
        try:
            return self._controller_events.get_nowait()
        except asyncio.QueueEmpty:
            waiter: asyncio.Future[WorkspaceStoppedEvent] = (
                asyncio.get_running_loop().create_future()
            )
            self._controller_event_waiters.add(waiter)
            try:
                return await waiter
            finally:
                self._controller_event_waiters.discard(waiter)

    async def _json_request(
        self,
        method: str,
        path: str,
        body: Any,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> Any:
        request_client = self.control_client if client is None else client
        try:
            response, content = await self._owned_request(
                request_client,
                request_client.build_request(method, path, json=body),
            )
            if response.status_code < 200 or response.status_code >= 300:
                detail = content[:512].decode("utf-8", "replace")
                raise ExecutionClientError(
                    f"execution {method} {path} failed: {detail}",
                    status_code=response.status_code,
                )
            if not content:
                return None
            try:
                return json.loads(content)
            except ValueError as exc:
                raise ExecutionClientError("invalid execution JSON response") from exc
        except ExecutionClientError as exc:
            await self._fail_admission(exc)
            raise

    async def import_legacy_sessions(
        self, rows: Iterable[LegacySessionRow | SessionImportRow | Mapping[str, Any]]
    ) -> int:
        """Send the bounded row export to E before the first acquired execution."""
        self._assert_controller_live()
        self._validate_controller(self.controller_id)
        bounded = list(itertools.islice(rows, MAX_MIGRATION_ROWS + 1))
        if len(bounded) > MAX_MIGRATION_ROWS:
            raise ExecutionClientError(f"legacy session import exceeds {MAX_MIGRATION_ROWS} rows")
        typed_rows: list[SessionImportRow] = []
        for row in bounded:
            if isinstance(row, LegacySessionRow):
                typed_rows.append(
                    SessionImportRow(
                        key=row.key,
                        oc_session_id=row.oc_session_id,
                        last_used=row.last_used,
                    )
                )
            elif isinstance(row, SessionImportRow):
                typed_rows.append(row)
            else:
                try:
                    # Accept the wire aliases too for callers that already
                    # serialized a row, while LegacySessionRow remains the
                    # canonical in-process export accepted from H.
                    try:
                        wire_row = SessionImportRow.model_validate(row)
                    except (TypeError, ValueError):
                        legacy_row = LegacySessionRow.model_validate(row)
                        wire_row = SessionImportRow(
                            key=legacy_row.key,
                            oc_session_id=legacy_row.oc_session_id,
                            last_used=legacy_row.last_used,
                        )
                    typed_rows.append(wire_row)
                except (TypeError, ValueError) as exc:
                    raise ExecutionClientError("invalid legacy session row") from exc
        result = await self._json_request(
            "POST",
            "/execution/v1/session-import",
            SessionImportRequest(
                controller_id=self.controller_id,
                rows=typed_rows,
            ).model_dump(mode="json"),
        )
        imported = result.get("imported") if isinstance(result, dict) else None
        if type(imported) is not int or imported < 0 or imported > len(typed_rows):
            raise ExecutionClientError("invalid legacy session import response")
        return imported

    def _assert_controller_live(self) -> None:
        if self._closed:
            raise ExecutionClientError("execution client is closed")
        if self._failed or self._controller_lost:
            raise ExecutionClientError(
                self._failure_reason or "execution controller connection is lost"
            )

    async def _owned_request(
        self, client: httpx.AsyncClient, request: httpx.Request
    ) -> tuple[httpx.Response, bytes]:
        self._assert_controller_live()
        response = await self._owned_send(client, request)
        content = await self._owned_response(response)
        return response, content

    async def _owned_response(self, response: httpx.Response) -> bytes:
        self._owned_responses.add(response)
        task = asyncio.create_task(self._bounded_response(response))
        self._owned_tasks.add(task)
        try:
            return await asyncio.shield(task)
        finally:
            self._owned_responses.discard(response)
            self._owned_tasks.discard(task)
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def _confirm_workspace_cancel(
        self,
        controller_id: str,
        invocation_id: str,
        *,
        execution_id: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self._assert_controller_live()
        self._validate_controller(controller_id)
        try:
            response, content = await self._owned_request(
                self.cleanup_client,
                self.cleanup_client.build_request(
                    "POST",
                    "/execution/v1/cancel",
                    json=WorkspaceCancelRequest(
                        controller_id=controller_id,
                        invocation_id=invocation_id,
                        execution_id=execution_id,
                    ).model_dump(mode="json", exclude_none=True),
                    timeout=max(
                        WORKSPACE_CANCEL_TIMEOUT_SECONDS,
                        timeout
                        or (
                            NATIVE_CLEANUP_TIMEOUT_SECONDS
                            + self._cleanup_budgets.get(invocation_id, 0.0)
                            + HOOK_CLEANUP_MARGIN_SECONDS
                        ),
                    ),
                ),
            )
            if response.status_code < 200 or response.status_code >= 300:
                raise ExecutionClientError(
                    f"workspace cancellation failed: {content[:512].decode('utf-8', 'replace')}",
                    status_code=response.status_code,
                )
            try:
                result = json.loads(content)
            except ValueError as exc:
                raise ExecutionClientError("invalid workspace cancellation response") from exc
            if result != {"status": "ok"}:
                raise ExecutionClientError("invalid workspace cancellation acknowledgement")
        except BaseException as exc:
            await self._fail_admission(exc)
            raise

    async def _workspace_json_request(
        self,
        path: str,
        body: WorkspacePrepareRequest | WorkspaceHandoffRequest,
    ) -> Any:
        self._assert_controller_live()
        self._validate_controller(body.controller_id)
        remaining = body.remaining_seconds
        try:
            response, content = await asyncio.wait_for(
                self._owned_request(
                    self.acquire_client,
                    self.acquire_client.build_request(
                        "POST", path, json=body.model_dump(mode="json")
                    ),
                ),
                timeout=remaining,
            )
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await asyncio.shield(
                    self._confirm_workspace_cancel(body.controller_id, body.invocation_id)
                )
            raise
        except TimeoutError as exc:
            await self._confirm_workspace_cancel(body.controller_id, body.invocation_id)
            raise WorkspaceOperationFailed(
                f"workspace operation timed out after {remaining}s"
            ) from exc
        except WorkspaceOperationFailed:
            raise
        except BaseException as exc:
            await self._confirm_workspace_cancel(body.controller_id, body.invocation_id)
            raise WorkspaceOperationFailed("workspace operation response was ambiguous") from exc
        if response.status_code < 200 or response.status_code >= 300:
            detail = content[:512].decode("utf-8", "replace")
            try:
                payload = json.loads(content)
            except ValueError:
                payload = None
            if isinstance(payload, dict) and payload.get("type") == "WorkspaceOperationFailed":
                try:
                    failure = WorkspaceOperationFailure.model_validate(payload)
                except ValueError:
                    failure = None
                if failure is not None:
                    if not failure.confirmed:
                        await self._confirm_workspace_cancel(body.controller_id, body.invocation_id)
                    raise WorkspaceOperationFailed(
                        failure.message,
                        status_code=response.status_code,
                        confirmed=failure.confirmed,
                    )
            if response.status_code in (404, 409, 422) and isinstance(payload, dict):
                rejection = payload.get("detail")
                if isinstance(rejection, str):
                    raise WorkspaceOperationFailed(
                        rejection,
                        status_code=response.status_code,
                        confirmed=True,
                        rejection=True,
                    )
            try:
                await self._confirm_workspace_cancel(body.controller_id, body.invocation_id)
            except BaseException as cancel_error:
                raise WorkspaceOperationFailed(
                    f"execution {path} failed and cancellation was uncertain: {detail}",
                    status_code=response.status_code,
                    confirmed=False,
                ) from cancel_error
            raise WorkspaceOperationFailed(
                f"execution {path} failed and was canceled: {detail}",
                status_code=response.status_code,
                confirmed=True,
            )
        try:
            return json.loads(content)
        except ValueError as exc:
            await self._confirm_workspace_cancel(body.controller_id, body.invocation_id)
            raise WorkspaceOperationFailed(
                "invalid workspace operation response; reservation canceled", confirmed=True
            ) from exc

    def _validate_controller(self, controller_id: str) -> None:
        if controller_id != self.controller_id:
            raise ExecutionClientError("execution request has the wrong controller")

    def _validate_handle(
        self, controller_id: str, execution_id: str, invocation_id: str
    ) -> ExecutionHandle:
        self._validate_controller(controller_id)
        handle = self._handles.get(invocation_id)
        if (
            handle is None
            or handle.controller_id != controller_id
            or handle.execution_id != execution_id
            or (self.instance_id is not None and handle.instance_id != self.instance_id)
        ):
            raise ExecutionClientError("execution request identity mismatch")
        return handle

    async def _fail_admission(self, reason: BaseException | str) -> None:
        self._failed = True
        self._controller_lost = True
        self._failure_reason = str(reason)
        error = reason if isinstance(reason, BaseException) else ExecutionClientError(str(reason))
        self._wake_controller_waiters(error)
        if self._controller_response is not None:
            with contextlib.suppress(BaseException):
                await self._controller_response.aclose()

    def _wake_controller_waiters(self, error: BaseException) -> None:
        waiters = tuple(self._controller_event_waiters)
        self._controller_event_waiters.clear()
        for waiter in waiters:
            if not waiter.done():
                waiter.set_exception(error)

    async def _close_owned_transport(self) -> None:
        responses = tuple(self._owned_responses)
        for response in responses:
            with contextlib.suppress(BaseException):
                await response.aclose()
        self._owned_responses.clear()
        pending = tuple(self._owned_tasks)
        for task in pending:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def acquire(self, request: AcquireRequest) -> ExecutionHandle:
        self._assert_controller_live()
        self._validate_controller(request.controller_id)
        try:
            response, content = await self._owned_request(
                self.acquire_client,
                self.acquire_client.build_request(
                    "POST", "/execution/v1/acquire", json=request.model_dump(mode="json")
                ),
            )
        except BaseException as exc:
            await self._fail_admission(exc)
            raise
        try:
            if response.status_code < 200 or response.status_code >= 300:
                if response.status_code == 502:
                    with contextlib.suppress(ValueError, TypeError):
                        failure = json.loads(content)
                        if (
                            isinstance(failure, dict)
                            and failure.get("type") == "LaunchFailed"
                            and isinstance(failure.get("message"), str)
                        ):
                            raise ExecutionClientLaunchFailed(
                                failure["message"], status_code=response.status_code
                            )
                raise ExecutionClientError(
                    f"execution acquire failed: {content[:512].decode('utf-8', 'replace')}",
                    status_code=response.status_code,
                )
            try:
                value = json.loads(content)
            except ValueError as exc:
                raise ExecutionClientError("invalid execution JSON response") from exc
            try:
                handle = ExecutionHandle.model_validate(value)
            except Exception as exc:
                raise ExecutionClientError("invalid execution handle") from exc
            if (
                handle.controller_id != request.controller_id
                or handle.invocation_id != request.invocation_id
                or handle.instance_id != self.instance_id
            ):
                raise ExecutionClientError("execution handle identity mismatch")
            self._assert_controller_live()
        except ExecutionClientLaunchFailed:
            raise
        except ExecutionClientError as exc:
            await self._fail_admission(exc)
            raise
        trace.adopt(handle.proxy_route)
        self._handles[handle.invocation_id] = handle
        self._turn_ids.setdefault(handle.invocation_id, itertools.count(1))
        return handle

    async def prepare_workspace(self, request: WorkspacePrepareRequest) -> dict[str, str]:
        """Prepare a public workspace before native acquisition."""
        if request.invocation_id in self._cleanup_budgets:
            raise ExecutionClientError("workspace preparation is already active")
        self._cleanup_budgets[request.invocation_id] = request.cleanup_budget_seconds
        try:
            result = await self._workspace_json_request("/execution/v1/workspace/prepare", request)
        except BaseException:
            self._cleanup_budgets.pop(request.invocation_id, None)
            raise
        expected = str(workspace_dir(request.work_dir, request.session_key))
        if (
            not isinstance(result, dict)
            or result.get("status") != "ok"
            or not isinstance(result.get("workspace"), str)
            or result["workspace"] != expected
        ):
            await self._confirm_workspace_cancel(request.controller_id, request.invocation_id)
            self._cleanup_budgets.pop(request.invocation_id, None)
            raise WorkspaceOperationFailed(
                "invalid workspace prepare response; reservation canceled", confirmed=True
            )
        return {"status": "ok", "workspace": expected}

    async def handoff_workspace(self, request: WorkspaceHandoffRequest) -> dict[str, str]:
        """Import a credential-free shared-workspace bundle before native acquisition."""
        result = await self._workspace_json_request("/execution/v1/workspace/handoff", request)
        expected = str(workspace_dir(request.work_dir, request.session_key))
        if (
            not isinstance(result, dict)
            or result.get("status") != "ok"
            or not isinstance(result.get("workspace"), str)
            or result["workspace"] != expected
        ):
            await self._confirm_workspace_cancel(request.controller_id, request.invocation_id)
            raise WorkspaceOperationFailed(
                "invalid workspace handoff response; reservation canceled", confirmed=True
            )
        return {"status": "ok", "workspace": expected}

    async def ack_workspace_cleanup(self, event: WorkspaceStoppedEvent) -> None:
        """Acknowledge private cleanup after the correlated controller event completes."""
        self._assert_controller_live()
        result = await self._json_request(
            "POST",
            "/execution/v1/workspace/cleanup-ack",
            WorkspaceCleanupAckRequest(
                controller_id=event.controller_id,
                instance_id=event.instance_id,
                session_key=event.session_key,
                event_id=event.event_id,
                invocation_id=event.invocation_id,
            ).model_dump(mode="json"),
            # ACKs stay on the small priority pool so long release/cancel responses
            # cannot consume both short control slots while waiting for this barrier.
            client=self.priority_client,
        )
        await self._require_ok(result, "workspace cleanup")

    async def _ack_session(self, request: TurnRequest, event: ExecutionEvent) -> None:
        handle = self._validate_handle(
            request.controller_id, request.execution_id, request.invocation_id
        )
        payload = event.payload
        session_ref = payload.get("session_ref") if isinstance(payload, dict) else None
        if not isinstance(session_ref, str) or not session_ref:
            raise ExecutionClientError("session event omitted its native reference")
        # This is the harness-side trace registry.  The native ref is diagnostic state,
        # never a caller-selected turn target.
        trace.set_session(handle.proxy_route, session_ref)
        result = await self._json_request(
            "POST",
            "/execution/v1/session-ready",
            SessionReadyRequest(
                controller_id=request.controller_id,
                execution_id=request.execution_id,
                invocation_id=request.invocation_id,
                turn_id=request.turn_id,
            ).model_dump(mode="json"),
            client=self.priority_client,
        )
        await self._require_ok(result, "session-ready")

    async def _require_ok(self, result: Any, operation: str) -> None:
        if result != {"status": "ok"}:
            error = ExecutionClientError(f"invalid {operation} acknowledgement")
            await self._fail_admission(error)
            raise error

    async def turn(self, request: TurnRequest) -> AsyncIterator[ExecutionEvent]:
        """Stream one bounded turn, acknowledging every resolved native session."""
        self._assert_controller_live()
        self._validate_handle(request.controller_id, request.execution_id, request.invocation_id)
        if request.invocation_id in self._active_turns:
            raise ExecutionClientError("execution turn is already active")
        self._active_turns.add(request.invocation_id)
        try:
            response = await self._owned_send(
                self.stream_client,
                self.stream_client.build_request(
                    "POST", "/execution/v1/turn", json=request.model_dump(mode="json")
                ),
            )
        except BaseException:
            with contextlib.suppress(BaseException):
                await asyncio.shield(
                    self._cancel_stream(request.controller_id, request.invocation_id)
                )
            self._active_turns.discard(request.invocation_id)
            self._cancelled_invocations.discard(request.invocation_id)
            raise
        if response.status_code < 200 or response.status_code >= 300:
            try:
                detail = (await self._owned_response(response))[:512].decode("utf-8", "replace")
                raise ExecutionClientError(
                    f"execution turn failed: {detail}", status_code=response.status_code
                )
            except BaseException:
                if response.status_code < 400 or response.status_code >= 500:
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(
                            self._cancel_stream(request.controller_id, request.invocation_id)
                        )
                raise
            finally:
                self._active_turns.discard(request.invocation_id)
                self._cancelled_invocations.discard(request.invocation_id)
        self._owned_responses.add(response)
        lines = _bounded_lines(response.aiter_bytes())
        finished = False
        saw_turn_done = False
        stream_error: BaseException | None = None

        async def read_one() -> bytes:
            return await lines.__anext__()

        try:
            while True:
                self._assert_controller_live()
                read_task: asyncio.Task[bytes] = asyncio.create_task(read_one())
                self._owned_tasks.add(read_task)
                try:
                    line = await asyncio.shield(read_task)
                except StopAsyncIteration:
                    break
                finally:
                    self._owned_tasks.discard(read_task)
                    if not read_task.done():
                        read_task.cancel()
                        with contextlib.suppress(BaseException):
                            await read_task
                if not line:
                    continue
                event = _json_record(line)
                if (
                    event.execution_id != request.execution_id
                    or event.invocation_id != request.invocation_id
                    or event.turn_id != request.turn_id
                ):
                    raise ExecutionClientError("execution event identity mismatch")
                if saw_turn_done:
                    raise ExecutionClientError("out-of-order event after turn_done")
                if event.kind == "session_resolved":
                    await self._ack_session(request, event)
                if event.kind == "error":
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    raise ExecutionClientError(str(payload.get("message", "native turn failed")))
                if event.kind == "turn_done":
                    saw_turn_done = True
                yield event
            if not saw_turn_done:
                raise ExecutionClientError("execution stream ended before turn_done")
            finished = True
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await asyncio.shield(
                    self._cancel_stream(request.controller_id, request.invocation_id)
                )
            raise
        except BaseException as exc:
            stream_error = exc
            raise
        finally:
            self._owned_responses.discard(response)
            await response.aclose()
            if not finished and not self._closed:
                if request.invocation_id not in self._cancelled_invocations:
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(
                            self._cancel_stream(request.controller_id, request.invocation_id)
                        )
            if (
                isinstance(stream_error, httpx.HTTPError)
                and request.invocation_id not in self._cancelled_invocations
            ):
                await self._fail_admission(stream_error)
            self._cancelled_invocations.discard(request.invocation_id)
            self._active_turns.discard(request.invocation_id)

    def turn_callable(self, handle: ExecutionHandle) -> Callable[..., Awaitable[TurnResult]]:
        """Bind execution identity and expose the terminal policy's run-turn vocabulary."""
        sequence = self._turn_ids.setdefault(handle.invocation_id, itertools.count(1))

        async def run_turn(
            *,
            prompt: str,
            max_tool_calls: int,
            on_text: Callable[[str], None] | None,
            on_tool: Callable[[OpenCodeToolUpdate], None] | None,
            stats: dict[str, Any],
        ) -> TurnResult:
            turn_id = f"turn-{next(sequence)}"
            request = TurnRequest(
                controller_id=handle.controller_id,
                execution_id=handle.execution_id,
                invocation_id=handle.invocation_id,
                turn_id=turn_id,
                prompt=prompt,
                max_tool_calls=max_tool_calls,
            )
            result: TurnResult | None = None
            async for event in self.turn(request):
                if event.kind == "text":
                    if on_text is not None and isinstance(event.payload, str):
                        on_text(event.payload)
                elif event.kind == "tool":
                    tool = _tool_from_wire(event.payload)
                    if tool is not None and on_tool is not None:
                        on_tool(tool)
                elif event.kind == "usage":
                    usage = _usage_from_wire(event.payload)
                    if usage is not None:
                        stats["usage"] = usage
                elif event.kind == "turn_done":
                    payload = event.payload if isinstance(event.payload, dict) else {}
                    wire_stats = payload.get("stats")
                    if isinstance(wire_stats, dict):
                        stats.update(wire_stats)
                        if isinstance(wire_stats.get("usage"), dict):
                            usage = _usage_from_wire(wire_stats["usage"])
                            if usage is not None:
                                stats["usage"] = usage
                    session_ref = payload.get("session_ref")
                    if not isinstance(session_ref, str):
                        session_ref = ""
                    result = TurnResult(
                        text=str(payload.get("text", "")),
                        session_ref=session_ref,
                        aborted=bool(payload.get("aborted", False)),
                    )
            if result is None:
                raise ExecutionClientError("execution turn ended without a result")
            # Keep the native reference diagnostic in the harness turn stats.  The
            # terminal policy never targets it; runner maintenance uses the typed
            # invocation-scoped session operations instead.
            stats["session_ref"] = result.session_ref
            return result

        return run_turn

    async def session_op(self, request: SessionOperation) -> None:
        self._assert_controller_live()
        self._validate_handle(request.controller_id, request.execution_id, request.invocation_id)
        result = await self._json_request(
            "POST", "/execution/v1/session-op", request.model_dump(mode="json")
        )
        await self._require_ok(result, "session operation")

    async def release(self, request: ReleaseRequest) -> None:
        self._assert_controller_live()
        self._validate_handle(request.controller_id, request.execution_id, request.invocation_id)
        timeout = (
            NATIVE_CLEANUP_TIMEOUT_SECONDS
            + self._cleanup_budgets.get(request.invocation_id, 0.0)
            + HOOK_CLEANUP_MARGIN_SECONDS
        )
        try:
            result = await asyncio.wait_for(
                self._json_request(
                    "POST",
                    "/execution/v1/release",
                    request.model_dump(mode="json"),
                    client=self.cleanup_client,
                ),
                timeout=max(WORKSPACE_CANCEL_TIMEOUT_SECONDS, timeout),
            )
            await self._require_ok(result, "release")
        except BaseException as exc:
            await self._fail_admission(exc)
            raise
        handle = self._handles.pop(request.invocation_id, None)
        if handle is not None:
            trace.drop(handle.proxy_route)
        self._turn_ids.pop(request.invocation_id, None)
        self._cleanup_budgets.pop(request.invocation_id, None)

    async def cancel(self, controller_id: str, invocation_id: str) -> None:
        await self._cancel_owned(controller_id, invocation_id, retain_confirmation=False)

    async def _cancel_owned(
        self,
        controller_id: str,
        invocation_id: str,
        *,
        execution_id: str | None = None,
        retain_confirmation: bool,
    ) -> None:
        self._validate_controller(controller_id)
        active = invocation_id in self._active_turns
        if active:
            self._cancelled_invocations.add(invocation_id)
        try:
            await self._confirm_workspace_cancel(
                controller_id, invocation_id, execution_id=execution_id
            )
        except BaseException as exc:
            if active:
                self._cancelled_invocations.discard(invocation_id)
            await self._fail_admission(exc)
            raise
        handle = self._handles.pop(invocation_id, None)
        if handle is not None:
            trace.drop(handle.proxy_route)
            if retain_confirmation:
                self._confirmed_cancellations[invocation_id] = handle
        self._turn_ids.pop(invocation_id, None)
        self._cleanup_budgets.pop(invocation_id, None)
        if not active:
            self._cancelled_invocations.discard(invocation_id)

    async def _cancel_stream(self, controller_id: str, invocation_id: str) -> None:
        """Cancel a turn stream and retain confirmation for runner finalization."""
        handle = self._handles.get(invocation_id)
        execution_id = handle.execution_id if handle is not None else None
        await self._cancel_owned(
            controller_id,
            invocation_id,
            execution_id=execution_id,
            retain_confirmation=True,
        )

    async def cancel_handle(self, handle: ExecutionHandle) -> None:
        """Finalize cancellation for a handle acquired by this client.

        Turn streaming may already have confirmed and removed the handle.  The
        temporary confirmation is scoped to that invocation and is consumed here;
        an unknown or in-flight identity still fails closed.
        """
        self._validate_controller(handle.controller_id)
        current = self._handles.get(handle.invocation_id)
        if current is not None:
            if current != handle:
                raise ExecutionClientError("execution request identity mismatch")
            await self._cancel_owned(
                handle.controller_id,
                handle.invocation_id,
                execution_id=handle.execution_id,
                retain_confirmation=False,
            )
            return
        confirmed = self._confirmed_cancellations.get(handle.invocation_id)
        if confirmed == handle:
            self._confirmed_cancellations.pop(handle.invocation_id, None)
            return
        raise ExecutionClientError("execution request identity mismatch")

    async def close(self) -> None:
        self._closed = True
        self._wake_controller_waiters(ExecutionClientError("execution client is closed"))
        monitor = self._controller_monitor
        self._controller_monitor = None
        if monitor is not None:
            monitor.cancel()
            with contextlib.suppress(BaseException):
                await monitor
        pending = tuple(self._owned_tasks)
        for task in pending:
            if task is not asyncio.current_task() and not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        responses = tuple(self._owned_responses)
        for response in responses:
            with contextlib.suppress(BaseException):
                await response.aclose()
        self._owned_responses.clear()
        if self._controller_response is not None:
            await self._controller_response.aclose()
            self._controller_response = None
        for handle in self._handles.values():
            trace.drop(handle.proxy_route)
        self._handles.clear()
        self._turn_ids.clear()
        self._cleanup_budgets.clear()
        self._cancelled_invocations.clear()
        self._confirmed_cancellations.clear()
        self._active_turns.clear()
        while not self._controller_events.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._controller_events.get_nowait()
        await self.stream_client.aclose()
        await self.acquire_client.aclose()
        await self.cleanup_client.aclose()
        await self.priority_client.aclose()
        await self.control_client.aclose()
        await self.controller_client.aclose()

    async def _owned_send(
        self, client: httpx.AsyncClient, request: httpx.Request
    ) -> httpx.Response:
        self._assert_controller_live()
        task = asyncio.create_task(client.send(request, stream=True))
        self._owned_tasks.add(task)
        try:
            response = await asyncio.shield(task)
            self._owned_responses.add(response)
            return response
        except BaseException:
            if task.done() and not task.cancelled():
                with contextlib.suppress(BaseException):
                    response = task.result()
                    self._owned_responses.add(response)
                    await response.aclose()
                    self._owned_responses.discard(response)
            raise
        finally:
            self._owned_tasks.discard(task)
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    @staticmethod
    async def _bounded_response(response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        try:
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_NDJSON_RECORD_BYTES:
                    raise ExecutionClientError("execution response exceeds 1 MiB")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            await response.aclose()

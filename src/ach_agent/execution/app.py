# SPDX-License-Identifier: Apache-2.0
"""HTTP API for the native execution mini-harness.

The controller stream is the ownership boundary.  All operation requests carry its
controller id, while invocation output is an independent bounded NDJSON response.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.types import Send

from ach_agent.engine.lifecycle import NativeLaunchFailed
from ach_agent.engine.workspace import WorkspaceHandoffFailed, WorkspaceHookFailed
from ach_agent.execution.service import (
    MAX_NDJSON_RECORD_BYTES,
    ExecutionService,
    OutputLimitExceeded,
)
from ach_agent.execution.wire import (
    AcquireRequest,
    ControllerHello,
    ControllerOpenRequest,
    ControllerStopRequest,
    ExecutionEvent,
    ReleaseRequest,
    SessionImportRequest,
    SessionOperation,
    SessionReadyRequest,
    TurnRequest,
    WorkspaceCancelRequest,
    WorkspaceCleanupAckRequest,
    WorkspaceHandoffRequest,
    WorkspaceOperationFailure,
    WorkspacePrepareRequest,
)

EXECUTION_API_VERSION = 1
MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024
WRITE_TIMEOUT_SECONDS = 30.0


class _BoundedStreamingResponse(StreamingResponse):
    """Apply a finite deadline to each network write."""

    def __init__(
        self,
        content: Any,
        *,
        on_close: Callable[[], Awaitable[None]] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(content, **kwargs)
        self._on_close = on_close

    async def stream_response(self, send: Send) -> None:
        try:
            with anyio.fail_after(WRITE_TIMEOUT_SECONDS):
                await send(
                    {
                        "type": "http.response.start",
                        "status": self.status_code,
                        "headers": self.raw_headers,
                    }
                )
            async for chunk in self.body_iterator:
                with anyio.fail_after(WRITE_TIMEOUT_SECONDS):
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            with anyio.fail_after(WRITE_TIMEOUT_SECONDS):
                await send({"type": "http.response.body", "body": b""})
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                if self._on_close is not None:
                    with anyio.CancelScope(shield=True):
                        with contextlib.suppress(BaseException):
                            await self._on_close()


def _json_line(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


async def _request_json(request: Request) -> Any:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_REQUEST_BODY_BYTES:
                raise _BodyTooLarge
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_REQUEST_BODY_BYTES:
            raise _BodyTooLarge
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _InvalidBody from exc


class _BodyTooLarge(Exception):
    pass


class _InvalidBody(Exception):
    pass


def _invalid(message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=422)


def _error_response(exc: Exception, *, workspace_confirmed: bool = True) -> JSONResponse:
    if isinstance(exc, (WorkspaceHookFailed, WorkspaceHandoffFailed)):
        return JSONResponse(
            WorkspaceOperationFailure(message=str(exc), confirmed=workspace_confirmed).model_dump(
                mode="json"
            ),
            status_code=422 if workspace_confirmed else 503,
        )
    if isinstance(exc, NativeLaunchFailed):
        return JSONResponse({"type": "LaunchFailed", "message": str(exc)}, status_code=502)
    if isinstance(exc, OutputLimitExceeded):
        return JSONResponse({"type": "OutputLimitExceeded", "message": str(exc)}, status_code=507)
    if isinstance(exc, ValueError):
        status = 409 if ("controller" in str(exc) or "turn" in str(exc)) else 404
        return JSONResponse({"detail": str(exc)}, status_code=status)
    if isinstance(exc, RuntimeError):
        if "already has a controller" in str(exc):
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"detail": str(exc)}, status_code=503)
    return JSONResponse({"detail": str(exc)}, status_code=500)


def create_execution_app(service: ExecutionService) -> FastAPI:
    """Create the versioned mini-harness execution API."""

    app = FastAPI(title="ach-agent-execution")
    app.state.service = service
    service.controller_required = True

    def service_error(exc: Exception) -> JSONResponse:
        if service.shutdown_requested:
            app.state.shutdown_requested = True
        return _error_response(exc, workspace_confirmed=not service._unhealthy)

    @app.post("/execution/v1/controller", response_model=None)
    async def controller(request: Request) -> StreamingResponse | JSONResponse:
        try:
            hello = ControllerOpenRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        if hello.version != EXECUTION_API_VERSION:
            return JSONResponse({"detail": "unsupported execution API version"}, status_code=409)
        if hello.instance_id != service.instance_id:
            return JSONResponse({"detail": "obsolete execution instance"}, status_code=409)
        try:
            await service.claim_controller(hello.controller_id, config=hello.config)
        except Exception as exc:
            return service_error(exc)

        response_hello = ControllerHello(
            version=EXECUTION_API_VERSION,
            instance_id=service.instance_id,
            controller_id=hello.controller_id,
        )

        async def held() -> AsyncIterator[bytes]:
            try:
                yield _json_line(response_hello.model_dump(mode="json"))
                events = service.controller_events()
                while (
                    service.controller_id == hello.controller_id
                    and not service.shutdown_requested
                    and not await request.is_disconnected()
                ):
                    if events is None:
                        await asyncio.sleep(0.05)
                        continue
                    try:
                        event = await asyncio.wait_for(events.get(), timeout=0.05)
                    except TimeoutError:
                        continue
                    yield _json_line(event.model_dump(mode="json"))
            except asyncio.CancelledError:
                raise
            finally:
                with anyio.CancelScope(shield=True):
                    with contextlib.suppress(BaseException):
                        await service.release_controller(hello.controller_id)
                if service.shutdown_requested:
                    app.state.shutdown_requested = True

        return _BoundedStreamingResponse(
            held(),
            media_type="application/x-ndjson",
            on_close=lambda: service.release_controller(hello.controller_id),
        )

    @app.post("/execution/v1/controller/stop")
    async def controller_stop(request: Request) -> JSONResponse:
        try:
            body = ControllerStopRequest.model_validate(await _request_json(request))
            await service.graceful_stop_controller(body.controller_id)
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "stopped"})

    @app.post("/execution/v1/acquire")
    async def acquire(request: Request) -> JSONResponse:
        try:
            body = AcquireRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            handle = await service.acquire(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse(handle.model_dump(mode="json"))

    @app.post("/execution/v1/workspace/prepare")
    async def workspace_prepare(request: Request) -> JSONResponse:
        try:
            body = WorkspacePrepareRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            result = await service.prepare_workspace(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse(result)

    @app.post("/execution/v1/workspace/handoff")
    async def workspace_handoff(request: Request) -> JSONResponse:
        try:
            body = WorkspaceHandoffRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            result = await service.handoff_workspace(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse(result)

    @app.post("/execution/v1/workspace/cleanup-ack")
    async def workspace_cleanup_ack(request: Request) -> JSONResponse:
        try:
            body = WorkspaceCleanupAckRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            await service.ack_workspace_cleanup(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "ok"})

    @app.post("/execution/v1/turn", response_model=None)
    async def turn(request: Request) -> StreamingResponse | JSONResponse:
        try:
            body = TurnRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            service.validate_turn(body)
        except Exception as exc:
            return service_error(exc)

        stream_finished = False

        async def cancel_invocation() -> None:
            with anyio.CancelScope(shield=True):
                with contextlib.suppress(BaseException):
                    await service.cancel(
                        body.controller_id,
                        body.invocation_id,
                        execution_id=body.execution_id,
                    )

        async def output() -> AsyncIterator[bytes]:
            nonlocal stream_finished
            cancelled = False
            finished = False

            try:
                async for event in service.turn(body):
                    # Serialization occurs before handing data to the ASGI server, so a
                    # non-JSON native diagnostic can never escape as an unbounded object.
                    record = _json_line(event.model_dump(mode="json"))
                    if len(record) > MAX_NDJSON_RECORD_BYTES:
                        raise OutputLimitExceeded("NDJSON record exceeds 1 MiB")
                    yield record
                finished = True
                stream_finished = True
            except asyncio.CancelledError:
                cancelled = True
                raise
            except OutputLimitExceeded as exc:
                await cancel_invocation()
                finished = True
                stream_finished = True
                yield _json_line(
                    ExecutionEvent(
                        kind="error",
                        execution_id=body.execution_id,
                        invocation_id=body.invocation_id,
                        turn_id=body.turn_id,
                        payload={"type": type(exc).__name__, "message": str(exc)},
                    ).model_dump(mode="json")
                )
                return
            except Exception as exc:
                await cancel_invocation()
                finished = True
                stream_finished = True
                yield _json_line(
                    ExecutionEvent(
                        kind="error",
                        execution_id=body.execution_id,
                        invocation_id=body.invocation_id,
                        turn_id=body.turn_id,
                        payload={"type": type(exc).__name__, "message": str(exc)},
                    ).model_dump(mode="json")
                )
                return
            finally:
                if cancelled or not finished:
                    await cancel_invocation()

        async def cancel_if_incomplete() -> None:
            if not stream_finished:
                await cancel_invocation()

        return _BoundedStreamingResponse(
            output(), media_type="application/x-ndjson", on_close=cancel_if_incomplete
        )

    @app.post("/execution/v1/session-op")
    async def session_op(request: Request) -> JSONResponse:
        try:
            body = SessionOperation.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            await service.session_op(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "ok"})

    @app.post("/execution/v1/session-ready")
    async def session_ready(request: Request) -> JSONResponse:
        try:
            body = SessionReadyRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            await service.session_ready(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "ok"})

    @app.post("/execution/v1/session-import")
    async def session_import(request: Request) -> JSONResponse:
        try:
            body = SessionImportRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            imported = await service.import_legacy_sessions(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "ok", "imported": imported})

    @app.post("/execution/v1/release")
    async def release(request: Request) -> JSONResponse:
        try:
            body = ReleaseRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            await service.release(body)
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "ok"})

    @app.post("/execution/v1/cancel")
    async def cancel(request: Request) -> JSONResponse:
        try:
            body = WorkspaceCancelRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, KeyError, TypeError, ValueError) as exc:
            return _invalid(str(exc))
        try:
            await service.cancel(
                body.controller_id,
                body.invocation_id,
                execution_id=body.execution_id,
            )
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "ok"})

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        status = "unhealthy" if service._unhealthy else "ok"
        return JSONResponse({"status": status}, status_code=503 if service._unhealthy else 200)

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        status = "unhealthy" if service._unhealthy else "ready"
        return JSONResponse({"status": status}, status_code=503 if service._unhealthy else 200)

    @app.get("/execution/v1/health")
    async def execution_health() -> JSONResponse:
        return JSONResponse(
            {
                "status": "unhealthy" if service._unhealthy else "ok",
                "version": EXECUTION_API_VERSION,
                "instance_id": service.instance_id,
            },
            status_code=503 if service._unhealthy else 200,
        )

    @app.get("/metrics")
    async def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app

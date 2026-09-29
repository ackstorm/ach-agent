# SPDX-License-Identifier: Apache-2.0
"""HTTP API for the native execution mini-harness.

The controller stream is the ownership boundary.  All operation requests carry its
controller id, while invocation output is an independent bounded NDJSON response.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import tarfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from starlette.types import Send

from ach_agent.engine.lifecycle import NativeLaunchFailed
from ach_agent.execution.service import (
    MAX_NDJSON_RECORD_BYTES,
    ExecutionService,
    OutputLimitExceeded,
    SessionHookFailed,
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
    WorkspacePrepareRequest,
    WorkspaceSessionStartRequest,
)
from ach_agent.sandbox.archive import ArchiveTooLarge, extract, write_capped
from ach_agent.sandbox.tokens import verify_engine_bearer

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
    if isinstance(exc, NativeLaunchFailed):
        return JSONResponse({"type": "LaunchFailed", "message": str(exc)}, status_code=502)
    if isinstance(exc, OutputLimitExceeded):
        return JSONResponse({"type": "OutputLimitExceeded", "message": str(exc)}, status_code=507)
    if isinstance(exc, ArchiveTooLarge):
        return JSONResponse({"detail": str(exc)}, status_code=413)
    if isinstance(exc, (SessionHookFailed, tarfile.TarError)):
        # Workspace rejections: the service answered and is healthy. A 4xx keeps the
        # client from treating a bad hook or a bad handoff archive as controller loss (F5).
        return JSONResponse({"detail": str(exc)}, status_code=422)
    if isinstance(exc, ValueError):
        status = 409 if ("controller" in str(exc) or "turn" in str(exc)) else 404
        return JSONResponse({"detail": str(exc)}, status_code=status)
    if isinstance(exc, RuntimeError):
        if "already has a controller" in str(exc) or str(exc) == "session closing":
            return JSONResponse({"detail": str(exc)}, status_code=409)
        return JSONResponse({"detail": str(exc)}, status_code=503)
    return JSONResponse({"detail": str(exc)}, status_code=500)


def _execution_probe(service: ExecutionService, *, readiness: bool) -> JSONResponse:
    if service._unhealthy or not service.initialized:
        return JSONResponse(
            {"status": "unhealthy" if service._unhealthy else "initializing"},
            status_code=503,
        )
    return JSONResponse({"status": "ready" if readiness else "ok"}, status_code=200)


def _register_execution_health_routes(app: FastAPI, service: ExecutionService) -> None:
    """Register identical probe routes on the private and public engine apps."""

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        return _execution_probe(service, readiness=False)

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        return _execution_probe(service, readiness=True)


class _SandboxAuth:
    """P7: only the harness holding K can drive this mini-harness; first valid claim pins."""

    def __init__(self, app: Any, verify_key: str, pin: dict[str, str | None]) -> None:
        self.app = app
        self.verify_key = verify_key
        self.pin = pin

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] != "/execution/v1/health":
            header = dict(scope["headers"]).get(b"authorization", b"").decode()
            bearer = header.removeprefix("Bearer ")
            claim = verify_engine_bearer(self.verify_key, bearer) if bearer != header else None
            pinned = self.pin["claim"]
            if claim is None or (pinned is not None and claim != pinned):
                await JSONResponse({"detail": "unauthorized"}, status_code=401)(
                    scope, receive, send
                )
                return
            self.pin["claim"] = claim
        await self.app(scope, receive, send)


def create_execution_app(service: ExecutionService, *, verify_key: str | None = None) -> FastAPI:
    """Create the versioned mini-harness execution API.

    With ``verify_key`` (sandbox mode) every route but health needs an Ed25519 engine bearer,
    and the archive-import/close routes exist.
    """

    app = FastAPI(title="ach-agent-execution")
    pin: dict[str, str | None] = {"claim": None}
    if verify_key:
        app.add_middleware(_SandboxAuth, verify_key=verify_key, pin=pin)
    app.state.service = service
    service.controller_required = True

    @app.on_event("shutdown")
    async def close_service() -> None:
        await service.close()

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
                while (
                    service.controller_id == hello.controller_id
                    and not service.shutdown_requested
                    and not service.closing
                    and not await request.is_disconnected()
                ):
                    await asyncio.sleep(0.05)
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

    @app.put("/execution/v1/workspace/handoff")
    async def workspace_handoff(request: Request) -> JSONResponse:
        controller_id = request.query_params.get("controller_id", "")
        invocation_id = request.query_params.get("invocation_id", "")
        if not controller_id or not invocation_id:
            return _invalid("controller_id and invocation_id query params are required")
        try:
            await service.import_handoff(controller_id, invocation_id, request.stream())
        except Exception as exc:
            return service_error(exc)
        return JSONResponse({"status": "ok"})

    @app.post("/execution/v1/workspace/session-start")
    async def workspace_session_start(request: Request) -> JSONResponse:
        try:
            body = WorkspaceSessionStartRequest.model_validate(await _request_json(request))
        except _BodyTooLarge:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        except (_InvalidBody, ValidationError) as exc:
            return _invalid(str(exc))
        try:
            await service.session_start(body)
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

    _register_execution_health_routes(app, service)

    @app.get("/execution/v1/health")
    async def execution_health() -> JSONResponse:
        body: dict[str, Any] = {
            "status": "unhealthy" if service._unhealthy else "ok",
            "version": EXECUTION_API_VERSION,
            "instance_id": service.instance_id,
        }
        if verify_key:
            body.update(configured=service.configured, closing=service.closing, claim=pin["claim"])
        return JSONResponse(body, status_code=503 if service._unhealthy else 200)

    if verify_key:
        _register_sandbox_routes(app, service)

    @app.get("/metrics")
    async def metrics() -> Response:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def _register_sandbox_routes(app: FastAPI, service: ExecutionService) -> None:
    max_bytes = int(os.environ.get("ACH_SANDBOX_MAX_ARCHIVE_BYTES", str(2 * 1024**3)))

    async def receive(
        request: Request, dest: Path, *, filter: Literal["data", "tar"] = "data"  # noqa: A002
    ) -> JSONResponse | None:
        if service.configured:
            return JSONResponse({"detail": "sandbox already configured"}, status_code=409)
        archive = Path(f"/tmp/ach-sandbox-in-{uuid.uuid4().hex}.tar.gz")
        try:
            await write_capped(request.stream(), archive, max_bytes=max_bytes)
            await asyncio.to_thread(
                extract, archive, dest, max_expanded_bytes=8 * max_bytes, filter=filter
            )
        except Exception as exc:
            if dest.name.startswith(".ach-harness-shared-files-"):
                shutil.rmtree(dest, ignore_errors=True)
            return _error_response(exc)
        finally:
            archive.unlink(missing_ok=True)
        return None

    @app.put("/execution/v1/sandbox/archive/home")
    async def archive_home(request: Request) -> JSONResponse:
        home = Path(os.environ.get("ACH_SANDBOX_HOME", "/home/agent"))
        # The agent's own files, restored into its own sandbox: keep absolute symlinks.
        return await receive(request, home, filter="tar") or JSONResponse({"status": "ok"})

    @app.put("/execution/v1/sandbox/archive/hydration")
    async def archive_hydration(request: Request) -> JSONResponse:
        # Must satisfy engine.context._safe_batch (prefix + transfer-root parent name).
        dest = Path(f"/tmp/ach-agent-transfer/.ach-harness-shared-files-{uuid.uuid4().hex}")
        return await receive(request, dest) or JSONResponse({"path": str(dest)})

    @app.post("/execution/v1/sandbox/close")
    async def close() -> JSONResponse:
        if not service.configured:
            return JSONResponse({"detail": "sandbox not configured"}, status_code=409)
        if service.busy:
            return JSONResponse({"detail": "invocation active"}, status_code=409)
        await service.stop_and_push()
        return JSONResponse({"status": "ok"})


def create_execution_health_app(service: ExecutionService) -> FastAPI:
    """Build the engine's public probe surface without exposing execution routes."""
    app = FastAPI(
        title="ach-agent-execution-health",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    _register_execution_health_routes(app, service)

    return app

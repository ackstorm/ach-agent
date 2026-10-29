# SPDX-License-Identifier: MIT
"""Mini-harness HTTP app: `/healthz` + the Started/status/list/cancel surface
(contract §3). Reuses old-ach-agent's finite-JSON/body-cap/error-response discipline
(`execution/app.py:_request_json`, `_json_line`, `_error_response`) — there is no
controller-stream ownership boundary here, so the public/private route split and the
turn-stream lifetime coupling it reused are not ported.
"""

from __future__ import annotations

import json
import math
import tarfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError, field_validator

from ach_runtime import __version__
from ach_runtime.config.public import ExecutionBootstrap, PublicConfig
from ach_runtime.engine.config import to_fixture
from ach_runtime.execution.service import ConflictingStart, ExecutionService, WorkspaceMismatch
from ach_runtime.execution.sessions import SessionHookFailed
from ach_runtime.execution.wire import CancelRequest, StartRequest
from ach_runtime.storage.archive import ArchiveTooLarge

MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024


class _ConfigureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    public: PublicConfig
    catalog: dict[str, JsonValue]

    @field_validator("catalog")
    @classmethod
    def finite_catalog(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        def check(item: Any) -> None:
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("catalog numbers must be finite")
            if isinstance(item, dict):
                for child in item.values():
                    check(child)
            elif isinstance(item, list):
                for child in item:
                    check(child)
        check(value)
        return value


class _BodyTooLarge(Exception):
    pass


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
    return json.loads(b"".join(chunks))


def _invalid(message: str) -> JSONResponse:
    return JSONResponse({"detail": message}, status_code=422)


def _parse_aware_deadline(raw: str) -> datetime:
    """Strictly reject a naive or malformed `deadline_at` query value before any restore
    mutation (I5) -- an aware ISO-8601 timestamp only."""
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"deadline_at is not a valid ISO-8601 timestamp: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline_at must be timezone-aware")
    return value


def _error_response(exc: Exception) -> JSONResponse:
    if isinstance(exc, ConflictingStart):
        return JSONResponse({"type": "ConflictingStart", "message": str(exc)}, status_code=409)
    if isinstance(exc, WorkspaceMismatch):
        return JSONResponse({"type": "WorkspaceMismatch", "message": str(exc)}, status_code=422)
    if isinstance(exc, (ArchiveTooLarge, tarfile.TarError)):
        # Deterministic archive-policy rejection: retrying the same tree cannot succeed.
        return JSONResponse({"type": "ArchiveRejected", "message": str(exc)}, status_code=422)
    if isinstance(exc, SessionHookFailed):
        return JSONResponse({"type": "SessionHookFailed", "message": str(exc)}, status_code=502)
    if getattr(exc, "ambiguous", False):
        return JSONResponse({"type": "Unknown", "message": str(exc)}, status_code=503)
    return JSONResponse({"type": "Failed", "message": str(exc)}, status_code=502)


def create_app(
    service: ExecutionService, *, bootstrap: ExecutionBootstrap | None = None
) -> FastAPI:
    app = FastAPI()
    app.state.service = service
    app.state.ready = False

    @app.on_event("shutdown")
    async def close_service() -> None:
        await service.close()

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        # Cold mini health can succeed without a native process (contract §4).
        return JSONResponse({"status": "ok", "release": __version__})

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = bool(getattr(service, "ready", False))
        return JSONResponse({"status": "ready" if ready else "not-ready"}, status_code=200 if ready else 503)

    @app.post("/configure")
    async def configure(request: Request) -> JSONResponse:
        try:
            payload = await _request_json(request)
        except (_BodyTooLarge, ValueError, UnicodeDecodeError) as exc:
            return _invalid(str(exc) or "invalid request body")
        try:
            parsed = _ConfigureRequest.model_validate(payload)
            _validate_configuration(parsed, bootstrap)
            # Validate the exact selected model translation before atomically adopting it.
            to_fixture(parsed.public, catalog=parsed.catalog, lookup_only_prompt=True)
        except (ValidationError, ValueError, TypeError) as exc:
            return _invalid(str(exc))
        service.configure(parsed.public, catalog=parsed.catalog)
        app.state.ready = True
        return JSONResponse({"configVersion": parsed.public.config_version})

    @app.post("/workspace/hooks/{phase}")
    async def workspace_hook(phase: str, request: Request) -> JSONResponse:
        if phase not in ("start", "stop"):
            return JSONResponse({"detail": f"unknown workspace hook phase {phase!r}"}, status_code=404)
        if not service.ready:
            return JSONResponse({"detail": "execution service is not configured"}, status_code=409)
        restored = False
        if phase == "start":
            try:
                payload = await _request_json(request)
            except (_BodyTooLarge, ValueError, UnicodeDecodeError) as exc:
                return _invalid(str(exc) or "invalid request body")
            restored = isinstance(payload, dict) and payload.get("restored") is True
        try:
            ran = await service.run_workspace_hook(phase, restored=restored)
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)
        return JSONResponse({"status": "ok" if ran else "skipped"})

    @app.post("/start")
    async def start(request: Request) -> JSONResponse:
        try:
            payload = await _request_json(request)
        except (_BodyTooLarge, ValueError, UnicodeDecodeError) as exc:
            return _invalid(str(exc) or "invalid request body")
        try:
            parsed = StartRequest.model_validate(payload)
        except ValidationError as exc:
            return _invalid(str(exc))
        try:
            started = await service.start(parsed)
        except Exception as exc:  # noqa: BLE001 - mapped below, never raised past the app
            return _error_response(exc)
        return JSONResponse(started.model_dump(by_alias=True, mode="json"), status_code=202)

    @app.get("/status/{invocation_id}")
    async def status(invocation_id: str) -> JSONResponse:
        try:
            run_status = await service.status(invocation_id)
        except KeyError:
            return JSONResponse({"detail": "unknown invocation_id"}, status_code=404)
        return JSONResponse(run_status.model_dump(by_alias=True, mode="json"))

    @app.get("/runs")
    async def runs() -> JSONResponse:
        all_runs = await service.list_runs()
        return JSONResponse([r.model_dump(by_alias=True, mode="json") for r in all_runs])

    @app.post("/cancel")
    async def cancel(request: Request) -> JSONResponse:
        try:
            payload = await _request_json(request)
        except (_BodyTooLarge, ValueError, UnicodeDecodeError) as exc:
            return _invalid(str(exc) or "invalid request body")
        try:
            parsed = CancelRequest.model_validate(payload)
        except ValidationError as exc:
            return _invalid(str(exc))
        try:
            await service.cancel(parsed.invocation_id, parsed.reason)
        except KeyError:
            return JSONResponse({"detail": "unknown invocation_id"}, status_code=404)
        return JSONResponse({"status": "cancelled"})

    def archive_response(path: Path) -> FileResponse:
        return FileResponse(
            path,
            media_type="application/gzip",
            filename="archive.tar.gz",
            background=BackgroundTask(path.unlink, missing_ok=True),
        )

    @app.get("/sessions")
    async def sessions() -> JSONResponse:
        records = []
        for state in service._sessions.inventory():
            records.append(await service.session_info(state.session_ref))
        return JSONResponse(records)

    @app.get("/sessions/{session_ref}/info")
    async def session_info(session_ref: str) -> JSONResponse:
        try:
            return JSONResponse(await service.session_info(session_ref))
        except KeyError:
            return JSONResponse({"detail": "unknown session_ref"}, status_code=404)

    @app.get("/sessions/{session_ref}/archive")
    async def session_archive(session_ref: str) -> Any:
        try:
            from ach_runtime.execution.snapshots import export_session

            return archive_response(await export_session(service, session_ref))
        except KeyError:
            return JSONResponse({"detail": "unknown session_ref"}, status_code=404)
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)

    @app.post("/sessions/{session_ref}/retire")
    async def retire_session(session_ref: str) -> JSONResponse:
        try:
            await service.retire_session(session_ref)
            return JSONResponse({"status": "retired", "sessionRef": session_ref})
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)

    @app.put("/sessions/{session_ref}/archive")
    async def restore_session_archive(session_ref: str, request: Request) -> JSONResponse:
        try:
            from ach_runtime.execution.snapshots import import_session

            raw_deadline = request.query_params.get("deadline_at")
            deadline_at = None
            if raw_deadline is not None:
                deadline_at = _parse_aware_deadline(raw_deadline)
            await import_session(service, session_ref, request.stream(), deadline_at=deadline_at)
            return JSONResponse({"status": "restored", "sessionRef": session_ref})
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)

    @app.get("/workspace/archive")
    async def workspace_archive() -> Any:
        try:
            from ach_runtime.execution.snapshots import export_workspace

            return archive_response(await export_workspace(service))
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)

    @app.put("/workspace/archive")
    async def restore_workspace_archive(request: Request) -> JSONResponse:
        try:
            from ach_runtime.execution.snapshots import import_workspace

            await import_workspace(service, request.stream())
            return JSONResponse({"status": "restored"})
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)

    @app.put("/execution/v1/workspace/handoff")
    async def workspace_handoff(request: Request, destination: str = "handoff") -> JSONResponse:
        workspace_root = service._workspace_dir.resolve()
        path = (service._workspace_dir / destination).resolve()
        if path == workspace_root:
            return _invalid("handoff destination cannot be the Workspace root")
        native = getattr(service._engine, "_sessions", {})
        def cleanup_running(task: Any) -> bool:
            return task is not None and not task.done()

        blocked = any(
            not inv.terminal
            or inv.state == "unknown"
            or inv.reservation is not None
            or any(cleanup_running(getattr(inv, name, None)) for name in (
                "cancel_task", "engine_cancel_task", "owner_stop_task"
            ))
            for inv in service._invocations.values()
        )
        if getattr(service, "_workspace_frozen", False) or blocked:
            return JSONResponse({"detail": "handoff requires quiescent execution state"}, status_code=409)
        service._workspace_frozen = True
        try:
            from ach_runtime.execution.snapshots import import_handoff

            for session_ref, native_session in tuple(native.items()):
                if getattr(native_session, "alive", False):
                    if not await service._engine.suspend(session_ref):
                        return JSONResponse(
                            {"detail": "handoff could not confirm clean session suspension"},
                            status_code=409,
                        )
            await import_handoff(service, str(path), request.stream())
            return JSONResponse({"status": "imported"})
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)
        finally:
            service._workspace_frozen = False

    @app.put("/execution/v1/sandbox/archive/hydration")
    async def archive_hydration(request: Request, session_ref: str) -> JSONResponse:
        try:
            from ach_runtime.execution.snapshots import import_hydration

            await import_hydration(service, session_ref, request.stream())
            return JSONResponse({"status": "imported", "sessionRef": session_ref})
        except KeyError:
            return JSONResponse({"detail": "unknown session_ref"}, status_code=404)
        except Exception as exc:  # noqa: BLE001 - mapped to the private API error contract
            return _error_response(exc)

    return app


def _validate_configuration(
    value: _ConfigureRequest, bootstrap: ExecutionBootstrap | None
) -> None:
    public, catalog = value.public, value.catalog
    # I7: `memoryToolsSpec` is the one additional safe static string key this shape
    # allows -- the active memory tools instructions, composed once by `to_fixture`.
    if not set(catalog) <= {"providers", "mcp", "memoryToolsSpec"} or not {"providers", "mcp"} <= set(catalog):
        raise ValueError("catalog must match the public_catalog providers/mcp shape")
    if "memoryToolsSpec" in catalog and not isinstance(catalog["memoryToolsSpec"], str):
        raise ValueError("catalog memoryToolsSpec must be a string")
    if bootstrap is not None:
        if public.agent != bootstrap.agent:
            raise ValueError("public agent does not match bootstrap agent")
        if public.infrastructure.facade_endpoint != bootstrap.facade_endpoint:
            raise ValueError("facade endpoint does not match bootstrap")
    providers = catalog.get("providers")
    mcp = catalog.get("mcp")
    if not isinstance(providers, dict) or not isinstance(mcp, dict):
        raise ValueError("catalog must contain providers and mcp objects")
    if set(providers) != {public.model.type}:
        raise ValueError("catalog must contain only the selected provider")
    provider = providers.get(public.model.type)
    if not isinstance(provider, dict) or set(provider) != {"baseURL", "apiKey", "models"}:
        raise ValueError("catalog provider does not match public_catalog shape")
    if not isinstance(provider.get("models"), dict) or set(provider["models"]) != {public.model.name}:
        raise ValueError("selected provider is absent from catalog")
    if not isinstance(provider.get("baseURL"), str) or provider.get("apiKey") != "local-proxy":
        raise ValueError("catalog provider must use a URL and local-proxy key")
    _require_http_url(provider["baseURL"], "provider baseURL")
    model_metadata = provider["models"][public.model.name]
    allowed_metadata = {"limit", "cost", "tool_call", "reasoning", "modalities", "attachment"}
    if not isinstance(model_metadata, dict) or not set(model_metadata).issubset(allowed_metadata):
        raise ValueError("catalog model metadata contains fields outside public_catalog")
    for name, entry in mcp.items():
        if not isinstance(name, str) or not isinstance(entry, dict) or set(entry) != {"url", "headers"}:
            raise ValueError("catalog MCP entries must be objects")
        if not isinstance(entry.get("url"), str) or entry.get("headers") != {}:
            raise ValueError("catalog MCP entries require a URL and empty headers")
        _require_http_url(entry["url"], f"MCP URL {name}")


def _require_http_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} must be an absolute HTTP(S) URL")

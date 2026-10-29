# SPDX-License-Identifier: MIT
"""Fixed-identity auth wrapper around the mini-harness execution app (owned slice of
Task 2 -- workspace-auth-dispatch). Adapts old-ach-agent's `_SandboxAuth` ASGI middleware
(execution/app.py:_SandboxAuth) to one pinned Workspace name plus an X-ACH-Instance-Id
binding for private-request/response correlation. `/healthz` and `/readyz` remain public
probes; every other HTTP route needs a signed bearer for the exact `workspace_name` and
a matching instance ID before the request reaches the wrapped app.
"""

from __future__ import annotations

import base64
import re
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.execution.app import create_app
from ach_runtime.execution.service import ExecutionService
from ach_runtime.harness.tokens import verify_engine_bearer

_INSTANCE_ID_HEADER = b"x-ach-instance-id"
_VERIFY_KEY_ALPHABET = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_verify_key(verify_key: str) -> None:
    if not _VERIFY_KEY_ALPHABET.match(verify_key):
        raise ValueError("malformed verify_key: not URL-safe base64url text")
    padded = verify_key + "=" * (-len(verify_key) % 4)
    try:
        Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(padded))
    except ValueError as exc:
        raise ValueError(f"malformed verify_key: {exc}") from exc


class _WorkspaceAuth:
    """Private HTTP routes require a bearer for `workspace_name` and its instance ID."""

    def __init__(self, app: Any, *, verify_key: str, workspace_name: str, instance_id: str) -> None:
        self.app = app
        self.verify_key = verify_key
        self.workspace_name = workspace_name
        self.instance_id = instance_id

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        async def send_with_instance_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [
                    *message.get("headers", []),
                    (_INSTANCE_ID_HEADER, self.instance_id.encode()),
                ]
            await send(message)

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if scope["path"] in {"/healthz", "/readyz"}:
            await self.app(scope, receive, send_with_instance_id)
            return
        headers = dict(scope["headers"])
        # latin-1 never fails, so a non-UTF-8 header is a 401 rather than a 500.
        header = headers.get(b"authorization", b"").decode("latin-1")
        bearer = header.removeprefix("Bearer ")
        claim = verify_engine_bearer(self.verify_key, bearer) if bearer != header else None
        instance_id = headers.get(_INSTANCE_ID_HEADER, b"").decode("latin-1")
        if claim != self.workspace_name or instance_id != self.instance_id:
            await JSONResponse({"detail": "unauthorized"}, status_code=401)(scope, receive, send_with_instance_id)
            return

        await self.app(scope, receive, send_with_instance_id)


def create_authenticated_app(
    service: ExecutionService, *, verify_key: str, workspace_name: str, instance_id: str,
    bootstrap: ExecutionBootstrap | None = None,
) -> FastAPI:
    if not verify_key:
        raise ValueError("verify_key is required")
    if not workspace_name:
        raise ValueError("workspace_name is required")
    if not instance_id:
        raise ValueError("instance_id is required")
    _validate_verify_key(verify_key)
    app = create_app(service, bootstrap=bootstrap)
    app.add_middleware(
        _WorkspaceAuth, verify_key=verify_key, workspace_name=workspace_name, instance_id=instance_id
    )
    return app

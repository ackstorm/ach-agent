# SPDX-License-Identifier: MIT
"""HTTP ingress and facade listener applications for the Harness control role."""

from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app
from starlette.responses import JSONResponse, Response

from ach_runtime.channels.auth import verify_webhook_auth
from ach_runtime.channels.a2a import A2AAgentExecutorBridge, build_a2a_app, make_a2a_agent_card
from ach_runtime.channels.gitlab import MalformedGitlabEventError
from ach_runtime.channels.routing import RoutingError, route
from ach_runtime.channels.webhook import normalize_webhook
from ach_runtime.channels.metrics import CHANNEL_INBOUND
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.harness.admission import AdmissionClosedError, AdmissionQueueFullError
from ach_runtime.harness.control import Control, create_facade_app
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.observability import IdentityRegistry
from ach_runtime.observability.metrics import initialise as initialise_metrics

MAX_BODY = 1024 * 1024


def _retrieve_outcome(future: asyncio.Future) -> None:
    """Admission retains outcomes/alarms; HTTP callers no longer await them."""
    if not future.cancelled():
        future.exception()


def create_app(control: Control) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ready = False
    app.state.a2a_bridges = {}

    def serving() -> bool:
        ach_key = getattr(control, "ach_key", None)
        return app.state.ready and not control._stopping and (ach_key is None or ach_key.ready())

    for configured in control.config.channels:
        if configured.type != "a2a":
            continue
        bridge = A2AAgentExecutorBridge(
            control,
            configured,
            ready=serving,
        )
        app.state.a2a_bridges[configured.name] = bridge
        app.mount(
            f"/a2a/{configured.name}",
            build_a2a_app(make_a2a_agent_card(control.config.agent.name, configured.name), bridge),
        )

    @app.get("/healthz")
    async def healthz() -> Response:
        return Response(status_code=200)

    @app.get("/readyz")
    async def readyz() -> Response:
        return Response(status_code=200 if serving() else 503)

    @app.post("/channels/{channel}/events")
    async def webhook(channel: str, request: Request) -> Response:
        configured = next((item for item in control.config.channels if item.name == channel), None)
        if configured is None:
            return JSONResponse({"detail": "unknown channel"}, status_code=404)
        if not serving():
            return JSONResponse({"detail": "control is not ready"}, status_code=503)
        if configured.type not in {"webhook", "webhook-script"} or configured.source not in {"generic", "gitlab", "github"} or configured.webhook is None:
            return JSONResponse({"detail": "adapter is unsupported by this control integration"}, status_code=501)
        oversized = False
        try:
            body = bytearray()
            async for chunk in request.stream():
                if not oversized:
                    body.extend(chunk)
                    if len(body) > MAX_BODY:
                        oversized = True
                        body.clear()
        except Exception:
            return JSONResponse({"detail": "request body could not be read"}, status_code=400)
        if oversized:
            return JSONResponse({"detail": "request body exceeds 1 MiB"}, status_code=413)
        raw = bytes(body)
        headers = {name.lower(): value for name, value in request.headers.items()}
        if not verify_webhook_auth(configured.webhook.auth, headers, raw):
            return JSONResponse({"detail": "webhook authentication failed"}, status_code=401)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            event = normalize_webhook(payload, dict(request.headers), configured)
            if event is None:
                return Response(status_code=200)
            routed = None if configured.type == "webhook-script" else route(event, configured)
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError, json.JSONDecodeError,
                MalformedGitlabEventError, RoutingError):
            return JSONResponse({"detail": "invalid webhook event"}, status_code=422)
        try:
            CHANNEL_INBOUND.labels(channel=configured.name, type=configured.type).inc()
            if configured.type == "webhook-script":
                ticket = control.admission.admit_script(event)
            else:
                assert routed is not None
                ticket = control.admission.admit(routed)
        except (AdmissionQueueFullError, AdmissionClosedError):
            return JSONResponse({"detail": "invocation could not be admitted"}, status_code=503)
        except Exception:
            return JSONResponse({"detail": "invocation could not be admitted"}, status_code=503)
        # Both signals remain owned by Admission, even after the HTTP response or
        # caller disconnect. Retrieve failures without clearing or cancelling them.
        ticket.started.add_done_callback(_retrieve_outcome)
        ticket.completed.add_done_callback(_retrieve_outcome)
        task_id = uuid.uuid4().hex
        return JSONResponse(
            {"status": "accepted", "task_id": task_id},
            status_code=202,
            headers={"X-ACH-Task-Id": task_id},
        )

    initialise_metrics(channel.name for channel in control.config.channels)
    app.mount(
        "/metrics",
        make_asgi_app(registry=IdentityRegistry(identity=ProcessIdentity(
            agent=control.config.agent.name, environment=control.config.identity_environment))),
    )

    return app


__all__ = ["create_app", "create_facade_app"]

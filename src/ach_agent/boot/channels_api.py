# SPDX-License-Identifier: Apache-2.0
"""Authenticated HTTP routes between the channels and harness roles."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response
from prometheus_client import make_asgi_app

from ach_agent.boot.completions import CompletionRegistry
from ach_agent.boot.health import HealthState
from ach_agent.channels.envelopes import Admission, ChannelInputs, EventEnvelope, EventRef
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.signing import (
    NONCE_HEADER,
    REQUEST_HEADER,
    RESPONSE_HEADER,
    TIMESTAMP_HEADER,
    AuthenticationError,
    NonceCache,
    response_mac,
    verify_request_mac,
)
from ach_agent.config.schema import ChannelSourceConfig

MAX_CHANNEL_BODY_BYTES = 1 * 1024 * 1024


def create_channels_app(
    registry: CompletionRegistry,
    key: bytes,
    *,
    agent: str = "default",
    channels: Iterable[str] | None = None,
    source_configs: Iterable[ChannelSourceConfig] | None = None,
    internal_auth: bool = True,
    nonce_cache: NonceCache | None = None,
    max_body_bytes: int = MAX_CHANNEL_BODY_BYTES,
) -> FastAPI:
    """Build the private harness API consumed by :class:`ChannelsClient`.

    The registry remains the admission authority. This app only authenticates,
    validates scope, serializes the existing registry result, and signs it.
    """
    configured_channels = set(channels or ())
    projected_sources = list(source_configs or ())
    replay = nonce_cache if nonce_cache is not None else NonceCache()
    app = FastAPI(title="ach-agent-harness-channels")
    state = HealthState(ready=True)
    app.extra["state"] = state
    app.extra.update({"agent": agent, "channels": configured_channels, "registry": registry})
    app.mount("/metrics", make_asgi_app())

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        return Response(
            content=json.dumps({"status": "ready" if state.ready else "draining"}),
            status_code=200 if state.ready else 503,
            media_type="application/json",
        )

    @app.get("/internal/v1/config", response_model=ChannelInputs)
    async def config() -> ChannelInputs:
        return ChannelInputs(agentName=agent, channels=projected_sources)

    async def authenticated_body(request: Request) -> tuple[bytes, str] | Response:
        if not internal_auth:
            return await request.body(), ""
        nonce = request.headers.get(NONCE_HEADER, "")
        timestamp_value = request.headers.get(TIMESTAMP_HEADER, "")
        signature = request.headers.get(REQUEST_HEADER, "")
        try:
            timestamp = int(timestamp_value)
        except (TypeError, ValueError):
            return signed_response(
                key, nonce, 401, {"kind": "error", "error": "invalid request authentication"}
            )
        body = await read_bounded_body(request, max_body_bytes)
        if isinstance(body, Response):
            return body
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        if (
            not nonce
            or not signature
            or not verify_request_mac(
                key, request.method, target, timestamp, nonce, body, signature
            )
        ):
            return signed_response(
                key, nonce, 401, {"kind": "error", "error": "invalid request authentication"}
            )
        try:
            replay.accept(nonce, timestamp)
        except AuthenticationError as exc:
            return signed_response(key, nonce, 401, {"kind": "error", "error": str(exc)})
        return body, nonce

    @app.post("/internal/v1/events")
    async def submit_event(request: Request) -> Response:
        authenticated = await authenticated_body(request)
        if isinstance(authenticated, Response):
            return authenticated
        raw_body, nonce = authenticated
        if state.draining:
            return signed_response(
                key,
                nonce,
                503,
                {"kind": "error", "error": "harness is draining", "retry": True},
            )
        try:
            payload = _object(raw_body)
            if payload.get("agent") != agent:
                return signed_response(key, nonce, 403, _rejection("scope mismatch: agent"))
            # EventEnvelope is strict, so validate the JSON representation rather than
            # rejecting its RFC3339 datetime string as a Python ``str``.
            envelope = EventEnvelope.model_validate_json(
                json.dumps(payload.get("event"), ensure_ascii=False, separators=(",", ":"))
            )
            if envelope.channel_name not in configured_channels:
                return signed_response(key, nonce, 403, _rejection("scope mismatch: channel"))
            event = _message_event(envelope)
            submission = await registry.submit(event)
        except Exception as exc:  # validation/admission errors are signed transport responses
            return signed_response(key, nonce, 400, {"kind": "error", "error": str(exc)})

        status = {
            Admission.ACCEPTED: 202,
            Admission.DUPLICATE: 200,
            Admission.FULL_QUEUE: 503,
        }[submission.admission]
        body = {
            "kind": "submission",
            "admission": submission.admission.value,
            "completion": submission.completion.model_dump(mode="json")
            if submission.completion is not None
            else None,
        }
        return signed_response(key, nonce, status, body)

    @app.post("/internal/v1/readyz")
    async def internal_readyz(request: Request) -> Response:
        authenticated = await authenticated_body(request)
        if isinstance(authenticated, Response):
            return authenticated
        raw_body, nonce = authenticated
        try:
            payload = _object(raw_body)
            if payload.get("agent") != agent:
                return signed_response(key, nonce, 403, _rejection("scope mismatch: agent"))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            return signed_response(key, nonce, 400, {"kind": "error", "error": str(exc)})
        status = 200 if state.ready else 503
        return signed_response(key, nonce, status, {"kind": "ready", "status": state.ready})

    @app.post("/internal/v1/results")
    async def get_result(request: Request) -> Response:
        authenticated = await authenticated_body(request)
        if isinstance(authenticated, Response):
            return authenticated
        raw_body, nonce = authenticated
        try:
            payload = _object(raw_body)
            if payload.get("agent") != agent:
                return signed_response(key, nonce, 403, _rejection("scope mismatch: agent"))
            ref = EventRef.model_validate(payload.get("ref"))
            if ref.agent != agent or ref.channel_name not in configured_channels:
                return signed_response(key, nonce, 403, _rejection("scope mismatch: result"))
            completion = registry.lookup(ref)
        except Exception as exc:
            return signed_response(key, nonce, 400, {"kind": "error", "error": str(exc)})
        return signed_response(
            key,
            nonce,
            200,
            {"kind": "completion", "completion": completion.model_dump(mode="json")},
        )

    return app


def _message_event(envelope: EventEnvelope) -> MessageEvent:
    return MessageEvent(
        idempotency_key=envelope.idempotency_key,
        session_key=envelope.session_key,
        channel_name=envelope.channel_name,
        secondary_idempotency_key=envelope.secondary_idempotency_key,
        payload=envelope.payload,
        delivery_context=envelope.delivery_context,
        source_trait=envelope.source_trait,
        received_at=envelope.received_at,
        task_id=envelope.task_id,
        free_form=envelope.free_form,
    )


async def read_bounded_body(request: Request, max_bytes: int) -> bytes | Response:
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > max_bytes:
        return Response(
            content=json.dumps({"kind": "error", "error": "request body too large"}),
            status_code=413,
            media_type="application/json",
        )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return Response(
                content=json.dumps({"kind": "error", "error": "request body too large"}),
                status_code=413,
                media_type="application/json",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def signed_response(key: bytes, nonce: str, status: int, payload: dict[str, Any]) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(
        content=body,
        status_code=status,
        media_type="application/json",
        headers={RESPONSE_HEADER: response_mac(key, nonce, status, body)},
    )


def _object(raw_body: bytes) -> dict[str, Any]:
    value = json.loads(raw_body)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _rejection(error: str) -> dict[str, Any]:
    return {"kind": "submission", "admission": "rejected", "completion": None, "error": error}

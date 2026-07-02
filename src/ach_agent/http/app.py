# SPDX-License-Identifier: Apache-2.0
"""FastAPI HTTP surface factory — inbound route + healthz/readyz/metrics (HTTP-01..04).

Locked decisions:
  - POST /channels/{channel_name}/events: raw body read before JSON parse; dispatches
    to WebhookChannelAdapter entry function; maps outcome to 401/200/202/503 (D-05).
  - GET /healthz: always 200 while process alive (HTTP-03).
  - GET /readyz: 200 iff lifespan has set _ready flag; 503 otherwise (HTTP-02, Pitfall 6).
    Engine warmup is NOT a gate (spec §8.5).
  - GET /metrics: Prometheus exposition via make_asgi_app() mounted sub-application (HTTP-04).
  - Webhook events are always async (202 accept-and-process); the route never holds the
    connection waiting for engine output (D-04).

RTR-06: NEVER import from hermes_agent.* here.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from ach_agent.channels.webhook import handle_webhook_request

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig

log = structlog.get_logger(__name__)

# Maximum inbound webhook body size (T-02-05 defense-in-depth DoS cap).
# GitLab MR webhooks are well under this limit in practice; this cap is a
# last-resort guard behind any ingress-level limit (nginx/ALB body cap).
MAX_WEBHOOK_BODY_BYTES: int = 1 * 1024 * 1024  # 1 MiB


def create_app(
    channels: Sequence[ChannelConfig],
    handler: MessageHandler,
    pool: Any = None,  # EnginePool — unused by the pre-admission gate (decoupled); kept for callers
    a2a_mounts: Sequence[tuple[str, Any]] | None = None,  # [(path, sub_app), ...] — A2A sub-apps
) -> FastAPI:
    """Create the FastAPI app with all HTTP surface endpoints.

    Args:
        channels:  List of channel configs (looked up by name on each request).
        handler:   MessageHandler (Router) for router.handle(event) calls.
        pool:      EnginePool instance. Acceptance is decoupled from engine readiness —
                   the pre-admission gate no longer reads this; kept for backward
                   compatibility with existing callers/tests.

    Returns:
        FastAPI application instance with lifespan, routes, and /metrics mount.
    """
    # Build a name→config lookup once at app creation
    channel_map: dict[str, ChannelConfig] = {ch.name: ch for ch in channels}

    # Mutable state — set in lifespan after channel wiring is complete.
    # draining: flipped True by drain handler (SIGTERM, Plan 03-03) — D-12 straggler gate.
    # Using a mutable container so the closure captures the reference, not the value.
    state: dict[str, Any] = {"ready": False, "draining": False}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        """FastAPI lifespan: set ready flag after wiring, clear on teardown.

        Pitfall 6: /readyz must not return 200 until after this lifespan block sets
        the flag. Engine warmup is NOT part of the ready gate (spec §8.5/HTTP-02).
        """
        # Wiring is complete — channels are registered and the route is active
        state["ready"] = True
        log.info("http: app ready — inbound route listening", channel_count=len(channel_map))
        yield
        state["ready"] = False
        log.info("http: app shutdown")

    app = FastAPI(title="ach-agent", lifespan=lifespan)
    # Expose state dict via app.extra so tests and main.py drain handler can flip flags
    app.extra["state"] = state

    # -----------------------------------------------------------------------
    # POST /channels/{channel_name}/events — inbound webhook (HTTP-01)
    # -----------------------------------------------------------------------

    @app.post("/channels/{channel_name}/events")
    async def inbound_events(channel_name: str, request: Request) -> JSONResponse:
        """Accept an inbound webhook event and dispatch it asynchronously.

        Stages:
          0. Pre-admission gate: draining (D-12) → 503 (acceptance is decoupled from
             engine readiness — the engine starts lazily per session_key)
          1. Resolve channel config by name (404 if unknown — T-02-06)
          2. Read raw body BEFORE any JSON parse (Pattern 1)
          3. Dispatch to webhook adapter (HMAC verify → parse → dedup → router)
          4. For non-202 outcomes (401/200-dup/400/422/503), return as-is
          5. ACCEPTED → return 202 accept-and-process-async
        """
        # 0. Pre-admission gate (D-12): reject before channel lookup + router.
        # draining (D-12): straggler inbound during SIGTERM drain → retriable 503.
        # GitLab redelivers to the next pod on 503 — this is the correct behavior.
        # Acceptance is decoupled from engine readiness (the A′ gate was removed):
        # the engine starts lazily per session_key inside the lane, never a
        # precondition for accepting the message (mirrors legacy ackbot-process).
        if state["draining"]:
            log.info(
                "http: 503 pre-admission",
                reason="draining",
                channel_name=channel_name,
            )
            return JSONResponse({"detail": "draining"}, status_code=503)

        # 1. Resolve channel config (T-02-06: 404 for unknown channel name)
        channel_cfg = channel_map.get(channel_name)
        if channel_cfg is None:
            log.warning("http: unknown channel", channel_name=channel_name)
            return JSONResponse({"detail": f"Unknown channel: {channel_name}"}, status_code=404)

        # 2. Raw body — MUST be read before any JSON parse (Pattern 1: HMAC runs on raw)
        #    T-02-05: enforce body-size cap before buffering.
        #    (a) Fast-reject on Content-Length header if present.
        #    (b) Streaming read with accumulation cap for missing/lying Content-Length.
        content_length_header = request.headers.get("content-length")
        if content_length_header is not None:
            try:
                declared_length = int(content_length_header)
            except ValueError:
                declared_length = 0
            if declared_length > MAX_WEBHOOK_BODY_BYTES:
                log.warning(
                    "http: request body too large (Content-Length)",
                    channel_name=channel_name,
                    declared_length=declared_length,
                    cap=MAX_WEBHOOK_BODY_BYTES,
                )
                return JSONResponse({"detail": "Payload too large"}, status_code=413)

        chunks: list[bytes] = []
        accumulated = 0
        async for chunk in request.stream():
            accumulated += len(chunk)
            if accumulated > MAX_WEBHOOK_BODY_BYTES:
                log.warning(
                    "http: request body exceeded cap mid-stream",
                    channel_name=channel_name,
                    cap=MAX_WEBHOOK_BODY_BYTES,
                )
                return JSONResponse({"detail": "Payload too large"}, status_code=413)
            chunks.append(chunk)
        raw_body: bytes = b"".join(chunks)
        headers: dict[str, str] = dict(request.headers)

        # 3. Dispatch — webhook adapter verifies auth, parses, deduplicates, routes.
        webhook_result = await handle_webhook_request(raw_body, headers, channel_cfg, handler)

        # 4. Non-202 outcomes (401, 200-dup, 400/422, 503) returned immediately
        if webhook_result.status_code != 202:
            return JSONResponse(webhook_result.body, status_code=webhook_result.status_code)

        # 5. ACCEPTED → accept-and-process-async (D-04)
        # X-ACH-Task-Id: correlation id for log/trace ONLY (not a lookup handle).
        return JSONResponse(
            webhook_result.body,
            status_code=202,
            headers={"X-ACH-Task-Id": webhook_result.task_id} if webhook_result.task_id else None,
        )

    # -----------------------------------------------------------------------
    # GET /healthz — liveness (HTTP-03, always 200 while process alive)
    # -----------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness probe — always 200 while process is alive (HTTP-03)."""
        return JSONResponse({"status": "ok"}, status_code=200)

    # -----------------------------------------------------------------------
    # GET /readyz — readiness (HTTP-02)
    # -----------------------------------------------------------------------

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Readiness probe — 200 iff lifespan has completed (HTTP-02, Pitfall 6).

        Ready = the webhook adapter is listening (lifespan set the flag).
        Engine warmup is NOT a gate (spec §8.5).
        """
        if not state["ready"]:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"status": "ok"}, status_code=200)

    # -----------------------------------------------------------------------
    # GET /metrics — Prometheus exposition (HTTP-04)
    # -----------------------------------------------------------------------

    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # Mount A2A sub-apps (single-process topology A, §15 / T-04-17).
    # Each A2A channel is mounted at /a2a/{channel_name} so the SDK's JSON-RPC
    # endpoint ("/") resolves to /a2a/{channel_name}/ without conflicting with
    # the existing POST /channels/{channel_name}/events route.
    for mount_path, sub_app in a2a_mounts or []:
        app.mount(mount_path, sub_app)
        log.info("a2a: sub-app mounted", path=mount_path)

    return app

"""FastAPI HTTP surface tests (HTTP-01..04) — Plan 02-02 GREEN / 02-05 updated.

Tests cover:
  - HTTP-02: GET /readyz returns 503 before lifespan, 200 after
  - HTTP-03: GET /healthz always returns 200
  - HTTP-04: GET /metrics returns Prometheus text via make_asgi_app()
  - HTTP-01: POST /channels/{name}/events dispatches to webhook adapter (202)
  - T-02-05: oversized body → 413, router NOT called (defense-in-depth DoS cap)
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig
from ach_agent.http.app import create_app
from ach_agent.router.router import RouterAdmitResult

# ---------------------------------------------------------------------------
# Helpers / shared fixtures
# ---------------------------------------------------------------------------


class FakeHandler:
    """Captures emitted MessageEvents and returns a configurable RouterAdmitResult."""

    def __init__(self, result: RouterAdmitResult = RouterAdmitResult.ACCEPTED) -> None:
        self._result = result
        self.events: list[MessageEvent] = []

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        return self._result


MR_PAYLOAD = {
    "object_kind": "merge_request",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"iid": 7, "title": "Add feature X", "state": "opened"},
}


def _make_channel_cfg(
    name: str = "gitlab-mr-review",
    secret_path: str = "/dev/null",
) -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": name,
            "type": "webhook",
            "source": "gitlab",
            "webhook": {
                "auth": {"type": "gitlab_token", "secretPath": secret_path},
            },
        }
    )


def _make_headers(secret: str, event_uuid: str | None = None) -> dict[str, str]:
    h: dict[str, str] = {
        "X-Gitlab-Token": secret,
        "X-Gitlab-Event": "Merge Request Hook",
        "Content-Type": "application/json",
    }
    if event_uuid is not None:
        h["X-Gitlab-Event-UUID"] = event_uuid
    return h


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_readyz(tmp_path: pytest.TempPathFactory) -> None:
    """HTTP-02: GET /readyz returns 503 before lifespan, 200 after (Pitfall 6).

    Uses FastAPI TestClient which triggers the lifespan on __enter__.
    Before entering the TestClient context: readyz → 503 (not ready).
    Inside the TestClient context (lifespan running): readyz → 200 (ready).
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler()
    app = create_app([cfg], handler)

    # Before lifespan is entered — use raw ASGITransport to bypass lifespan.
    # The ASGI transport does NOT trigger lifespan, so state["ready"] is False → 503.
    # Use asyncio.run() to create a fresh event loop (avoids "no current event loop"
    # when running after pytest-asyncio tests that close their event loops).
    async def get_readyz_no_lifespan() -> int:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/readyz")
            return resp.status_code

    status_before = asyncio.run(get_readyz_no_lifespan())
    assert status_before == 503, (
        f"readyz must return 503 before lifespan, got {status_before}"
    )

    # Inside the TestClient context manager — lifespan is triggered → 200
    with TestClient(app) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 200, (
            f"readyz must return 200 after lifespan enters, got {resp.status_code}"
        )


def test_healthz(tmp_path: pytest.TempPathFactory) -> None:
    """HTTP-03: GET /healthz always returns 200 while process is alive."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler()
    app = create_app([cfg], handler)

    with TestClient(app) as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


def test_metrics(tmp_path: pytest.TempPathFactory) -> None:
    """HTTP-04: GET /metrics returns Prometheus metrics via make_asgi_app()."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler()
    app = create_app([cfg], handler)

    with TestClient(app) as client:
        resp = client.get("/metrics")
        assert resp.status_code == 200, f"metrics returned {resp.status_code}"
        body = resp.text
        # Prometheus text format contains "# HELP" or "# TYPE" or may be empty on first call
        assert "# HELP" in body or "# TYPE" in body or body.strip() == "", (
            f"metrics body does not look like Prometheus exposition: {body[:200]!r}"
        )


def test_inbound_route_dispatches(tmp_path: pytest.TempPathFactory) -> None:
    """HTTP-01: POST /channels/{name}/events dispatches to webhook adapter → 202."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    app = create_app([cfg], handler)

    with TestClient(app) as client:
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )
    assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
    assert resp.json()["status"] == "accepted"


# ---------------------------------------------------------------------------
# T-02-05: body-size cap tests (RED phase — no cap implemented yet)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Decouple acceptance from engine readiness (drop A′ gate; keep draining).
# The A′ gate (engine_has_been_ready_once) has been removed from the
# pre-admission check — a webhook-only deployment must not 503 forever
# waiting on an engine that only starts lazily on acceptance (deadlock).
# ---------------------------------------------------------------------------


class FakePool:
    """Minimal fake EnginePool exposing engine_has_been_ready_once."""

    def __init__(self, ready: bool = False) -> None:
        self.engine_has_been_ready_once = ready


def test_webhook_accepted_when_engine_not_started(tmp_path: pytest.TempPathFactory) -> None:
    """Decouple: POST /channels/.../events returns 202 even when
    engine_has_been_ready_once=False — acceptance no longer waits on the engine
    (the reproduced cold-start deadlock, asserted fixed).
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    pool = FakePool(ready=False)  # engine never started yet
    app = create_app([cfg], handler, pool=pool)

    with TestClient(app) as client:
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )
    assert resp.status_code == 202, (
        f"engine-not-ready must not block acceptance, got {resp.status_code}: {resp.text}"
    )
    assert len(handler.events) == 1, "router must be called — acceptance is decoupled"


def test_a_prime_pass_after_ready(tmp_path: pytest.TempPathFactory) -> None:
    """POST /channels/.../events returns 202 when engine_has_been_ready_once=True."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    pool = FakePool(ready=True)  # engine ready
    app = create_app([cfg], handler, pool=pool)

    with TestClient(app) as client:
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )
    assert resp.status_code == 202, (
        f"POST must return 202 when engine ready, got {resp.status_code}"
    )


def test_webhook_503_only_when_draining(tmp_path: pytest.TempPathFactory) -> None:
    """D-12: 503 is emitted ONLY for draining — never for engine-not-ready.

    Same app/pool (engine never ready): before draining → 202; after draining → 503.
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    pool = FakePool(ready=False)  # engine not ready — must not be a 503 reason
    app = create_app([cfg], handler, pool=pool)

    with TestClient(app) as client:
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )
        assert resp.status_code == 202, (
            f"engine-not-ready must never 503, got {resp.status_code}"
        )

        app.extra["state"]["draining"] = True
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )
        assert resp.status_code == 503, (
            f"draining must return 503, got {resp.status_code}"
        )


def test_draining_503(tmp_path: pytest.TempPathFactory) -> None:
    """D-12: POST returns 503 when state['draining']=True (straggler during drain).

    Sets draining via app.extra["state"] after lifespan entry. Handler must not be called.
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    pool = FakePool(ready=True)  # engine ready — draining is the gate, not A′
    app = create_app([cfg], handler, pool=pool)

    with TestClient(app) as client:
        # Simulate drain: set draining on the shared state dict exposed via app.extra
        app.extra["state"]["draining"] = True
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )
    assert resp.status_code == 503, (
        f"D-12: draining gate must return 503, got {resp.status_code}"
    )
    assert handler.events == [], "D-12: draining gate must prevent router from being called"


# ---------------------------------------------------------------------------
# T-02-05: body-size cap tests (RED phase — no cap implemented yet)
# ---------------------------------------------------------------------------


def test_oversized_body_via_content_length_returns_413(
    tmp_path: pytest.TempPathFactory,
) -> None:
    """T-02-05: Content-Length exceeding cap → 413 before router is called.

    RED: no MAX_WEBHOOK_BODY_BYTES cap exists yet — today the request is
    accepted and the handler is called, so this test FAILS at the 413 assert.
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    app = create_app([cfg], handler)

    cap = 1 * 1024 * 1024  # 1 MiB — must match MAX_WEBHOOK_BODY_BYTES
    oversized_content_length = cap + 1

    with TestClient(app) as client:
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=b"x",  # actual body tiny; Content-Length header is what matters
            headers={
                **_make_headers("s3cr3t", event_uuid="fake-uuid-cl"),
                "Content-Length": str(oversized_content_length),
            },
        )

    assert resp.status_code == 413, (
        f"T-02-05: oversized Content-Length must return 413, got {resp.status_code}"
    )
    assert handler.events == [], (
        "T-02-05: router must NOT be called for oversized body"
    )


def test_oversized_body_streaming_returns_413(tmp_path: pytest.TempPathFactory) -> None:
    """T-02-05: body exceeding cap mid-stream → 413, router NOT called.

    RED: no streaming cap implemented — today the body is fully buffered and
    the request reaches the router, so this test FAILS at the 413 assert.
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    app = create_app([cfg], handler)

    cap = 1 * 1024 * 1024  # 1 MiB
    oversized_body = b"x" * (cap + 1)

    with TestClient(app) as client:
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=oversized_body,
            headers=_make_headers("s3cr3t", event_uuid="fake-uuid-stream"),
        )

    assert resp.status_code == 413, (
        f"T-02-05: oversized streaming body must return 413, got {resp.status_code}"
    )
    assert handler.events == [], (
        "T-02-05: router must NOT be called for oversized body"
    )

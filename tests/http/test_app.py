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
from pathlib import Path

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
# DUR-02 / D-12: A′ cold-start gate + draining 503 tests (Plan 03-02)
# ---------------------------------------------------------------------------


class FakePool:
    """Minimal fake EnginePool exposing engine_has_been_ready_once."""

    def __init__(self, ready: bool = False) -> None:
        self.engine_has_been_ready_once = ready


def test_a_prime_503_before_ready(tmp_path: pytest.TempPathFactory) -> None:
    """DUR-02: POST /channels/.../events returns 503 when engine_has_been_ready_once=False.

    A′ gate fires before the router — handler.events must be empty (never called).
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    pool = FakePool(ready=False)  # engine not ready yet
    app = create_app([cfg], handler, pool=pool)

    with TestClient(app) as client:
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )
    assert resp.status_code == 503, (
        f"DUR-02: A′ gate must return 503 before engine ready, got {resp.status_code}"
    )
    assert handler.events == [], "A′ gate must prevent router from being called"


def test_a_prime_pass_after_ready(tmp_path: pytest.TempPathFactory) -> None:
    """DUR-02: POST /channels/.../events returns 202 when engine_has_been_ready_once=True.

    Normal admission via FakeHandler ACCEPTED — A′ gate passes through.
    """
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
        f"DUR-02: A′ gate must pass through after engine ready, got {resp.status_code}"
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


def test_draining_503_does_not_count_cold_start_reject(tmp_path: Path) -> None:
    """WR-01: when SIGTERM drain arrives before first warmup (draining AND not ready),
    the 503 is a drain reject, not a cold-start reject — COLD_START_REJECTS must NOT
    increment. The combined pre-admission gate reports reason='draining' and only
    reason='engine_not_ready' may touch the cold-start counter.
    """
    from ach_agent.router.metrics import COLD_START_REJECTS

    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")
    cfg = _make_channel_cfg(secret_path=str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    pool = FakePool(ready=False)  # engine NEVER ready yet AND draining → drain wins
    app = create_app([cfg], handler, pool=pool)

    counter = COLD_START_REJECTS.labels(channel="gitlab-mr-review")
    before = counter._value.get()

    with TestClient(app) as client:
        app.extra["state"]["draining"] = True
        resp = client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t", event_uuid=str(uuid.uuid4())),
        )

    assert resp.status_code == 503, f"drain gate must return 503, got {resp.status_code}"
    assert counter._value.get() == before, (
        "WR-01: drain-503 (draining AND not-ready) must not increment COLD_START_REJECTS"
    )


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

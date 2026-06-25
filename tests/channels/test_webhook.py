"""Webhook channel tests (CHN-01, IDM-01, D-05, SEC-02) — Plan 02-02 GREEN.

Tests cover:
  - CHN-01 / SEC-02: valid/invalid X-Gitlab-Token → 202 / 401
  - CHN-01: MR payload extraction into delivery_context (D-07)
  - IDM-01: X-Gitlab-Event-UUID as idempotency key; ms-timestamp fallback
  - D-05: status map ACCEPTED→202 / DUPLICATE→200 / FULL_QUEUE→503
  - SEC-02: secret read per-request (never cached)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.webhook import handle_webhook_request
from ach_agent.config.schema import ChannelConfig
from ach_agent.router.router import RouterAdmitResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MR_PAYLOAD = {
    "object_kind": "merge_request",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"iid": 7, "title": "Add feature X", "state": "opened"},
}


class FakeHandler:
    """Captures emitted MessageEvents and returns a configurable RouterAdmitResult."""

    def __init__(self, result: RouterAdmitResult = RouterAdmitResult.ACCEPTED) -> None:
        self._result = result
        self.events: list[MessageEvent] = []
        self._call_count = 0

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        self._call_count += 1
        return self._result


def _make_channel_cfg(secret_path: str) -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": "gitlab-mr-review",
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


@pytest.mark.asyncio
async def test_valid_token_accepted(tmp_path: pytest.TempPathFactory) -> None:
    """CHN-01 / SEC-02: valid X-Gitlab-Token → 202 Accepted."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("my-webhook-secret")

    cfg = _make_channel_cfg(str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("my-webhook-secret", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 202
    assert result.body["status"] == "accepted"
    assert handler._call_count == 1, "handler.handle() must be called once"


@pytest.mark.asyncio
async def test_invalid_token_401(tmp_path: pytest.TempPathFactory) -> None:
    """CHN-01 / SEC-02: invalid X-Gitlab-Token → 401; handler.handle() NOT called."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("real-secret")

    cfg = _make_channel_cfg(str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("wrong-secret", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 401
    assert handler._call_count == 0, "handler.handle() must NOT be called on 401"


@pytest.mark.asyncio
async def test_mr_payload_extraction(tmp_path: pytest.TempPathFactory) -> None:
    """CHN-01: MR payload fields (project_id, mr_iid) extracted into delivery_context (D-07)."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")

    cfg = _make_channel_cfg(str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("s3cr3t", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 202
    assert len(handler.events) == 1
    event = handler.events[0]
    assert event.delivery_context["project_id"] == 42, "D-07: project_id must be 42"
    assert event.delivery_context["mr_iid"] == 7, "D-07: mr_iid must be 7"
    assert event.session_key == "42:7", "session_key derived from project_id:mr_iid"


@pytest.mark.asyncio
async def test_dedup_key_from_event_uuid(tmp_path: pytest.TempPathFactory) -> None:
    """IDM-01: X-Gitlab-Event-UUID header used as idempotency key when present."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")

    cfg = _make_channel_cfg(str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    event_uuid = "550e8400-e29b-41d4-a716-446655440000"
    headers = _make_headers("s3cr3t", event_uuid=event_uuid)
    raw_body = json.dumps(MR_PAYLOAD).encode()

    await handle_webhook_request(raw_body, headers, cfg, handler)

    assert len(handler.events) == 1
    assert handler.events[0].idempotency_key == event_uuid, (
        f"IDM-01: idempotency_key must be X-Gitlab-Event-UUID={event_uuid!r}"
    )


@pytest.mark.asyncio
async def test_dedup_key_fallback(tmp_path: pytest.TempPathFactory) -> None:
    """IDM-01: ms-timestamp fallback used as idempotency key when UUID header absent."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")

    cfg = _make_channel_cfg(str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    # No X-Gitlab-Event-UUID in headers — fallback must kick in
    headers = _make_headers("s3cr3t", event_uuid=None)
    raw_body = json.dumps(MR_PAYLOAD).encode()

    await handle_webhook_request(raw_body, headers, cfg, handler)

    assert len(handler.events) == 1
    key = handler.events[0].idempotency_key
    # Fallback is str(int(time.time() * 1000)) — a numeric string
    assert key.isdigit(), (
        f"IDM-01 fallback: idempotency_key must be ms-timestamp string, got {key!r}"
    )


@pytest.mark.asyncio
async def test_http_status_map(tmp_path: pytest.TempPathFactory) -> None:
    """D-05: router outcomes map to correct HTTP statuses (202/200/503)."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("s3cr3t")

    cfg = _make_channel_cfg(str(secret_file))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    # ACCEPTED → 202
    handler_accepted = FakeHandler(RouterAdmitResult.ACCEPTED)
    r1 = await handle_webhook_request(
        raw_body, _make_headers("s3cr3t", event_uuid=str(uuid.uuid4())), cfg, handler_accepted
    )
    assert r1.status_code == 202, f"ACCEPTED must map to 202, got {r1.status_code}"

    # DUPLICATE → 200
    handler_dup = FakeHandler(RouterAdmitResult.DUPLICATE)
    r2 = await handle_webhook_request(
        raw_body, _make_headers("s3cr3t", event_uuid=str(uuid.uuid4())), cfg, handler_dup
    )
    assert r2.status_code == 200, f"DUPLICATE must map to 200, got {r2.status_code}"

    # FULL_QUEUE → 503
    handler_full = FakeHandler(RouterAdmitResult.FULL_QUEUE)
    r3 = await handle_webhook_request(
        raw_body, _make_headers("s3cr3t", event_uuid=str(uuid.uuid4())), cfg, handler_full
    )
    assert r3.status_code == 503, f"FULL_QUEUE must map to 503, got {r3.status_code}"


@pytest.mark.asyncio
async def test_secret_read_per_request(tmp_path: pytest.TempPathFactory) -> None:
    """SEC-02: webhook secret is read from file per-request, never cached.

    Verifies by writing a new secret between two calls and observing that the
    second call uses the NEW value — proving the secret is NOT cached.
    """
    secret_file = tmp_path / "secret"
    secret_file.write_text("initial-secret")

    cfg = _make_channel_cfg(str(secret_file))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    # First call succeeds with initial secret
    handler1 = FakeHandler(RouterAdmitResult.ACCEPTED)
    r1 = await handle_webhook_request(
        raw_body, _make_headers("initial-secret", event_uuid=str(uuid.uuid4())), cfg, handler1
    )
    assert r1.status_code == 202, "First call with correct secret must succeed"

    # Rotate the secret (rewrite the file)
    secret_file.write_text("rotated-secret")

    # Second call with OLD secret must now fail (401) — proves no caching
    handler2 = FakeHandler(RouterAdmitResult.ACCEPTED)
    r2 = await handle_webhook_request(
        raw_body, _make_headers("initial-secret", event_uuid=str(uuid.uuid4())), cfg, handler2
    )
    assert r2.status_code == 401, (
        "After secret rotation, old token must be rejected (SEC-02: no caching)"
    )

    # Second call with NEW secret must succeed — proves per-request read
    handler3 = FakeHandler(RouterAdmitResult.ACCEPTED)
    r3 = await handle_webhook_request(
        raw_body, _make_headers("rotated-secret", event_uuid=str(uuid.uuid4())), cfg, handler3
    )
    assert r3.status_code == 202, (
        "New token must be accepted after rotation (SEC-02: per-request read)"
    )


# ---------------------------------------------------------------------------
# source-selected parser + auth.type-selected verification
# ---------------------------------------------------------------------------

GITHUB_PR_PAYLOAD = {
    "action": "opened",
    "number": 7,
    "pull_request": {"number": 7, "title": "X"},
    "repository": {"full_name": "acme/repo", "id": 123},
}


@pytest.mark.asyncio
async def test_github_source_parses_pr_and_hmac_auth(tmp_path: pytest.TempPathFactory) -> None:
    """github source: PR parse + HMAC-SHA256 auth → 202."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("gh-hmac-secret")

    cfg = ChannelConfig.model_validate(
        {
            "name": "gh",
            "type": "webhook",
            "source": "github",
            "webhook": {"auth": {"type": "hmac", "secretPath": str(secret_file)}},
        }
    )
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    raw_body = json.dumps(GITHUB_PR_PAYLOAD).encode()
    signature = hmac.new(b"gh-hmac-secret", raw_body, hashlib.sha256).hexdigest()
    delivery = str(uuid.uuid4())
    headers = {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": delivery,
        "Content-Type": "application/json",
    }

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 202
    assert handler._call_count == 1
    event = handler.events[0]
    assert event.delivery_context == {"repo": "acme/repo", "pr_number": 7}
    assert event.session_key == "acme/repo:7"
    assert event.idempotency_key == delivery


@pytest.mark.asyncio
async def test_hmac_auth_rejects_bad_signature(tmp_path: pytest.TempPathFactory) -> None:
    """github source: wrong HMAC signature → 401, handler NOT called."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("gh-hmac-secret")

    cfg = ChannelConfig.model_validate(
        {
            "name": "gh",
            "type": "webhook",
            "source": "github",
            "webhook": {"auth": {"type": "hmac", "secretPath": str(secret_file)}},
        }
    )
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    raw_body = json.dumps(GITHUB_PR_PAYLOAD).encode()
    headers = {
        "X-Hub-Signature-256": "sha256=deadbeef",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 401
    assert handler._call_count == 0


@pytest.mark.asyncio
async def test_generic_source_uses_request_id_key(tmp_path: pytest.TempPathFactory) -> None:
    """generic source: no payload requirements; session_key == idempotency_key."""
    cfg = ChannelConfig.model_validate(
        {
            "name": "g",
            "type": "webhook",
            "source": "generic",
            "webhook": {"auth": {"type": "none"}},
        }
    )
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    raw_body = json.dumps({"event": "ping"}).encode()
    headers = {"X-Request-ID": "req-123", "Content-Type": "application/json"}

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 202
    event = handler.events[0]
    assert event.idempotency_key == "req-123"
    assert event.delivery_context == {}
    assert event.session_key == "req-123"


@pytest.mark.asyncio
async def test_gitlab_token_auth_rejects_bad_token(tmp_path: pytest.TempPathFactory) -> None:
    """gitlab source + gitlab_token auth: wrong X-Gitlab-Token → 401."""
    secret_file = tmp_path / "secret"
    secret_file.write_text("real-secret")

    cfg = _make_channel_cfg(str(secret_file))
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("wrong-secret", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 401
    assert handler._call_count == 0

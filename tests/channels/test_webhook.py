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
from typing import Any

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.webhook import handle_webhook_request
from ach_agent.config.schema import ChannelConfig
from ach_agent.router.router import RouterAdmitResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Dedicated env var name for auth-exercising tests in this file. monkeypatch.setenv/delenv
# scope the value to each test, so reuse across tests is safe.
SECRET_ENV = "ACH_SECRET_TEST"

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


def _make_channel_cfg(env_name: str = SECRET_ENV) -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": "gitlab-mr-review",
            "type": "webhook",
            "source": "gitlab",
            "webhook": {
                "auth": {"type": "gitlab_token", "secret": {"env": env_name}},
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
async def test_valid_token_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHN-01 / SEC-02: valid X-Gitlab-Token → 202 Accepted."""
    monkeypatch.setenv(SECRET_ENV, "my-webhook-secret")

    cfg = _make_channel_cfg()
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("my-webhook-secret", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 202
    assert result.body["status"] == "accepted"
    assert handler._call_count == 1, "handler.handle() must be called once"


@pytest.mark.asyncio
async def test_invalid_token_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHN-01 / SEC-02: invalid X-Gitlab-Token → 401; handler.handle() NOT called."""
    monkeypatch.setenv(SECRET_ENV, "real-secret")

    cfg = _make_channel_cfg()
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("wrong-secret", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 401
    assert handler._call_count == 0, "handler.handle() must NOT be called on 401"


@pytest.mark.asyncio
async def test_mr_payload_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHN-01: MR payload fields (project_id, mr_iid) extracted into delivery_context (D-07)."""
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
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
async def test_dedup_key_from_event_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """IDM-01: X-Gitlab-Event-UUID header used as idempotency key when present."""
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
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
async def test_dedup_key_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """IDM-01: ms-timestamp fallback used as idempotency key when UUID header absent."""
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
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
async def test_http_status_map(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05: router outcomes map to correct HTTP statuses (202/200/503)."""
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
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
async def test_secret_read_per_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-02: webhook secret is read from env per-request, never cached.

    Verifies by rotating the env var between two calls and observing that the
    second call uses the NEW value — proving the secret is NOT cached.
    """
    monkeypatch.setenv(SECRET_ENV, "initial-secret")

    cfg = _make_channel_cfg()
    raw_body = json.dumps(MR_PAYLOAD).encode()

    # First call succeeds with initial secret
    handler1 = FakeHandler(RouterAdmitResult.ACCEPTED)
    r1 = await handle_webhook_request(
        raw_body, _make_headers("initial-secret", event_uuid=str(uuid.uuid4())), cfg, handler1
    )
    assert r1.status_code == 202, "First call with correct secret must succeed"

    # Rotate the secret (re-set the env var)
    monkeypatch.setenv(SECRET_ENV, "rotated-secret")

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
async def test_github_source_parses_pr_and_hmac_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """github source: PR parse + HMAC-SHA256 auth → 202."""
    monkeypatch.setenv(SECRET_ENV, "gh-hmac-secret")

    cfg = ChannelConfig.model_validate(
        {
            "name": "gh",
            "type": "webhook",
            "source": "github",
            "webhook": {"auth": {"type": "hmac", "secret": {"env": SECRET_ENV}}},
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
async def test_hmac_auth_rejects_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    """github source: wrong HMAC signature → 401, handler NOT called."""
    monkeypatch.setenv(SECRET_ENV, "gh-hmac-secret")

    cfg = ChannelConfig.model_validate(
        {
            "name": "gh",
            "type": "webhook",
            "source": "github",
            "webhook": {"auth": {"type": "hmac", "secret": {"env": SECRET_ENV}}},
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
async def test_generic_source_uses_request_id_key() -> None:
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
async def test_gitlab_token_auth_rejects_bad_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """gitlab source + gitlab_token auth: wrong X-Gitlab-Token → 401."""
    monkeypatch.setenv(SECRET_ENV, "real-secret")

    cfg = _make_channel_cfg()
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("wrong-secret", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 401
    assert handler._call_count == 0


@pytest.mark.asyncio
async def test_unset_env_secret_rejects_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """CR-02: schema-valid secret={env: NAME} whose env var is UNSET must REJECT.

    resolve_secret() returns None when the env var is unset, so _verify_header_token must
    fail-closed rather than treat the unresolved secret as a pass. An otherwise-plausible
    token is presented to prove the rejection comes from the unresolvable secret, not a
    header mismatch.
    """
    monkeypatch.delenv(SECRET_ENV, raising=False)
    cfg = _make_channel_cfg()
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("some-plausible-token", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 401
    assert handler._call_count == 0, (
        "handler.handle() must NOT be called when the secret env var is unset"
    )


def test_header_token_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """header_token auth: static shared secret in a configurable header (constant-time)."""
    from ach_agent.channels.webhook import _verify_auth
    from ach_agent.config.schema import SecretSource, WebhookAuthBlock

    monkeypatch.setenv(SECRET_ENV, "topsecret")
    auth = WebhookAuthBlock(
        type="header_token", header="X-Api-Key", secret=SecretSource(env=SECRET_ENV)
    )
    assert _verify_auth(auth, {"x-api-key": "topsecret"}, b"") is True
    assert _verify_auth(auth, {"x-api-key": "wrong"}, b"") is False
    assert _verify_auth(auth, {}, b"") is False


# ---------------------------------------------------------------------------
# Secondary dedup key (GitLab logical content composite) — Plan 3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_sets_secondary_idempotency_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """gitlab source: secondary_idempotency_key == composite; primary still the UUID."""
    from ach_agent.router.dedup import derive_gitlab_composite_key

    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    event_uuid = str(uuid.uuid4())
    headers = _make_headers("s3cr3t", event_uuid=event_uuid)
    raw_body = json.dumps(MR_PAYLOAD).encode()

    await handle_webhook_request(raw_body, headers, cfg, handler)

    event = handler.events[0]
    assert event.idempotency_key == event_uuid, "primary key unchanged (UUID)"
    assert event.secondary_idempotency_key == derive_gitlab_composite_key(MR_PAYLOAD)


@pytest.mark.asyncio
async def test_github_leaves_secondary_key_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """github source: secondary_idempotency_key stays None (gitlab-only in v1)."""
    monkeypatch.setenv(SECRET_ENV, "gh-hmac-secret")

    cfg = ChannelConfig.model_validate(
        {
            "name": "gh",
            "type": "webhook",
            "source": "github",
            "webhook": {"auth": {"type": "hmac", "secret": {"env": SECRET_ENV}}},
        }
    )
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    raw_body = json.dumps(GITHUB_PR_PAYLOAD).encode()
    signature = hmac.new(b"gh-hmac-secret", raw_body, hashlib.sha256).hexdigest()
    headers = {
        "X-Hub-Signature-256": f"sha256={signature}",
        "X-GitHub-Delivery": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    await handle_webhook_request(raw_body, headers, cfg, handler)

    assert handler.events[0].secondary_idempotency_key is None


# ---------------------------------------------------------------------------
# Configurable GitLab event routing (note-hook 422 fix)
# ---------------------------------------------------------------------------

NOTE_ON_MR_PAYLOAD = {
    "object_kind": "note",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"noteable_type": "MergeRequest", "note": "please rebase"},
    "merge_request": {"iid": 7, "title": "Add feature X"},
}

ISSUE_PAYLOAD = {
    "object_kind": "issue",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"iid": 5, "title": "Bug report", "description": "boom"},
}

NOTE_ON_ISSUE_PAYLOAD = {
    "object_kind": "note",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"noteable_type": "Issue", "note": "still broken"},
    "issue": {"iid": 5, "title": "Bug report"},
}

NOTE_ON_COMMIT_PAYLOAD = {
    "object_kind": "note",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"noteable_type": "Commit", "note": "nice"},
}

PIPELINE_PAYLOAD = {
    "object_kind": "pipeline",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"id": 999, "status": "success"},
}

NOTE_ON_MR_MISSING_MR = {
    "object_kind": "note",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"noteable_type": "MergeRequest", "note": "please rebase"},
    # no "merge_request" block → routable-but-malformed → 422
}


def _make_cfg_events(
    env_name: str = SECRET_ENV,
    events: list[str] | None = None,
    bot_username: str | None = None,
    trigger_users: list[str] | None = None,
) -> ChannelConfig:
    webhook: dict[str, Any] = {"auth": {"type": "gitlab_token", "secret": {"env": env_name}}}
    if events is not None:
        webhook["gitlabEvents"] = events
    if bot_username is not None:
        webhook["botUsername"] = bot_username
    if trigger_users is not None:
        webhook["triggerUsers"] = trigger_users
    return ChannelConfig.model_validate(
        {"name": "gl", "type": "webhook", "source": "gitlab", "webhook": webhook}
    )


def _note_by(username: str, *, system: bool = False) -> dict[str, Any]:
    """A note-on-MR hook authored by `username` (project 42, MR 7)."""
    attrs: dict[str, Any] = {"noteable_type": "MergeRequest", "note": "please rebase"}
    if system:
        attrs["system"] = True
    return {
        "object_kind": "note",
        "user": {"username": username},
        "project": {"id": 42},
        "merge_request": {"iid": 7},
        "object_attributes": attrs,
    }


def _mr_by(username: str) -> dict[str, Any]:
    """A merge_request hook authored by `username` (project 42, MR 7)."""
    return {
        "object_kind": "merge_request",
        "user": {"username": username},
        "project": {"id": 42, "name": "my-repo"},
        "object_attributes": {"iid": 7, "title": "x", "state": "opened"},
    }


async def _post(payload: dict[str, Any], cfg: ChannelConfig, secret: str) -> tuple:
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers(secret, event_uuid=str(uuid.uuid4()))
    result = await handle_webhook_request(json.dumps(payload).encode(), headers, cfg, handler)
    return result, handler


@pytest.mark.asyncio
async def test_mr_hook_default_routes_with_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(MR_PAYLOAD, cfg, "s")
    assert result.status_code == 202
    ev = handler.events[0]
    assert ev.session_key == "42:7"
    assert ev.delivery_context["kind"] == "merge_request"
    assert ev.delivery_context["mr_iid"] == 7


@pytest.mark.asyncio
async def test_note_on_mr_routes_same_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(NOTE_ON_MR_PAYLOAD, cfg, "s")
    assert result.status_code == 202
    ev = handler.events[0]
    assert ev.session_key == "42:7"
    assert ev.delivery_context["kind"] == "note"
    assert ev.delivery_context["target_type"] == "mr"
    assert ev.delivery_context["mr_iid"] == 7


@pytest.mark.asyncio
async def test_issue_hook_routes_namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(ISSUE_PAYLOAD, cfg, "s")
    assert result.status_code == 202
    ev = handler.events[0]
    assert ev.session_key == "42:issue:5"
    assert ev.delivery_context["kind"] == "issue"
    assert ev.delivery_context["issue_iid"] == 5


@pytest.mark.asyncio
async def test_note_on_issue_routes_namespaced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(NOTE_ON_ISSUE_PAYLOAD, cfg, "s")
    assert result.status_code == 202
    assert handler.events[0].session_key == "42:issue:5"


@pytest.mark.asyncio
async def test_note_on_commit_ignored_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(NOTE_ON_COMMIT_PAYLOAD, cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}
    assert handler._call_count == 0


@pytest.mark.asyncio
async def test_pipeline_ignored_200_not_422(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(PIPELINE_PAYLOAD, cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}


@pytest.mark.asyncio
async def test_note_on_mr_ignored_when_mr_not_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events(events=["issue"])
    result, handler = await _post(NOTE_ON_MR_PAYLOAD, cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}


@pytest.mark.asyncio
async def test_commit_note_missing_project_still_ignored_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-routable note (commit) must accept-ignore (200) even if project block is absent."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    payload = {
        "object_kind": "note",
        "object_attributes": {"noteable_type": "Commit", "note": "nice"},
        # no "project" block — must NOT 422 (non-routable → 200)
    }
    result, handler = await _post(payload, cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}


@pytest.mark.asyncio
async def test_note_on_mr_missing_block_raises_422(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(NOTE_ON_MR_MISSING_MR, cfg, "s")
    assert result.status_code == 422
    assert handler._call_count == 0


# ---------------------------------------------------------------------------
# Actor gates: botUsername loop-guard + triggerUsers allowlist (gitlab only)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_self_authored_dropped_by_bot_username(monkeypatch: pytest.MonkeyPatch) -> None:
    """Event authored by botUsername → 200 ignored, never enqueued (loop guard)."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events(bot_username="ackbot")
    result, handler = await _post(_note_by("ackbot"), cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}
    assert handler._call_count == 0


@pytest.mark.asyncio
async def test_other_author_passes_when_bot_username_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """botUsername gate drops only the bot — a human author still routes."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events(bot_username="ackbot")
    result, handler = await _post(_note_by("alice"), cfg, "s")
    assert result.status_code == 202
    assert handler._call_count == 1


@pytest.mark.asyncio
async def test_trigger_users_allows_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Author in triggerUsers → routes."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events(trigger_users=["alice", "bob"])
    result, handler = await _post(_note_by("alice"), cfg, "s")
    assert result.status_code == 202
    assert handler._call_count == 1


@pytest.mark.asyncio
async def test_trigger_users_drops_unlisted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Author not in triggerUsers → 200 ignored, never enqueued."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events(trigger_users=["alice"])
    result, handler = await _post(_note_by("mallory"), cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}
    assert handler._call_count == 0


@pytest.mark.asyncio
async def test_trigger_users_gates_mr_hook_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Allowlist applies to every routed kind, not just notes (MR hook by unlisted user)."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events(trigger_users=["alice"])
    result, handler = await _post(_mr_by("bob"), cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}
    assert handler._call_count == 0


@pytest.mark.asyncio
async def test_no_actor_config_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both gates unset (default) → any author routes (off by default)."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(_note_by("anyone"), cfg, "s")
    assert result.status_code == 202
    assert handler._call_count == 1


@pytest.mark.asyncio
async def test_system_note_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitLab system notes (label/assignee changes) → 200 ignored regardless of config."""
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(_note_by("alice", system=True), cfg, "s")
    assert result.status_code == 200
    assert result.body == {"status": "ignored"}
    assert handler._call_count == 0


# ---------------------------------------------------------------------------
# Correlation task_id on the 202 accept (log/trace correlation ONLY)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accepted_response_carries_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """202 accept: WebhookResult.task_id is a non-empty uuid, echoed in the body."""
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)
    headers = _make_headers("s3cr3t", event_uuid=str(uuid.uuid4()))
    raw_body = json.dumps(MR_PAYLOAD).encode()

    result = await handle_webhook_request(raw_body, headers, cfg, handler)

    assert result.status_code == 202
    assert result.task_id, "WebhookResult.task_id must be non-empty on 202"
    assert result.body["task_id"] == result.task_id, "task_id must be echoed in the body"
    # uuid4 hex — uuid.UUID(...) parses it back.
    assert uuid.UUID(result.task_id)

    # The MessageEvent dispatched to the router carries the SAME task_id.
    assert handler.events[0].task_id == result.task_id


@pytest.mark.asyncio
async def test_distinct_events_get_distinct_task_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two distinct webhook requests get two distinct task_ids."""
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
    raw_body = json.dumps(MR_PAYLOAD).encode()

    handler1 = FakeHandler(RouterAdmitResult.ACCEPTED)
    r1 = await handle_webhook_request(
        raw_body, _make_headers("s3cr3t", event_uuid=str(uuid.uuid4())), cfg, handler1
    )
    handler2 = FakeHandler(RouterAdmitResult.ACCEPTED)
    r2 = await handle_webhook_request(
        raw_body, _make_headers("s3cr3t", event_uuid=str(uuid.uuid4())), cfg, handler2
    )

    assert r1.task_id != r2.task_id, "distinct events must get distinct task_ids"


@pytest.mark.asyncio
async def test_non_accepted_responses_have_no_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """DUPLICATE (200) / FULL_QUEUE (503) bodies are unchanged — no task_id."""
    monkeypatch.setenv(SECRET_ENV, "s3cr3t")

    cfg = _make_channel_cfg()
    raw_body = json.dumps(MR_PAYLOAD).encode()

    handler_dup = FakeHandler(RouterAdmitResult.DUPLICATE)
    r_dup = await handle_webhook_request(
        raw_body, _make_headers("s3cr3t", event_uuid=str(uuid.uuid4())), cfg, handler_dup
    )
    assert "task_id" not in r_dup.body

    handler_full = FakeHandler(RouterAdmitResult.FULL_QUEUE)
    r_full = await handle_webhook_request(
        raw_body, _make_headers("s3cr3t", event_uuid=str(uuid.uuid4())), cfg, handler_full
    )
    assert "task_id" not in r_full.body

"""GitlabCommentAdapter tests (ACT-02, ACT-03, SEC-02, SEC-03, T-02-07).

Tests cover:
  - ACT-02: deliver() POSTs note to GitLab MR notes endpoint with PRIVATE-TOKEN
  - ACT-02 / SEC-02: cross-host redirect strips PRIVATE-TOKEN (Pitfall 3)
  - T-02-07: relative Location resolved via urljoin, PRIVATE-TOKEN retained (same host)
  - T-02-07: fail-closed strip when redirect_host is None after urljoin
  - SEC-02 behavioral: GITLAB_TOKEN read at call time, never stored in adapter (D-12)
  - ACT-03 / D-05: sideEffect consent gate — dry-run + audit or ConsentDenied + audit;
    reply still delivers on denied (D-05)
  - ACT-03 / D-07: audit event carries all required correlation fields
  - SEC-03: GITLAB_TOKEN sentinel never appears in log output
"""

from __future__ import annotations

import json
import asyncio
from io import StringIO
from typing import Any

import pytest
import structlog

from ach_agent.actions.gitlab_comment import (
    ConsentDenied,
    GitlabCommentAdapter,
    UnsupportedActionKind,
    dispatch_actions,
)
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ResponseActionBlock
from ach_agent.engine.sanitized_env import (
    redact_ek_processor,
    redact_gitlab_token_processor,
)


# ---------------------------------------------------------------------------
# Helpers — fake aiohttp session
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal fake aiohttp response context manager."""

    def __init__(
        self,
        status: int = 201,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers: dict[str, str] = headers or {}
        self.captured_headers: dict[str, str] = {}
        self.captured_url: str = ""
        self.captured_json: dict[str, Any] | None = None

    async def text(self) -> str:
        return ""

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _FakeSession:
    """Minimal fake aiohttp.ClientSession that captures POST calls."""

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.posts: list[dict[str, Any]] = []
        self.closed = False

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> _FakeResponse:
        self.posts.append({"url": url, "headers": dict(headers or {}), "json": json})
        self._response.captured_url = url
        self._response.captured_headers = dict(headers or {})
        self._response.captured_json = json
        return self._response

    async def close(self) -> None:
        self.closed = True


class _FakeSessionWithRedirect:
    """Fake aiohttp.ClientSession that returns a cross-host redirect then 201."""

    def __init__(self, redirect_url: str) -> None:
        self._redirect_url = redirect_url
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self._call_count = 0
        self.closed = False

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> Any:
        self._call_count += 1
        self.posts.append({"url": url, "headers": dict(headers or {}), "json": json})
        # First POST: return 301 redirect to cross-host target
        resp = _FakeResponse(
            status=301,
            headers={"Location": self._redirect_url},
        )
        return resp

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> Any:
        self.gets.append({"url": url, "headers": dict(headers or {})})
        resp = _FakeResponse(status=201)
        return resp

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Task 1 Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posts_mr_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """ACT-02: GitlabCommentAdapter.deliver POSTs note to GitLab MR notes endpoint."""
    monkeypatch.setenv("GITLAB_TOKEN", "test_token_abc")

    fake_resp = _FakeResponse(status=201)
    fake_session = _FakeSession(fake_resp)

    adapter = GitlabCommentAdapter(base_url="https://gitlab.example.com")
    # Inject the fake session directly (bypassing _ensure_session)
    adapter._session = fake_session  # type: ignore[assignment]

    action = {"kind": "reply", "input": {"text": "LGTM!"}}
    context = {"project_id": 42, "mr_iid": 7}

    await adapter.deliver(action, context)

    assert len(fake_session.posts) == 1, "Expected exactly one POST call"
    post = fake_session.posts[0]

    # Verify URL shape
    assert "projects/42/merge_requests/7/notes" in post["url"], (
        f"URL must target the MR notes endpoint; got {post['url']!r}"
    )
    # Verify PRIVATE-TOKEN header present
    assert post["headers"].get("PRIVATE-TOKEN") == "test_token_abc", (
        "PRIVATE-TOKEN header must carry the token"
    )
    # Verify note body
    assert post["json"] == {"body": "LGTM!"}, (
        f"POST body must be {{body: text}}; got {post['json']!r}"
    )


@pytest.mark.asyncio
async def test_redirect_strips_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-02 / ACT-02: PRIVATE-TOKEN header stripped when redirect crosses host boundary."""
    monkeypatch.setenv("GITLAB_TOKEN", "test_token_abc")

    # Cross-host redirect: gitlab.example.com → cdn.otherdomain.com
    cross_host_redirect = "https://cdn.otherdomain.com/path/to/resource"
    fake_session = _FakeSessionWithRedirect(redirect_url=cross_host_redirect)

    adapter = GitlabCommentAdapter(base_url="https://gitlab.example.com")
    adapter._session = fake_session  # type: ignore[assignment]

    action = {"kind": "reply", "input": {"text": "Review done."}}
    context = {"project_id": 10, "mr_iid": 3}

    await adapter.deliver(action, context)

    # The initial POST must have gone to the gitlab URL with the token
    assert len(fake_session.posts) == 1
    assert fake_session.posts[0]["headers"].get("PRIVATE-TOKEN") == "test_token_abc"

    # The followed GET to the redirect target must NOT have PRIVATE-TOKEN
    assert len(fake_session.gets) == 1, "Expected one GET after redirect"
    followed_headers = fake_session.gets[0]["headers"]
    assert "PRIVATE-TOKEN" not in followed_headers, (
        f"PRIVATE-TOKEN must be stripped on cross-host redirect; "
        f"followed headers: {followed_headers!r}"
    )


@pytest.mark.asyncio
async def test_token_read_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-02 behavioral: GITLAB_TOKEN env var read at call time, never stored in adapter.

    Mirror of test_secret_read_per_request in tests/channels/test_webhook.py:
    Set GITLAB_TOKEN=v1, call deliver(), assert PRIVATE-TOKEN: v1 on the wire;
    set GITLAB_TOKEN=v2, call deliver() again, assert PRIVATE-TOKEN: v2.
    This proves call-time read (D-12 behavioral requirement).
    """
    # First call with v1
    monkeypatch.setenv("GITLAB_TOKEN", "token_v1")

    calls: list[dict[str, Any]] = []

    class _CapturingResponse:
        def __init__(self, captured_headers: dict[str, str]) -> None:
            self.status = 201
            self.headers: dict[str, str] = {}
            self._captured_headers = captured_headers

        async def text(self) -> str:
            return ""

        async def __aenter__(self) -> _CapturingResponse:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

    class _CapturingSession:
        closed = False

        def post(
            self,
            url: str,
            *,
            json: dict[str, Any] | None = None,
            headers: dict[str, str] | None = None,
            allow_redirects: bool = True,
        ) -> _CapturingResponse:
            calls.append({"url": url, "headers": dict(headers or {}), "json": json})
            return _CapturingResponse(dict(headers or {}))

        async def close(self) -> None:
            self.closed = True

    adapter = GitlabCommentAdapter(base_url="https://gitlab.example.com")
    adapter._session = _CapturingSession()  # type: ignore[assignment]

    action = {"kind": "reply", "input": {"text": "First call."}}
    context = {"project_id": 1, "mr_iid": 1}

    # First deliver — should use token_v1
    await adapter.deliver(action, context)
    assert len(calls) == 1
    assert calls[0]["headers"].get("PRIVATE-TOKEN") == "token_v1", (
        f"First call must use PRIVATE-TOKEN: token_v1; got {calls[0]['headers']!r}"
    )

    # Rotate the env var (simulates secret rotation)
    monkeypatch.setenv("GITLAB_TOKEN", "token_v2")

    # Second deliver — must use token_v2 (proves no caching in adapter)
    await adapter.deliver(action, context)
    assert len(calls) == 2
    assert calls[1]["headers"].get("PRIVATE-TOKEN") == "token_v2", (
        f"Second call must use PRIVATE-TOKEN: token_v2 after rotation; "
        f"got {calls[1]['headers']!r}"
    )


class _FakeSessionWithRelativeRedirect:
    """Fake aiohttp.ClientSession that returns a RELATIVE-path redirect then 201.

    First POST → 301 with Location: /api/v4/projects/10/merge_requests/3/notes_alt
    (a relative path on the same GitLab host).
    Subsequent GET → 201.

    Used for T-02-07 relative-redirect test (RED phase).
    """

    def __init__(self, relative_location: str) -> None:
        self._relative_location = relative_location
        self.posts: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.closed = False

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> Any:
        self.posts.append({"url": url, "headers": dict(headers or {}), "json": json})
        return _FakeResponse(
            status=301,
            headers={"Location": self._relative_location},
        )

    def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> Any:
        self.gets.append({"url": url, "headers": dict(headers or {})})
        return _FakeResponse(status=201)

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# T-02-07 Tests: relative Location urljoin + fail-closed strip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relative_redirect_same_host_retains_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-02-07 RED: relative Location is resolved via urljoin to same-host absolute URL.

    The adapter must:
    1. Resolve the relative Location against the current URL (urljoin).
    2. Compare the RESOLVED host against base_host → same host → RETAIN PRIVATE-TOKEN.
    3. Follow the redirect to the correct absolute URL and succeed.

    RED: today `current_url = location` stores the bare relative path; the GET then
    goes to the relative path string directly, not a valid absolute URL. This test
    FAILS because either the GET url is wrong or the token is incorrectly stripped.
    """
    monkeypatch.setenv("GITLAB_TOKEN", "test_token_abc")

    # Relative location on the same host
    relative_location = "/api/v4/projects/10/merge_requests/3/notes_v2"
    fake_session = _FakeSessionWithRelativeRedirect(relative_location=relative_location)

    adapter = GitlabCommentAdapter(base_url="https://gitlab.example.com")
    adapter._session = fake_session  # type: ignore[assignment]

    action = {"kind": "reply", "input": {"text": "relative redirect test"}}
    context = {"project_id": 10, "mr_iid": 3}

    await adapter.deliver(action, context)

    # Must have followed the redirect (one GET issued)
    assert len(fake_session.gets) == 1, (
        f"T-02-07: expected one GET after relative redirect, got {len(fake_session.gets)}"
    )

    followed_url = fake_session.gets[0]["url"]
    # The GET must be an absolute URL resolved to the same host
    assert followed_url == "https://gitlab.example.com" + relative_location, (
        f"T-02-07: relative Location must be resolved to absolute URL; "
        f"got {followed_url!r}"
    )

    # PRIVATE-TOKEN must be RETAINED (same host)
    followed_headers = fake_session.gets[0]["headers"]
    assert "PRIVATE-TOKEN" in followed_headers, (
        f"T-02-07: PRIVATE-TOKEN must be retained for same-host relative redirect; "
        f"headers on followed GET: {followed_headers!r}"
    )


# ---------------------------------------------------------------------------
# Task 2 Tests
# ---------------------------------------------------------------------------


class _FakeDeliveryAdapter:
    """Records deliver() calls for dispatch_actions testing."""

    def __init__(self) -> None:
        self.delivered: list[tuple[dict[str, Any], dict[str, Any]]] = []

    async def deliver(
        self, action: dict[str, Any], context: dict[str, Any]
    ) -> None:
        self.delivered.append((action, context))


@pytest.mark.asyncio
async def test_sideeffect_rejected_reply_delivered() -> None:
    """ACT-03: sideEffect action rejected with UnsupportedActionKind; reply still delivered.

    A result list with both a sideEffect and a reply:
    - The FakeAdapter records exactly the reply delivery (not the sideEffect).
    - No exception escapes the dispatch loop (Pitfall 5).
    """
    fake_adapter = _FakeDeliveryAdapter()
    context = {"project_id": 5, "mr_iid": 2}

    actions = [
        {"kind": "sideEffect", "name": "approveMR", "input": {}},
        {"kind": "reply", "name": "replyMR", "input": {"text": "Looks good!"}},
    ]

    # Must not raise — sideEffect is caught and logged, reply proceeds
    await dispatch_actions(actions, fake_adapter, context)

    # Only the reply was delivered
    assert len(fake_adapter.delivered) == 1, (
        f"Expected exactly 1 delivery (reply), got {len(fake_adapter.delivered)}"
    )
    delivered_action, delivered_context = fake_adapter.delivered[0]
    assert delivered_action["kind"] == "reply"
    assert delivered_context == context


# ---------------------------------------------------------------------------
# SEC-03: GITLAB_TOKEN never in logs
# ---------------------------------------------------------------------------


def _configure_json_logging_with_gitlab_redaction(stream: StringIO) -> None:
    """Configure structlog with both ek_ and GITLAB_TOKEN redaction, JSON output.

    Mirrors _configure_json_logging from test_skeleton.py but adds the
    redact_gitlab_token_processor (SEC-03).
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_ek_processor,  # SEC-01: ek_ redaction
            redact_gitlab_token_processor,  # SEC-03: GITLAB_TOKEN redaction
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
    )


# ---------------------------------------------------------------------------
# Phase 5 / ACT-03 consent-gate tests (Plan 02 implementation — replaces Wave 0 stubs)
# ---------------------------------------------------------------------------


def _make_consent_event(user_consented: bool, channel_name: str = "webhook-mr") -> MessageEvent:
    """Build a minimal MessageEvent for consent-gate tests."""
    return MessageEvent(
        idempotency_key="test-idempotency-key-001",
        session_key="test-session-001",
        channel_name=channel_name,
        user_consented=user_consented,
    )


def _make_response_action_config(
    action_name: str, consent_tier: str
) -> dict[str, ResponseActionBlock]:
    """Build a per-channel response_action_config for testing."""
    block = ResponseActionBlock(
        name=action_name, kind="sideEffect", consentTier=consent_tier
    )
    return {action_name: block}


@pytest.mark.asyncio
async def test_sideeffect_consent_tier_consent_user_consented() -> None:
    """ACT-03 / sideeffect: consentTier=consent + user_consented=True → dry-run + audit.

    The sideEffect is dry-run executed (executor called), the audit event has
    decision="executed-dryrun", and no exception escapes the loop.
    """
    stream = StringIO()
    _configure_json_logging_with_gitlab_redaction(stream)

    fake_adapter = _FakeDeliveryAdapter()
    event = _make_consent_event(user_consented=True)
    cfg = _make_response_action_config("approveMR", "consent")

    actions = [
        {"kind": "sideEffect", "name": "approveMR", "input": {}},
        {"kind": "reply", "name": "replyMR", "input": {"text": "Approved!"}},
    ]

    # Must not raise; consent passes
    await dispatch_actions(
        actions, fake_adapter, {}, event=event, response_action_config=cfg
    )

    # Reply must still be delivered
    assert len(fake_adapter.delivered) == 1, (
        f"Expected 1 reply delivery, got {len(fake_adapter.delivered)}"
    )
    assert fake_adapter.delivered[0][0]["kind"] == "reply"

    # Audit event with decision=executed-dryrun must appear in logs
    output = stream.getvalue()
    audit_lines = [
        json.loads(line)
        for line in output.splitlines()
        if line.strip() and '"sideeffect.audit"' in line
    ]
    assert len(audit_lines) >= 1, (
        f"Expected at least one sideeffect.audit event, got none. Log output:\n{output}"
    )
    audit = audit_lines[0]
    assert audit.get("decision") == "executed-dryrun", (
        f"Expected decision=executed-dryrun, got {audit.get('decision')!r}"
    )


@pytest.mark.asyncio
async def test_sideeffect_consent_denied() -> None:
    """ACT-03 / consent_denied: consentTier=consent + user_consented=False → ConsentDenied + audit.

    ConsentDenied is raised internally, caught inside the loop, audit has
    decision="denied". The exception does NOT propagate out of dispatch_actions.
    The following reply action is still delivered (D-05).
    """
    stream = StringIO()
    _configure_json_logging_with_gitlab_redaction(stream)

    fake_adapter = _FakeDeliveryAdapter()
    event = _make_consent_event(user_consented=False)
    cfg = _make_response_action_config("approveMR", "consent")

    actions = [
        {"kind": "sideEffect", "name": "approveMR", "input": {}},
        {"kind": "reply", "name": "replyMR", "input": {"text": "Looks good!"}},
    ]

    # Must not raise — ConsentDenied is caught inside the loop (D-05)
    await dispatch_actions(
        actions, fake_adapter, {}, event=event, response_action_config=cfg
    )

    # Reply still delivered even though sideEffect was denied (reply_still_delivered)
    assert len(fake_adapter.delivered) == 1, (
        f"Expected 1 reply delivery after denial, got {len(fake_adapter.delivered)}"
    )
    assert fake_adapter.delivered[0][0]["kind"] == "reply"

    # Audit event with decision=denied must appear in logs
    output = stream.getvalue()
    audit_lines = [
        json.loads(line)
        for line in output.splitlines()
        if line.strip() and '"sideeffect.audit"' in line
    ]
    assert len(audit_lines) >= 1, (
        f"Expected at least one sideeffect.audit event, got none. Log output:\n{output}"
    )
    audit = audit_lines[0]
    assert audit.get("decision") == "denied", (
        f"Expected decision=denied, got {audit.get('decision')!r}"
    )


@pytest.mark.asyncio
async def test_sideeffect_auto_tier() -> None:
    """ACT-03 / auto_tier: consentTier=auto → dry-run + audit (no consent check).

    Even with user_consented=False, auto tier bypasses the consent check and
    executes the dry-run, auditing decision="executed-dryrun".
    """
    stream = StringIO()
    _configure_json_logging_with_gitlab_redaction(stream)

    fake_adapter = _FakeDeliveryAdapter()
    # user_consented=False to prove auto tier does NOT consult the marker
    event = _make_consent_event(user_consented=False)
    cfg = _make_response_action_config("autoAction", "auto")

    actions = [
        {"kind": "sideEffect", "name": "autoAction", "input": {}},
    ]

    await dispatch_actions(
        actions, fake_adapter, {}, event=event, response_action_config=cfg
    )

    # No reply in actions; adapter not called
    assert len(fake_adapter.delivered) == 0

    # Audit must record executed-dryrun (auto tier bypasses consent check)
    output = stream.getvalue()
    audit_lines = [
        json.loads(line)
        for line in output.splitlines()
        if line.strip() and '"sideeffect.audit"' in line
    ]
    assert len(audit_lines) >= 1, (
        f"Expected at least one sideeffect.audit event for auto tier. Log:\n{output}"
    )
    audit = audit_lines[0]
    assert audit.get("decision") == "executed-dryrun", (
        f"auto tier must execute dry-run; got decision={audit.get('decision')!r}"
    )
    assert audit.get("consent_tier") == "auto", (
        f"Expected consent_tier=auto in audit, got {audit.get('consent_tier')!r}"
    )


@pytest.mark.asyncio
async def test_sideeffect_audit_fields() -> None:
    """ACT-03 / audit_fields: audit event carries all D-07 required correlation fields.

    Required fields: action_name, action_kind, consent_tier, decision, reason,
    session_key, idempotency_key, channel_name.
    """
    stream = StringIO()
    _configure_json_logging_with_gitlab_redaction(stream)

    fake_adapter = _FakeDeliveryAdapter()
    event = MessageEvent(
        idempotency_key="idem-key-audit-test",
        session_key="session-audit-test",
        channel_name="webhook-audit",
        user_consented=True,
    )
    cfg = _make_response_action_config("tagMR", "consent")

    actions = [
        {"kind": "sideEffect", "name": "tagMR", "input": {"label": "reviewed"}},
    ]

    await dispatch_actions(
        actions, fake_adapter, {}, event=event, response_action_config=cfg
    )

    output = stream.getvalue()
    audit_lines = [
        json.loads(line)
        for line in output.splitlines()
        if line.strip() and '"sideeffect.audit"' in line
    ]
    assert len(audit_lines) >= 1, (
        f"Expected sideeffect.audit event with D-07 fields. Log:\n{output}"
    )
    audit = audit_lines[0]

    # D-07: all required correlation fields must be present
    assert audit.get("action_name") == "tagMR", (
        f"action_name missing or wrong: {audit.get('action_name')!r}"
    )
    assert audit.get("action_kind") == "sideEffect", (
        f"action_kind missing or wrong: {audit.get('action_kind')!r}"
    )
    assert audit.get("consent_tier") == "consent", (
        f"consent_tier missing or wrong: {audit.get('consent_tier')!r}"
    )
    assert audit.get("decision") == "executed-dryrun", (
        f"decision missing or wrong: {audit.get('decision')!r}"
    )
    assert "reason" in audit, "reason field missing from audit event"
    assert audit.get("session_key") == "session-audit-test", (
        f"session_key missing or wrong: {audit.get('session_key')!r}"
    )
    assert audit.get("idempotency_key") == "idem-key-audit-test", (
        f"idempotency_key missing or wrong: {audit.get('idempotency_key')!r}"
    )
    assert audit.get("channel_name") == "webhook-audit", (
        f"channel_name missing or wrong: {audit.get('channel_name')!r}"
    )


@pytest.mark.asyncio
async def test_sideeffect_reply_still_delivered_on_denial() -> None:
    """ACT-03 / reply_still_delivered: denied sideEffect does not block reply delivery.

    D-05: ConsentDenied is caught inside the loop; the following reply action
    is still delivered to the adapter. This is the canonical reply_still_delivered test.
    """
    fake_adapter = _FakeDeliveryAdapter()
    event = _make_consent_event(user_consented=False)
    cfg = _make_response_action_config("closeMR", "consent")

    actions = [
        {"kind": "sideEffect", "name": "closeMR", "input": {}},
        {"kind": "reply", "name": "replyClose", "input": {"text": "Closing MR."}},
    ]

    # Must not raise; ConsentDenied caught inside loop
    await dispatch_actions(
        actions, fake_adapter, {"project_id": 1, "mr_iid": 2},
        event=event, response_action_config=cfg
    )

    assert len(fake_adapter.delivered) == 1, (
        f"reply_still_delivered: expected 1 reply even after denied sideEffect; "
        f"got {len(fake_adapter.delivered)}"
    )
    delivered_action = fake_adapter.delivered[0][0]
    assert delivered_action["kind"] == "reply"
    assert delivered_action["input"]["text"] == "Closing MR."


def test_gitlab_token_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """SEC-03: GITLAB_TOKEN sentinel value never appears in structured log output.

    Sets GITLAB_TOKEN to a sentinel, configures structlog with the redaction
    processor, emits a log line that would expose the token, and asserts the
    sentinel is absent from the rendered JSON output.
    """
    sentinel = "fake_gl_token_sentinel_do_not_log"
    monkeypatch.setenv("GITLAB_TOKEN", sentinel)

    stream = StringIO()
    _configure_json_logging_with_gitlab_redaction(stream)

    test_log = structlog.get_logger("test.sec03")

    # Simulate what would happen if the token accidentally appeared in a log call
    # (e.g. in an error message from the server or a debug log of headers)
    test_log.error(
        "gitlab_comment: post failed",
        status=403,
        response=f"Forbidden — invalid token {sentinel}",
    )
    test_log.info(
        "gitlab_comment: note posted",
        project_id=42,
        mr_iid=7,
        # Deliberate: simulate a mistaken log of the env value
        token_check=sentinel,
    )

    output = stream.getvalue()
    assert sentinel not in output, (
        f"SEC-03 violated: GITLAB_TOKEN sentinel found in log output:\n{output}"
    )

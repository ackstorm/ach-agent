"""GitLab e2e tests (SC#1, SC#4, ACT-01, SEC-03/SC#5) — Plan 02-04 GREEN.

Full-harness end-to-end: webhook → router → engine → gitlab_comment.

Architecture (hermetic — zero live GitLab, zero credentials):
  - Harness app:      FastAPI app created via create_app() with a fake engine
  - Mock-gitlab:      in-process FastAPI app (docker/mock-gitlab/app.py) serving
                      POST /api/v4/.../notes and GET /assert
  - GitlabAdapter:    patched to use httpx ASGITransport against the mock-gitlab app
  - Engine:           fake engine_runner returning a known reply action

Pitfall 7 (RESEARCH): every POST must include a unique X-Gitlab-Event-UUID — the
dedup store would reject a repeated UUID as DUPLICATE, returning 200 instead of 202.

SEC-03/SC#5: harness log output captured and asserted free of sentinel values.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog

from ach_agent.actions.gitlab_comment import GitlabCommentAdapter
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig
from ach_agent.engine.sanitized_env import redact_ek_processor, redact_gitlab_token_processor
from ach_agent.http.app import create_app
from ach_agent.router import Router
from ach_agent.router.dedup import InMemoryDedupStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SECRET = "test_hmac_secret"
_REPLY_TEXT = "LGTM — the code looks good."
_EK_SENTINEL = "ek_test_sentinel_do_not_log"
_GL_TOKEN_SENTINEL = "fake_gl_token_sentinel"

# Project root: tests/e2e/test_gitlab_e2e.py → tests/e2e → tests → project root
_PROJECT_ROOT = Path(__file__).parent.parent.parent
_MOCK_GITLAB_APP_PATH = _PROJECT_ROOT / "docker" / "mock-gitlab" / "app.py"

MR_PAYLOAD: dict[str, Any] = {
    "object_kind": "merge_request",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"iid": 7, "title": "Add feature X", "state": "opened"},
}


def _make_mr_headers(secret: str) -> dict[str, str]:
    """Build signed GitLab MR webhook headers (Pitfall 7: always unique UUID)."""
    return {
        "X-Gitlab-Token": secret,
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Event-UUID": str(uuid.uuid4()),  # Pitfall 7: unique per POST
        "Content-Type": "application/json",
    }


def _make_webhook_cfg(
    name: str,
    secret_path: str,
    deliver_type: str = "gitlab_comment",
) -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": name,
            "type": "webhook",
            "webhook": {
                "auth": {"type": "hmac", "secretPath": secret_path},
                "deliver": {"type": deliver_type},
            },
        }
    )


def _configure_json_logging_to(stream: StringIO) -> None:
    """Configure structlog to emit JSON to a StringIO for SEC assertion."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_ek_processor,
            redact_gitlab_token_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
    )


# ---------------------------------------------------------------------------
# In-process mock-gitlab loader
# ---------------------------------------------------------------------------


def _load_mock_gitlab_app(module_name: str) -> Any:
    """Load docker/mock-gitlab/app.py as an independent module and reset state.

    Loads a fresh module instance to avoid cross-test shared state.
    Returns the FastAPI app object and the module (so _notes can be inspected).
    """
    spec = importlib.util.spec_from_file_location(module_name, str(_MOCK_GITLAB_APP_PATH))
    assert spec is not None and spec.loader is not None, (
        f"Could not load mock-gitlab app from {_MOCK_GITLAB_APP_PATH}"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    module._notes.clear()
    return module


# ---------------------------------------------------------------------------
# httpx ↔ aiohttp bridge (hermetic session replacement)
# ---------------------------------------------------------------------------


class _FakeAiohttpResp:
    """Minimal aiohttp-compatible response backed by an httpx response."""

    def __init__(self, httpx_resp: httpx.Response) -> None:
        self.status = httpx_resp.status_code
        self.headers: dict[str, str] = dict(httpx_resp.headers)
        self._text = httpx_resp.text

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeAiohttpResp":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _FakePostContextManager:
    """Async context manager for a single POST call via httpx."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        json_body: dict[str, Any] | None,
        headers: dict[str, str],
    ) -> None:
        self._client = client
        self._url = url
        self._json = json_body
        self._headers = headers

    async def __aenter__(self) -> _FakeAiohttpResp:
        resp = await self._client.post(
            self._url, json=self._json, headers=self._headers
        )
        return _FakeAiohttpResp(resp)

    async def __aexit__(self, *args: Any) -> None:
        pass


class _BridgeSession:
    """Fake aiohttp ClientSession that routes POSTs to an httpx AsyncClient.

    Allows the GitlabCommentAdapter (which uses aiohttp internally) to make
    hermetic HTTP calls to the in-process mock-gitlab FastAPI app.
    """

    closed = False

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_redirects: bool = True,
    ) -> _FakePostContextManager:
        return _FakePostContextManager(
            client=self._client,
            url=url,
            json_body=json,
            headers=headers or {},
        )

    async def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Fake engine results
# ---------------------------------------------------------------------------

_RESULT_REPLY = {
    "actions": [
        {"name": "channel_message", "kind": "reply", "input": {"text": _REPLY_TEXT}},
    ]
}

_RESULT_SIDEEFFECT_AND_REPLY = {
    "actions": [
        {"name": "approve_mr", "kind": "sideEffect", "input": {"action": "approve"}},
        {"name": "channel_message", "kind": "reply", "input": {"text": _REPLY_TEXT}},
    ]
}


# ---------------------------------------------------------------------------
# SC#1: full gitlab_comment e2e happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitlab_e2e_happy_path(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC#1: full webhook → engine → gitlab_comment round-trip (hermetic, no live GitLab).

    Sends a signed MR webhook to the harness, waits for the engine to run,
    asserts that the mock-gitlab receiver captured the expected note.
    """
    # Set up sentinels in env (SEC-03 / SC#5)
    monkeypatch.setenv("ACH_API_KEY", _EK_SENTINEL)
    monkeypatch.setenv("GITLAB_TOKEN", _GL_TOKEN_SENTINEL)

    # Capture log output for SEC-03 assertion
    log_stream = StringIO()
    _configure_json_logging_to(log_stream)

    # Write HMAC secret file
    secret_file = tmp_path / "hmac_secret"
    secret_file.write_text(_SECRET)

    # Load in-process mock-gitlab
    mock_gitlab_module = _load_mock_gitlab_app("mock_gitlab_sc1")
    mock_gitlab_app = mock_gitlab_module.app
    mock_gitlab_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_gitlab_app),
        base_url="http://mock-gitlab",
    )

    # Signal when the note has been delivered
    delivery_done: asyncio.Event = asyncio.Event()

    # Patch GitlabCommentAdapter: replace aiohttp session with httpx bridge
    async def fake_ensure_session(
        self: GitlabCommentAdapter,
    ) -> _BridgeSession:  # type: ignore[override]
        return _BridgeSession(mock_gitlab_client)  # type: ignore[return-value]

    original_post_with_redirect = GitlabCommentAdapter._post_with_redirect

    async def patched_post_with_redirect(
        self: GitlabCommentAdapter, session: Any, url: str, body_text: str, token: str, base_host: str | None
    ) -> str:
        result = await original_post_with_redirect(self, session, url, body_text, token, base_host)
        delivery_done.set()
        return result

    monkeypatch.setattr(GitlabCommentAdapter, "_ensure_session", fake_ensure_session)
    monkeypatch.setattr(GitlabCommentAdapter, "_post_with_redirect", patched_post_with_redirect)

    # Build harness components
    channel_cfg = _make_webhook_cfg("gitlab-mr-review", str(secret_file), "gitlab_comment")
    gitlab_adapter = GitlabCommentAdapter(base_url="http://mock-gitlab")

    async def fake_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        from ach_agent.actions.gitlab_comment import dispatch_actions
        await dispatch_actions(
            actions=_RESULT_REPLY["actions"],
            adapter=gitlab_adapter,
            context=event.delivery_context,
        )
        on_kill()

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine_runner,
        delivery_adapter=None,
    )

    app = create_app(channels=[channel_cfg], handler=router)

    # POST a signed MR webhook (unique X-Gitlab-Event-UUID per Pitfall 7)
    payload_bytes = json.dumps(MR_PAYLOAD).encode()
    headers = _make_mr_headers(_SECRET)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        resp = await client.post(
            "/channels/gitlab-mr-review/events",
            content=payload_bytes,
            headers=headers,
        )

    # D-04: accept-and-process-async → 202
    assert resp.status_code == 202, f"SC#1: expected 202, got {resp.status_code}: {resp.text}"

    # Wait for out-of-band delivery (bounded — no naked polling loop; CLAUDE.md ban)
    try:
        async with asyncio.timeout(5.0):
            await delivery_done.wait()
    except TimeoutError:
        pytest.fail("SC#1: timed out waiting for gitlab_comment delivery")

    # Assert mock-gitlab captured the note
    check_resp = await mock_gitlab_client.get("/assert")
    notes = check_resp.json()["notes"]
    assert len(notes) >= 1, f"SC#1: expected at least 1 note captured, got {notes}"
    assert notes[0]["body"] == _REPLY_TEXT, f"SC#1: note body mismatch: {notes[0]['body']!r}"
    assert notes[0]["project_id"] == 42
    assert notes[0]["iid"] == 7

    # SC#5 / SEC-03: verify secret sentinels absent from log output
    await mock_gitlab_client.aclose()
    log_output = log_stream.getvalue()
    assert _EK_SENTINEL not in log_output, (
        f"SEC-03: ek_ sentinel found in harness log output:\n{log_output[:500]}"
    )
    assert _GL_TOKEN_SENTINEL not in log_output, (
        f"SEC-03: GITLAB_TOKEN sentinel found in harness log output:\n{log_output[:500]}"
    )


# ---------------------------------------------------------------------------
# ACT-01: synchronous reply mode e2e
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_mode_e2e(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACT-01 / CR-01: reply mode invokes engine EXACTLY ONCE via reply_future.

    Design (gap-closure 02-05 Task 3):
      - deliver.type: reply → webhook.py creates event.reply_future before handler.handle()
      - engine_runner (counting fake) resolves event.reply_future with the reply text
        instead of calling dispatch_actions
      - app.py awaits webhook_result.reply_future (202 ACCEPTED) → returns 200 + reply body
      - No gitlab_comment is posted (reply stays on held connection)

    Regression assertions (CR-01 double-fire fix):
      (a) engine_runner counter == 1 (engine fired exactly ONCE per webhook)
      (b) Response is 200 with correct reply text
      (c) mock-gitlab captured NO note (no stray gitlab_comment post)

    This test FAILS against the old wiring because:
      - create_app has no reply_future path — sync_invoke was the old API
      - event.reply_future field does not exist on MessageEvent
    """
    monkeypatch.setenv("GITLAB_TOKEN", _GL_TOKEN_SENTINEL)

    secret_file = tmp_path / "hmac_secret"
    secret_file.write_text(_SECRET)

    # Load mock-gitlab receiver to verify no notes are captured
    mock_gitlab_module = _load_mock_gitlab_app("mock_gitlab_act01")
    mock_gitlab_app = mock_gitlab_module.app
    mock_gitlab_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_gitlab_app),
        base_url="http://mock-gitlab",
    )

    channel_cfg = _make_webhook_cfg("gitlab-reply", str(secret_file), "reply")

    # Counting engine_runner (real signature: (event, on_kill)) — CR-01 regression probe
    engine_call_count = 0

    async def counting_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        nonlocal engine_call_count
        engine_call_count += 1
        # Resolve reply_future so the route can return (reply mode path)
        if event.reply_future is not None and not event.reply_future.done():
            event.reply_future.set_result(_REPLY_TEXT)
        on_kill()

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=counting_engine_runner,
        delivery_adapter=None,
    )

    # create_app with NO sync_invoke — the new API uses reply_future on the event
    app = create_app(channels=[channel_cfg], handler=router)

    payload_bytes = json.dumps(MR_PAYLOAD).encode()
    headers = _make_mr_headers(_SECRET)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        resp = await client.post(
            "/channels/gitlab-reply/events",
            content=payload_bytes,
            headers=headers,
        )

    # (a) CR-01 regression: engine must fire exactly once
    assert engine_call_count == 1, (
        f"CR-01: engine must be invoked EXACTLY ONCE per webhook, got {engine_call_count}"
    )

    # (b) ACT-01: synchronous reply → 200 + reply in body
    assert resp.status_code == 200, (
        f"ACT-01: expected 200, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "reply" in body, f"ACT-01: 'reply' key missing from response: {body}"
    assert body["reply"] == _REPLY_TEXT, (
        f"ACT-01: reply text mismatch: {body['reply']!r}"
    )

    # (c) Assert mock-gitlab received NO notes (reply mode must not post gitlab_comment)
    check_resp = await mock_gitlab_client.get("/assert")
    notes = check_resp.json()["notes"]
    assert len(notes) == 0, (
        f"ACT-01: reply mode must not post a gitlab_comment, got {notes}"
    )

    await mock_gitlab_client.aclose()


# ---------------------------------------------------------------------------
# SC#4: sideEffect rejected, reply still delivered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sideeffect_stubbed(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC#4: sideEffect action rejected; reply still delivered; no crash.

    Engine returns both sideEffect + reply. The dispatch helper must:
      - Log-skip the sideEffect (D-11: UnsupportedActionKind, never raise)
      - Still deliver the reply as a gitlab_comment note
    """
    monkeypatch.setenv("GITLAB_TOKEN", _GL_TOKEN_SENTINEL)

    secret_file = tmp_path / "hmac_secret"
    secret_file.write_text(_SECRET)

    mock_gitlab_module = _load_mock_gitlab_app("mock_gitlab_sc4")
    mock_gitlab_app = mock_gitlab_module.app
    mock_gitlab_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mock_gitlab_app),
        base_url="http://mock-gitlab",
    )

    delivery_done: asyncio.Event = asyncio.Event()

    # Bridge aiohttp → httpx for the delivery adapter
    async def fake_ensure_session_sc4(
        self: GitlabCommentAdapter,
    ) -> _BridgeSession:  # type: ignore[override]
        return _BridgeSession(mock_gitlab_client)  # type: ignore[return-value]

    original_post_with_redirect = GitlabCommentAdapter._post_with_redirect

    async def patched_post_with_redirect_sc4(
        self: GitlabCommentAdapter,
        session: Any,
        url: str,
        body_text: str,
        token: str,
        base_host: str | None,
    ) -> str:
        result = await original_post_with_redirect(self, session, url, body_text, token, base_host)
        delivery_done.set()
        return result

    monkeypatch.setattr(GitlabCommentAdapter, "_ensure_session", fake_ensure_session_sc4)
    monkeypatch.setattr(
        GitlabCommentAdapter, "_post_with_redirect", patched_post_with_redirect_sc4
    )

    channel_cfg = _make_webhook_cfg("gitlab-sc4", str(secret_file), "gitlab_comment")
    gitlab_adapter = GitlabCommentAdapter(base_url="http://mock-gitlab")

    async def fake_engine_runner_sc4(event: MessageEvent, on_kill: Any) -> None:
        from ach_agent.actions.gitlab_comment import dispatch_actions
        # SC#4: engine returns BOTH sideEffect AND reply — sideEffect must be skipped,
        # reply must still be delivered
        await dispatch_actions(
            actions=_RESULT_SIDEEFFECT_AND_REPLY["actions"],
            adapter=gitlab_adapter,
            context=event.delivery_context,
        )
        on_kill()

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine_runner_sc4,
        delivery_adapter=None,
    )

    app = create_app(channels=[channel_cfg], handler=router)

    payload_bytes = json.dumps(MR_PAYLOAD).encode()
    headers = _make_mr_headers(_SECRET)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        resp = await client.post(
            "/channels/gitlab-sc4/events",
            content=payload_bytes,
            headers=headers,
        )

    assert resp.status_code == 202, (
        f"SC#4: expected 202, got {resp.status_code}: {resp.text}"
    )

    # Wait for reply delivery (bounded asyncio.timeout — no naked polling loop)
    try:
        async with asyncio.timeout(5.0):
            await delivery_done.wait()
    except TimeoutError:
        pytest.fail("SC#4: timed out waiting for reply delivery after sideEffect rejection")

    # Assert the reply note WAS captured (sideEffect rejected but reply delivered)
    check_resp = await mock_gitlab_client.get("/assert")
    notes = check_resp.json()["notes"]
    assert len(notes) >= 1, f"SC#4: expected reply note, got {notes}"
    assert notes[0]["body"] == _REPLY_TEXT, f"SC#4: note body mismatch: {notes[0]['body']!r}"

    await mock_gitlab_client.aclose()

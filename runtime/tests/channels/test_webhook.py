# SPDX-License-Identifier: MIT
"""Legacy generic webhook behavior translated to the current Event contract."""

from __future__ import annotations

import asyncio
import gc
import weakref
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from ach_runtime.channels.webhook import normalize_webhook
from ach_runtime.config.schema import ChannelConfig, WebhookAuthBlock
from ach_runtime.harness.admission import Admission
from ach_runtime.harness.app import create_app


def _channel(source: str = "generic", *, auth: WebhookAuthBlock | None = None) -> ChannelConfig:
    return ChannelConfig.model_validate({
        "name": "hooks",
        "type": "webhook",
        "source": source,
        "webhook": {"auth": (auth or WebhookAuthBlock(type="none")).model_dump(by_alias=True)},
        "routing": {"sessionKey": "main"},
    })


@pytest.mark.asyncio
async def test_generic_stable_id_required_and_priority() -> None:
    channel = _channel()
    headers = {
        "X-GitHub-Delivery": "github-id",
        "X-Gitlab-Event-UUID": "gitlab-id",
        "svix-id": "svix-id",
        "X-Request-ID": "request-id",
        "Idempotency-Key": "idempotency-id",
    }
    event = normalize_webhook({"action": "created"}, headers, channel)
    assert event is not None
    assert event.event_id == "github-id"
    assert event.type == "webhook"
    assert event.source == "generic"
    assert event.context == {}

    for expected, names in [
        ("gitlab-id", ("X-Gitlab-Event-UUID", "svix-id", "X-Request-ID", "Idempotency-Key")),
        ("svix-id", ("svix-id", "X-Request-ID", "Idempotency-Key")),
        ("request-id", ("X-Request-ID", "Idempotency-Key")),
        ("idempotency-id", ("Idempotency-Key",)),
    ]:
        lower_priority = {name: headers[name] for name in names}
        event = normalize_webhook({"action": "created"}, lower_priority, channel)
        assert event is not None
        assert event.event_id == expected

    with pytest.raises(ValueError, match="stable"):
        normalize_webhook({}, {}, channel)

    # The current admission ledger uses the normalized stable ID as its dedup key.
    first = normalize_webhook({}, {"Idempotency-Key": "same-delivery"}, channel)
    repeated = normalize_webhook({}, {"Idempotency-Key": "same-delivery"}, channel)
    assert first is not None and repeated is not None
    from ach_runtime.channels.routing import route

    admission = Admission(
        namespace="ns", agent_uid="3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77",
        max_concurrent_invocations=1, max_queued_total=4, idempotency_window_seconds=60,
        max_invocation_seconds=20, workspace_max_concurrent_sessions=1,
        channel_concurrency={"hooks": 1}, clock=lambda: datetime.now(timezone.utc), runner=_hold_runner,
    )
    app = create_app(SimpleNamespace(config=SimpleNamespace(channels=[channel], agent=SimpleNamespace(name="test-agent"), identity_environment="test-env"), admission=admission, _stopping=False))
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        missing_id = await client.post("/channels/hooks/events", json={"action": "created"})
    assert missing_id.status_code == 422
    assert admission.queued_total == 0

    ticket_a = admission.admit(route(first, channel))
    ticket_b = admission.admit(route(repeated, channel))
    assert ticket_a is ticket_b
    assert admission.queued_total == 1
    await admission.close()


async def _hold_runner(event, ticket) -> None:
    ticket.mark_started()
    await asyncio.Future()


def _deferred_app():
    channel = _channel()
    config = SimpleNamespace(channels=[channel], agent=SimpleNamespace(name="test-agent"), identity_environment="test-env")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def runner(event, ticket):
        entered.set()
        await release.wait()
        ticket.mark_started()

    admission = Admission(
        namespace="ns", agent_uid="3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77",
        max_concurrent_invocations=1, max_queued_total=1, idempotency_window_seconds=60,
        max_invocation_seconds=20, workspace_max_concurrent_sessions=1,
        channel_concurrency={"hooks": 1}, clock=lambda: datetime.now(timezone.utc), runner=runner,
    )
    control = SimpleNamespace(config=config, admission=admission, _stopping=False)
    return create_app(control), entered, release, admission


@pytest.mark.asyncio
async def test_webhook_ack_on_admission_does_not_wait_for_started() -> None:
    app, entered, release, admission = _deferred_app()
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        post = asyncio.create_task(client.post(
            "/channels/hooks/events", headers={"Idempotency-Key": "delivery"}, json={"x": 1}
        ))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            response = await asyncio.wait_for(post, 1)
            ticket = next(iter(admission._tickets.values()))
            assert not ticket.started.done()
        finally:
            release.set()
            await admission.close()
    assert response.status_code == 202
    assert response.json()["status"] == "accepted"
    assert response.json()["task_id"] == response.headers["x-ach-task-id"]
    assert len(response.json()["task_id"]) == 32


@pytest.mark.asyncio
async def test_webhook_duplicate_is_accepted_while_start_is_blocked_and_queue_is_full() -> None:
    app, entered, release, admission = _deferred_app()
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        try:
            first = await asyncio.wait_for(client.post(
                "/channels/hooks/events", headers={"Idempotency-Key": "delivery"}, json={"x": 1}
            ), 1)
            await asyncio.wait_for(entered.wait(), 1)
            ticket = next(iter(admission._tickets.values()))
            duplicate = await asyncio.wait_for(client.post(
                "/channels/hooks/events", headers={"Idempotency-Key": "delivery"}, json={"x": 1}
            ), 1)
            full = await client.post(
                "/channels/hooks/events", headers={"Idempotency-Key": "other"}, json={"x": 1}
            )
            assert first.status_code == duplicate.status_code == 202
            assert full.status_code == 503
            assert admission.queued_total == 1
            assert next(iter(admission._tickets.values())) is ticket
            assert not ticket.started.done()
            release.set()
            await asyncio.wait_for(ticket.completed, 1)
            assert ticket.started.result() is None
        finally:
            release.set()
            await admission.close()


@pytest.mark.asyncio
async def test_webhook_late_start_failure_does_not_leave_unhandled_futures() -> None:
    app, entered, release, admission = _deferred_app()
    app.state.ready = True
    loop = asyncio.get_running_loop()
    errors = []
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: errors.append(context))

    async def fail_runner(event, ticket):
        entered.set()
        await release.wait()
        raise RuntimeError("startup failed after admission")

    admission._runner = fail_runner
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
            response = await asyncio.wait_for(client.post(
                "/channels/hooks/events", headers={"Idempotency-Key": "failed"}, json={"x": 1}
            ), 1)
        assert response.status_code == 202
        ticket = next(iter(admission._tickets.values()))
        release.set()
        done, _ = await asyncio.wait({ticket.completed}, timeout=1)
        assert done
        await asyncio.sleep(0)  # run the outcome callbacks, not an execution delay
        # Leave both outcomes unawaited: the HTTP caller has already gone away.
        started_ref = weakref.ref(ticket.started)
        completed_ref = weakref.ref(ticket.completed)
        await admission.close()
        done.clear()
        admission._tickets.clear()
        del ticket
        gc.collect()
        assert started_ref() is completed_ref() is None
        assert errors == []
    finally:
        release.set()
        await admission.close()
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
async def test_auth_body_cap_and_ignored_gitlab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISSING_HOOK", "test-token")
    gitlab = _channel("gitlab", auth=WebhookAuthBlock(type="header_token", header="X-Hook", env="MISSING_HOOK"))
    admission = Admission(
        namespace="ns", agent_uid="3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77",
        max_concurrent_invocations=1, max_queued_total=4, idempotency_window_seconds=60,
        max_invocation_seconds=20, workspace_max_concurrent_sessions=1,
        channel_concurrency={"hooks": 1}, clock=lambda: datetime.now(timezone.utc), runner=_hold_runner,
    )
    app = create_app(SimpleNamespace(config=SimpleNamespace(channels=[gitlab], agent=SimpleNamespace(name="test-agent"), identity_environment="test-env"), admission=admission, _stopping=False))
    app.state.ready = True
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        unauthorized = await client.post("/channels/hooks/events", content=b"{}")
        oversized = await client.post("/channels/hooks/events", content=b"x" * (1024 * 1024 + 1))
        ignored = await client.post(
            "/channels/hooks/events", headers={"X-Hook": "test-token", "X-Gitlab-Event-UUID": "ignored"},
            json={"object_kind": "pipeline"},
        )
    assert unauthorized.status_code == 401
    assert oversized.status_code == 413
    assert ignored.status_code == 200
    assert admission.queued_total == 0
    await admission.close()


@pytest.mark.asyncio
async def test_github_legacy_payload_auth_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib
    import hmac
    import json

    monkeypatch.setenv("GITHUB_HOOK_SECRET", "hook-secret")
    channel = ChannelConfig.model_validate({
        "name": "github-pr",
        "type": "webhook",
        "source": "github",
        "webhook": {"auth": {"type": "hmac", "env": "GITHUB_HOOK_SECRET"}},
    })
    payload = {
        "action": "opened",
        "repository": {"full_name": "ackstorm/ach-runtime"},
        "number": 17,
        "pull_request": {"number": 99},
    }
    event = normalize_webhook(payload, {"X-GitHub-Delivery": "delivery-17"}, channel)
    assert event is not None
    assert event.context["github"] == {"repo": "ackstorm/ach-runtime", "prNumber": 17}

    for empty_number in (0, ""):
        fallback_event = normalize_webhook(
            {
                "repository": {"full_name": "ackstorm/ach-runtime"},
                "number": empty_number,
                "pull_request": {"number": 99},
            },
            {"X-GitHub-Delivery": "delivery-fallback"},
            channel,
        )
        assert fallback_event is not None
        assert fallback_event.context["github"]["prNumber"] == 99

    from ach_runtime.channels.routing import route

    started = asyncio.get_running_loop().create_future()
    started.set_result(None)
    admissions = []

    class Admission:
        def admit(self, routed):
            admissions.append(routed)
            return SimpleNamespace(
                invocation_id="started-github", started=started,
                completed=asyncio.get_running_loop().create_future(),
            )

    app = create_app(SimpleNamespace(
        config=SimpleNamespace(channels=[channel], agent=SimpleNamespace(name="test-agent"), identity_environment="test-env"), admission=Admission(), _stopping=False
    ))
    app.state.ready = True
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signature = hmac.new(b"hook-secret", raw, hashlib.sha256).hexdigest()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        accepted = await client.post(
            "/channels/github-pr/events",
            content=raw,
            headers={"X-GitHub-Delivery": "delivery-17", "X-Hub-Signature-256": f"sha256={signature}"},
        )
        rejected = await client.post(
            "/channels/github-pr/events",
            content=raw,
            headers={"X-GitHub-Delivery": "delivery-18", "X-Hub-Signature-256": "sha256=bad"},
        )
    assert accepted.status_code == 202
    assert rejected.status_code == 401
    assert len(admissions) == 1
    assert admissions[0] == route(event, channel)
    assert admissions[0].workspace_key == "github:github-pr:ackstorm/ach-runtime"
    assert admissions[0].session_key == "pr:17"

    for malformed in (
        {"number": 17, "pull_request": {"number": 17}},
        {"repository": {"full_name": "ackstorm/ach-runtime"}, "number": True},
    ):
        with pytest.raises(ValueError):
            normalize_webhook(malformed, {"X-GitHub-Delivery": "delivery"}, channel)

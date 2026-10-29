from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.schema import WebhookAuthBlock
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.harness.admission import Admission
from ach_runtime.harness.app import create_app
from ach_runtime.harness.control import Control


@pytest.fixture
def invalid_event_app():
    channel = SimpleNamespace(
        name="gitlab", type="webhook", source="gitlab",
        webhook=SimpleNamespace(
            auth=WebhookAuthBlock(type="none"), gitlab_events=None,
            bot_username=None, trigger_users=None, merge_requests_only=False,
        ),
    )
    config = SimpleNamespace(channels=[channel], agent=SimpleNamespace(name="test-agent"),
                             identity_environment="test-env")
    admission = Admission(
        namespace="ns", agent_uid="3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77",
        max_concurrent_invocations=1, max_queued_total=4, idempotency_window_seconds=1,
        max_invocation_seconds=20, workspace_max_concurrent_sessions=1,
        channel_concurrency={"gitlab": 1}, clock=lambda: datetime.now(timezone.utc), runner=_runner,
    )
    control = SimpleNamespace(config=config, admission=admission, _stopping=False)
    return create_app(control), admission


async def _runner(event, ticket):
    ticket.mark_started()


async def test_invalid_gitlab_payload_is_rejected_before_admission(invalid_event_app) -> None:
    app, admission = invalid_event_app
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        not_ready = await client.post("/channels/gitlab/events", content=b"{}")
        app.state.ready = True
        response = await client.post(
            "/channels/gitlab/events",
            headers={"X-Gitlab-Event-UUID": "delivery-1"},
            json={"object_kind": "merge_request", "object_attributes": {}},
        )
        unknown = await client.post("/channels/missing/events", content=b"{}")
        oversized = await client.post("/channels/gitlab/events", content=b"x" * (1024 * 1024 + 1))
    assert not_ready.status_code == 503
    assert response.status_code == 422
    assert unknown.status_code == 404
    assert oversized.status_code == 413
    assert admission.queued_total == 0
    await admission.close()


async def test_metrics_is_mounted_with_static_process_identity() -> None:
    from ach_runtime.observability.metrics import TURNS_TOTAL

    TURNS_TOTAL.labels(model="metrics-model", channel="webhook").inc()
    control = SimpleNamespace(
        config=SimpleNamespace(
            channels=[],
            agent=SimpleNamespace(name="metrics-agent"),
            identity_environment="metrics-env",
        ),
        admission=SimpleNamespace(),
        _stopping=False,
    )
    app = create_app(control)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        response = await client.get("/metrics/")
    assert response.status_code == 200
    assert ('ach_agent_turns_total{agent="metrics-agent",channel="webhook",'
            'environment="metrics-env",model="metrics-model"} 1.0') in response.text
    assert 'agent="metrics-agent"' in response.text
    assert 'environment="metrics-env"' in response.text


async def test_metrics_is_mounted_without_an_ach_block() -> None:
    control = SimpleNamespace(
        config=SimpleNamespace(
            channels=[], agent=SimpleNamespace(name="metrics-agent"), identity_environment="team-ns"
        ),
        admission=SimpleNamespace(),
        _stopping=False,
    )
    transport = httpx.ASGITransport(app=create_app(control))
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        response = await client.get("/metrics/")
    assert response.status_code == 200
    assert 'environment="team-ns"' in response.text


@pytest.mark.parametrize("payload", [
    ("Merge Request Hook", {"object_kind": "merge_request", "project": {"id": 7},
                             "object_attributes": {"iid": 4}, "user": ["x"]}),
    ("Note Hook", {"object_kind": "note", "project": {"id": 7},
                    "object_attributes": ["x"], "user": {"username": "alice"}}),
])
async def test_malformed_nested_gitlab_objects_return_422_without_admission(invalid_event_app, payload) -> None:
    app, admission = invalid_event_app
    app.state.ready = True
    event_name, body = payload
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        response = await client.post(
            "/channels/gitlab/events",
            headers={"X-Gitlab-Event-UUID": "delivery-1", "X-Gitlab-Event": event_name}, json=body,
        )
    assert response.status_code == 422
    assert admission.queued_total == 0
    await admission.close()


def _script_app(script: str, *, output: Path, auth: str = "none", bot: str | None = None):
    channel = ChannelConfig.model_validate({
        "name": "gitlab-register-hook",
        "type": "webhook-script",
        "source": "gitlab",
        "webhook": {
            "auth": {"type": auth, **({"env": "HOOK_TOKEN"} if auth != "none" else {})},
            "gitlabEvents": ["merge_request"],
            **({"botUsername": bot} if bot else {}),
        },
        "script": {"script": script, "env": {"OUT": str(output)}},
    })
    control = Control.__new__(Control)
    control.config = SimpleNamespace(channels=[channel], agent=SimpleNamespace(name="test-agent"),
                                     identity_environment="test-env")
    control._private_env = {"PATH": "/bin:/usr/bin"}
    control._stopping = False
    control.admission = Admission(
        namespace="ns", agent_uid="3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77",
        max_concurrent_invocations=1, max_concurrent_scripts=1, max_queued_total=1,
        idempotency_window_seconds=60, max_invocation_seconds=20,
        workspace_max_concurrent_sessions=1, channel_concurrency={channel.name: 1},
        clock=lambda: datetime.now(timezone.utc), runner=control.run,
    )
    app = create_app(control)
    app.state.ready = True
    return app, control.admission


def _mr_body() -> dict[str, object]:
    return {
        "object_kind": "merge_request",
        "project": {"id": 42, "path_with_namespace": "team/service"},
        "object_attributes": {"iid": 7, "last_commit": {"id": "abc123"}},
        "user": {"username": "alice"},
    }


async def test_webhook_script_http_ack_precedes_real_spawn_and_duplicate_shares_ticket(
    tmp_path: Path, monkeypatch,
) -> None:
    gate = tmp_path / "release"
    output = tmp_path / "completed"
    app, admission = _script_app(
        f'while [ ! -e "{gate}" ]; do sleep 0.01; done; printf "%s" "$ACH_EVENT_PROJECT_PATH:$ACH_EVENT_MR_IID:$ACH_EVENT_HEAD_SHA" > "$OUT"',
        output=output,
    )
    spawn_entered = asyncio.Event()
    allow_spawn = asyncio.Event()
    real_spawn = asyncio.create_subprocess_exec

    async def gated_spawn(*args, **kwargs):
        spawn_entered.set()
        await allow_spawn.wait()
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        first_task = asyncio.create_task(client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Event-UUID": "delivery-1"}, json=_mr_body(),
        ))
        await asyncio.wait_for(spawn_entered.wait(), 2)
        ticket = next(iter(admission._tickets.values()))
        first = await asyncio.wait_for(first_task, 1)
        assert first.status_code == 202
        assert not ticket.started.done() and not ticket.started.cancelled()
        allow_spawn.set()
        assert not ticket.completed.done(), "202 must precede script exit"
        duplicate = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Event-UUID": "delivery-1"}, json=_mr_body(),
        )
        assert duplicate.status_code == 202
        assert next(iter(admission._tickets.values())) is ticket
        assert admission.queued_total == 1
        gate.touch()
        await asyncio.wait_for(ticket.completed, 2)
    assert output.read_text() == "team/service:7:abc123"
    await admission.close()


async def test_webhook_script_http_auth_filter_malformed_and_saturation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOOK_TOKEN", "expected")
    gate = tmp_path / "release"
    app, admission = _script_app(
        f'while [ ! -e "{gate}" ]; do sleep 0.01; done',
        output=tmp_path / "unused", auth="gitlab_token", bot="alice",
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        auth = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Event-UUID": "bad-auth"}, json=_mr_body(),
        )
        filtered = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Token": "expected", "X-Gitlab-Event-UUID": "filtered"},
            json=_mr_body(),
        )
        malformed = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Token": "expected", "X-Gitlab-Event-UUID": "bad"},
            json={"object_kind": "merge_request", "project": {}, "object_attributes": {}},
        )
        assert auth.status_code == 401
        assert filtered.status_code == 200
        assert malformed.status_code == 422
        assert admission.queued_total == 0

        accepted = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Token": "expected", "X-Gitlab-Event-UUID": "accepted"},
            json={**_mr_body(), "user": {"username": "bob"}},
        )
        assert accepted.status_code == 202
        full = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Token": "expected", "X-Gitlab-Event-UUID": "second"},
            json={**_mr_body(), "user": {"username": "bob"}},
        )
        assert full.status_code == 503
        gate.touch()
        await asyncio.wait_for(next(iter(admission._tickets.values())).completed, 2)
    await admission.close()


async def test_webhook_script_http_spawn_failure_keeps_admission_ack_202(tmp_path: Path, monkeypatch) -> None:
    app, admission = _script_app(":", output=tmp_path / "unused")

    async def fail_spawn(*args, **kwargs):
        raise OSError("spawn denied")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        response = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Event-UUID": "spawn-failure"}, json=_mr_body(),
        )
    ticket = next(iter(admission._tickets.values()))
    assert response.status_code == 202
    with pytest.raises(RuntimeError):
        await ticket.completed
    assert ticket.started.done() and isinstance(ticket.started.exception(), RuntimeError)
    await admission.close()


async def test_webhook_script_post_spawn_nonzero_exit_keeps_http_202(tmp_path: Path) -> None:
    app, admission = _script_app("exit 17", output=tmp_path / "unused")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://control") as client:
        response = await client.post(
            "/channels/gitlab-register-hook/events",
            headers={"X-Gitlab-Event-UUID": "post-spawn-failure"}, json=_mr_body(),
        )
    ticket = next(iter(admission._tickets.values()))
    assert response.status_code == 202
    with pytest.raises(RuntimeError, match="exited 17"):
        await ticket.completed
    assert ticket.started.done() and ticket.started.exception() is None
    await admission.close()



async def test_create_app_initialises_metrics_for_configured_channels(monkeypatch) -> None:
    from ach_runtime.harness import app as app_module

    seen: list[list[str]] = []
    monkeypatch.setattr(app_module, "initialise_metrics", lambda channels: seen.append(list(channels)))
    control = SimpleNamespace(
        config=SimpleNamespace(
            channels=[SimpleNamespace(name="presence-ch", type="webhook")],
            agent=SimpleNamespace(name="presence-agent"),
            identity_environment="presence-env",
        ),
        admission=SimpleNamespace(),
        _stopping=False,
    )
    create_app(control)
    assert seen == [["presence-ch"]]

"""Decouple acceptance from engine readiness (authoritative conformance test).

Formerly this asserted the "proven-start gate A′" (old CONTRACT §6.7): NACK/503
during the pod's first warmup. That coupling was a design bug — a webhook-only
deployment 503s every inbound event forever because the engine only starts on
acceptance (deadlock: never accepted, so never started). Restored to legacy
(ackbot-process) behavior: acceptance depends only on harness readiness and the
`draining` gate, never on engine state — the engine starts lazily per
session_key. See docs/references/2026-07-01-router-pool-vs-legacy.md finding B8.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from ach_agent.config.schema import ChannelConfig


def _make_webhook_cfg(secret_path: str, name: str = "test-channel") -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": name,
            "type": "webhook",
            "source": "gitlab",
            "webhook": {
                "auth": {"type": "gitlab_token", "secret": {"file": secret_path}},
            },
        }
    )


MR_PAYLOAD: dict[str, Any] = {
    "object_kind": "merge_request",
    "project": {"id": 1, "name": "test-repo"},
    "object_attributes": {"iid": 1, "title": "Test MR", "state": "opened"},
}


@pytest.mark.asyncio
async def test_inv07_engine_not_ready_does_not_gate_acceptance(tmp_path: Any) -> None:
    """Decouple: engine_has_been_ready_once=False must NOT block acceptance —
    inbound events receive 202 and are routed, never a 503 for engine reasons.

    The engine starts lazily per session_key inside the lane (pool.acquire in
    engine_runner) — it is not a precondition for accepting the message.
    """
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.http.app import create_app
    from ach_agent.router import Router
    from ach_agent.router.dedup import InMemoryDedupStore

    secret_file = tmp_path / "hmac_secret"
    secret_file.write_text("test-secret")
    channel_cfg = _make_webhook_cfg(str(secret_file))

    async def fake_engine(event: MessageEvent, on_kill: Any) -> None:
        on_kill()

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine,
        delivery_adapter=None,
    )

    # There is no engine-readiness gate anymore — acceptance depends only on harness
    # readiness (+ draining). The engine starts lazily per session_key in the lane.
    app = create_app(channels=[channel_cfg], handler=router)

    headers = {
        "X-Gitlab-Token": "test-secret",
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Event-UUID": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        resp = await client.post(
            "/channels/test-channel/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=headers,
        )

    assert resp.status_code == 202, (
        "Decouple: engine-not-ready must never gate acceptance — "
        f"expected 202, got {resp.status_code}. Acceptance depends only on harness "
        "readiness and the draining gate (never engine state)."
    )

"""CONTRACT §6.7: Proven-start gate A′ invariant (authoritative conformance test).

Invariant: during the pod's first warmup, NACK/503 instead of buffering;
accept-and-buffer only after the engine has been ready once.
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
                "auth": {"type": "gitlab_token", "secretPath": secret_path},
            },
        }
    )


MR_PAYLOAD: dict[str, Any] = {
    "object_kind": "merge_request",
    "project": {"id": 1, "name": "test-repo"},
    "object_attributes": {"iid": 1, "title": "Test MR", "state": "opened"},
}


@pytest.mark.asyncio
async def test_inv07_a_prime_gate(tmp_path: Any) -> None:
    """§6.7: A′ proven-start gate — NACK/503 during first warmup — authoritative conformance.

    CONTRACT perspective: when engine_has_been_ready_once is False (first warmup),
    inbound events receive a 503 — never a 200/202 that would silently accept
    and buffer. Accept-and-buffer is only valid after the engine has proven
    its first startup (proven-start gate, spec §8.5).
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

    # FakePool with engine_has_been_ready_once=False models the first warmup.
    class _FakePool:
        engine_has_been_ready_once: bool = False

    pool = _FakePool()
    app = create_app(channels=[channel_cfg], handler=router, pool=pool)

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

    assert resp.status_code == 503, (
        f"§6.7: A′ gate — during first warmup (engine_has_been_ready_once=False) "
        f"inbound must return 503 (NACK), not {resp.status_code}. "
        "Accept-and-buffer is only valid after the engine has been ready once (spec §8.5)."
    )

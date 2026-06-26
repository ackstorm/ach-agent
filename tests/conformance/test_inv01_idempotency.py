"""CONTRACT §6.1: Idempotency-key derivation invariant (authoritative conformance test).

Invariant: idempotency-key derivation is per-channel-type; unique-per-distinct-event;
degrades to unique-per-arrival (never to a shared/empty key).
"""
from __future__ import annotations

import time


def test_inv01_idempotency_key_derivation() -> None:
    """§6.1: idempotency-key derivation per channel type — authoritative conformance.

    CONTRACT perspective: submit distinct events of each channel type → each yields
    a distinct non-empty key. Header-less webhook payloads yield unique-per-arrival
    keys (the broad-key regression — a shared fallback would silently drop the second
    event via dedup).
    """
    from datetime import UTC, datetime

    from ach_agent.router.dedup import (
        derive_a2a_idempotency_key,
        derive_cron_idempotency_key,
        derive_webhook_idempotency_key,
    )

    # Each channel type yields a non-empty key for a known input.
    wh_key = derive_webhook_idempotency_key({"X-Gitlab-Event-UUID": "gl-uuid-001"})
    assert wh_key, "webhook idempotency key must be non-empty"
    assert wh_key == "gl-uuid-001"

    a2a_key = derive_a2a_idempotency_key("task-001")
    assert a2a_key, "a2a idempotency key must be non-empty"

    tick = datetime(2026, 6, 20, 9, 0, 0, tzinfo=UTC)
    cron_key = derive_cron_idempotency_key("heartbeat", tick)
    assert cron_key, "cron idempotency key must be non-empty"

    # Keys across distinct events must differ (uniqueness per distinct event).
    keys = [wh_key, a2a_key, cron_key]
    assert len(set(keys)) == len(keys), (
        "§6.1: distinct events must yield distinct idempotency keys"
    )

    # Broad-key regression: two header-less webhooks must NOT produce the same key.
    # A shared fallback would cause the second event to be silently deduped against
    # the first — this is the broad-key dedup bug (SC#3).
    key1 = derive_webhook_idempotency_key({})
    time.sleep(0.002)
    key2 = derive_webhook_idempotency_key({})
    assert key1 != key2, (
        "§6.1: header-less webhook fallback must be unique-per-arrival "
        "(shared key would silently drop the second event — broad-key regression)"
    )
    assert key1.isdigit() and key2.isdigit(), (
        "§6.1: header-less fallback keys must be ms-timestamp strings"
    )


def test_inv01_a2a_idempotency_from_task_id() -> None:
    """§6.1: a2a idempotency key derives from the task id — distinct, never empty/shared.

    Distinct task ids → distinct keys; an empty task id degrades to a unique-per-arrival
    ms-timestamp (never empty, never a shared constant).
    """
    from ach_agent.router.dedup import derive_a2a_idempotency_key

    k1 = derive_a2a_idempotency_key("task-001")
    k2 = derive_a2a_idempotency_key("task-002")
    assert k1 and k2, "§6.1: a2a keys must be non-empty"
    assert k1 != k2, "§6.1: distinct task ids must yield distinct a2a keys"
    assert k1 == "a2a:task-001", "§6.1: a2a key derives from the task id"

    # Empty task id → unique-per-arrival fallback (never empty/shared).
    e1 = derive_a2a_idempotency_key("")
    time.sleep(0.002)
    e2 = derive_a2a_idempotency_key("")
    assert e1 and e2 and e1 != e2, "§6.1: empty-task-id a2a fallback must be unique-per-arrival"


async def test_inv01_queue_idempotency_from_message_id() -> None:
    """§6.1: queue idempotency key IS the redis message id — distinct, never empty.

    Drive QueueConsumer._handle_message with a capturing handler and assert the
    emitted MessageEvent carries idempotency_key == the redis message id. Distinct
    message ids → distinct keys.
    """
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.channels.queue import QueueConsumer
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.router.router import RouterAdmitResult

    captured: list[MessageEvent] = []

    class _CapturingHandler:
        async def handle(self, event: MessageEvent) -> RouterAdmitResult:
            captured.append(event)
            return RouterAdmitResult.ACCEPTED

    class _FakeRedis:
        async def xack(self, *_args: object, **_kwargs: object) -> int:
            return 1

    cfg = ChannelConfig.model_validate(
        {"name": "q1", "type": "queue", "queue": {"type": "redis", "key": "events"}}
    )
    consumer = QueueConsumer(cfg, handler=_CapturingHandler(), redis_client=_FakeRedis())

    await consumer._handle_message(b"1718900000000-0", {b"text": b"hello"})
    await consumer._handle_message(b"1718900000001-0", {b"text": b"world"})

    assert [e.idempotency_key for e in captured] == ["1718900000000-0", "1718900000001-0"], (
        "§6.1: queue idempotency_key must be the redis message id (distinct, never empty)"
    )

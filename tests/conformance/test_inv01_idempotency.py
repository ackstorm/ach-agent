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

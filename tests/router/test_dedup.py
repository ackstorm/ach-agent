"""Dedup store + idempotency derivation tests (IDM-01/02/03 + SC#3).

Tests pure logic in router/dedup.py — no Router dependency required.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime


def test_idempotency_all_channels() -> None:
    """IDM-01: per-channel idempotency derivation returns expected key."""
    from ach_agent.router.dedup import (
        derive_cron_idempotency_key,
        derive_slack_idempotency_key,
        derive_telegram_idempotency_key,
        derive_webhook_idempotency_key,
    )

    # Webhook: header priority chain — first present wins
    assert derive_webhook_idempotency_key({"X-GitHub-Delivery": "gh-abc"}) == "gh-abc"
    assert derive_webhook_idempotency_key({"X-Gitlab-Event-UUID": "gl-xyz"}) == "gl-xyz"
    assert derive_webhook_idempotency_key({"svix-id": "svix-123"}) == "svix-123"
    assert derive_webhook_idempotency_key({"X-Request-ID": "req-456"}) == "req-456"
    assert derive_webhook_idempotency_key({"Idempotency-Key": "idem-789"}) == "idem-789"

    # Slack: ts field
    assert derive_slack_idempotency_key({"ts": "1234567890.123456"}) == "1234567890.123456"

    # Telegram: update_id
    assert derive_telegram_idempotency_key({"update_id": 99}) == "99"
    assert derive_telegram_idempotency_key({"update_id": 0}) == "0"

    # Cron: {channel}:{scheduled_tick_iso} using the PASSED datetime (D-09)
    tick = datetime(2026, 6, 19, 10, 30, 0, tzinfo=UTC)
    key = derive_cron_idempotency_key("heartbeat", tick)
    assert key == "heartbeat:2026-06-19T10:30:00"


def test_two_headerless_webhooks_both_process() -> None:
    """IDM-02 / SC#3: two header-less payloads yield DISTINCT keys (ms-timestamp fallback).

    This is the broad-key regression test. A shared/empty fallback would cause
    the second event to be silently deduped against the first (Pitfall 1).
    """
    from ach_agent.router.dedup import derive_webhook_idempotency_key

    key1 = derive_webhook_idempotency_key({})  # no delivery headers
    time.sleep(0.002)                          # ensure ms-timestamp differs
    key2 = derive_webhook_idempotency_key({})

    assert key1 != key2, (
        "Header-less fallback must be unique-per-arrival (ms-timestamp). "
        "A shared key would silently drop the second event — the broad-key dedup bug (SC#3)."
    )
    # Both keys must be non-empty strings parseable as integers (ms timestamps)
    assert key1.isdigit(), f"Expected ms-timestamp string, got: {key1!r}"
    assert key2.isdigit(), f"Expected ms-timestamp string, got: {key2!r}"


def test_file_backed_dedup_basic(tmp_path: "Path") -> None:
    """DUR-01: FileBackedDedupStore basic seen/mark contract."""
    from pathlib import Path
    from ach_agent.router.dedup import FileBackedDedupStore

    store = FileBackedDedupStore(tmp_path / "dedup.db")
    key = "test-event-key"
    assert not store.seen(key)
    store.mark(key, ttl_seconds=3600)
    assert store.seen(key)
    store.close()


def test_disk_dedup_survives_restart(tmp_path: "Path") -> None:
    """DUR-01 SC#1: redelivered event after restart is discarded by disk store."""
    from pathlib import Path
    from ach_agent.router.dedup import FileBackedDedupStore

    db_path = tmp_path / "dedup.db"

    # Session 1: mark a key (simulates first pod run)
    store1 = FileBackedDedupStore(db_path)
    store1.mark("gitlab-mr-review:uuid-123", ttl_seconds=3600)
    assert store1.seen("gitlab-mr-review:uuid-123")
    store1.close()  # simulates pod shutdown

    # Session 2: reopen the same DB (simulates pod restart)
    store2 = FileBackedDedupStore(db_path)
    assert store2.seen("gitlab-mr-review:uuid-123"), (
        "DUR-01: redelivered event must be discarded after restart (disk dedup)"
    )
    store2.close()


def test_disk_dedup_expiry_is_wall_clock(tmp_path: "Path") -> None:
    """DUR-01 D-02: FileBackedDedupStore uses wall-clock time.time() — key seen after reopen."""
    from pathlib import Path
    from ach_agent.router.dedup import FileBackedDedupStore

    db_path = tmp_path / "dedup.db"

    # Mark with a long TTL and close
    store1 = FileBackedDedupStore(db_path)
    store1.mark("some-key", ttl_seconds=3600)
    store1.close()

    # Reopen — wall-clock expiry must still show the key as seen (not falsely expired)
    store2 = FileBackedDedupStore(db_path)
    assert store2.seen("some-key"), (
        "DUR-01 D-02: wall-clock expiry must survive reopen (not monotonic)"
    )
    store2.close()


def test_file_backed_dedup_expires(tmp_path: "Path") -> None:
    """DUR-01: FileBackedDedupStore honors TTL=0 expiry."""
    from pathlib import Path
    from ach_agent.router.dedup import FileBackedDedupStore

    store = FileBackedDedupStore(tmp_path / "dedup.db")
    key = "expiring-key"
    store.mark(key, ttl_seconds=0)
    # TTL=0 means expiry=time.time()+0 which is already in the past or present
    time.sleep(0.01)
    assert not store.seen(key), "Key should expire after TTL=0 + tiny sleep"
    store.close()


def test_dedup_ttl_window() -> None:
    """IDM-03: InMemoryDedupStore.seen() respects TTL window.

    - seen(k) is False before mark
    - seen(k) is True after mark(k, ttl)
    - seen(k) is False again after TTL elapses
    """
    from ach_agent.router.dedup import InMemoryDedupStore

    store = InMemoryDedupStore()
    key = "test-event-key"

    # Before mark: not seen
    assert not store.seen(key)

    # After mark with 1-second TTL: seen
    store.mark(key, ttl_seconds=1)
    assert store.seen(key)

    # After TTL elapses (using tiny TTL + monotonic advance via sleep)
    store.mark(key, ttl_seconds=0)  # TTL=0: expires immediately
    # Force time to pass at least a tiny bit so monotonic > expiry
    time.sleep(0.001)
    assert not store.seen(key), "Key should expire after TTL=0 + tiny sleep"

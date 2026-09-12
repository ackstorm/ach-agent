from __future__ import annotations

import pytest

from ach_agent.channels.signing import (
    AuthenticationError,
    NonceCache,
    request_mac,
    response_mac,
    verify_request_mac,
    verify_response_mac,
)

KEY = b"channel-harness-key"
BODY = b'{"id":"evt-1","text":"hello"}'


def test_request_mac_binds_every_request_field_and_exact_body() -> None:
    signature = request_mac(KEY, "POST", "/internal/v1/events", 1_700_000_000, "n-1", BODY)

    assert verify_request_mac(
        KEY, "POST", "/internal/v1/events", 1_700_000_000, "n-1", BODY, signature
    )
    for changed in (
        {"method": "PUT"},
        {"target": "/internal/v1/results"},
        {"timestamp": 1_700_000_001},
        {"nonce": "n-2"},
        {"body": BODY + b" "},
    ):
        values = {
            "method": "POST",
            "target": "/internal/v1/events",
            "timestamp": 1_700_000_000,
            "nonce": "n-1",
            "body": BODY,
        }
        values.update(changed)
        assert not verify_request_mac(KEY, signature=signature, **values)


def test_response_mac_binds_request_nonce_status_and_exact_body() -> None:
    signature = response_mac(KEY, "n-1", 202, BODY)

    assert verify_response_mac(KEY, "n-1", 202, BODY, signature)
    assert not verify_response_mac(KEY, "n-2", 202, BODY, signature)
    assert not verify_response_mac(KEY, "n-1", 200, BODY, signature)
    assert not verify_response_mac(KEY, "n-1", 202, BODY + b" ", signature)


def test_nonce_cache_accepts_window_boundaries_and_rejects_replay() -> None:
    cache = NonceCache(window_seconds=60, max_entries=2, clock=lambda: 100.0)

    cache.accept("at-start", 70)
    cache.accept("at-end", 130)
    with pytest.raises(AuthenticationError, match="replay"):
        cache.accept("at-start", 70)
    with pytest.raises(AuthenticationError, match="timestamp"):
        cache.accept("too-old", 69)
    with pytest.raises(AuthenticationError, match="timestamp"):
        cache.accept("too-new", 131)


def test_nonce_cache_saturation_does_not_evict_live_nonce() -> None:
    cache = NonceCache(window_seconds=60, max_entries=1, clock=lambda: 100.0)
    cache.accept("live", 100)

    with pytest.raises(AuthenticationError, match="saturated"):
        cache.accept("new", 100)
    with pytest.raises(AuthenticationError, match="replay"):
        cache.accept("live", 100)


def test_future_timestamp_nonce_survives_through_its_admissible_boundary() -> None:
    now = [100.0]
    cache = NonceCache(window_seconds=60, max_entries=2, clock=lambda: now[0])
    cache.accept("future", 130)

    now[0] = 160.0
    with pytest.raises(AuthenticationError, match="replay"):
        cache.accept("future", 130)

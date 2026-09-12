"""Small, deterministic HMAC envelope used by the channels/harness HTTP seam."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable

REQUEST_HEADER = "X-ACH-Request-MAC"
RESPONSE_HEADER = "X-ACH-Response-MAC"
TIMESTAMP_HEADER = "X-ACH-Request-Timestamp"
NONCE_HEADER = "X-ACH-Request-Nonce"
SIGNATURE_HEADER = RESPONSE_HEADER

REQUEST_CLOCK_SKEW_SECONDS = 30
DEFAULT_NONCE_WINDOW_SECONDS = 60
DEFAULT_NONCE_CACHE_SIZE = 4096


class AuthenticationError(ValueError):
    """A request/response was not authentic or could be replayed."""


def _canonical(value: list[object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def request_mac(
    key: bytes, method: str, target: str, timestamp: int, nonce: str, body: bytes
) -> str:
    """Return the hex HMAC for the exact request method, target, and body."""
    message = _canonical(["request", 1, method, target, timestamp, nonce, _digest(body)])
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def response_mac(key: bytes, request_nonce: str, status: int, body: bytes) -> str:
    """Return the hex HMAC for one HTTP response bound to its request nonce."""
    message = _canonical(["response", 1, request_nonce, status, _digest(body)])
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_request_mac(
    key: bytes,
    method: str,
    target: str,
    timestamp: int,
    nonce: str,
    body: bytes,
    signature: str,
) -> bool:
    expected = request_mac(key, method, target, timestamp, nonce, body)
    return hmac.compare_digest(expected, signature)


def verify_response_mac(
    key: bytes, request_nonce: str, status: int, body: bytes, signature: str
) -> bool:
    expected = response_mac(key, request_nonce, status, body)
    return hmac.compare_digest(expected, signature)


class NonceCache:
    """Bounded replay cache for authenticated channel requests.

    A full cache rejects a new nonce. It never evicts an accepted live nonce,
    which keeps saturation from turning one accepted request into a replay hole.
    """

    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_NONCE_WINDOW_SECONDS,
        max_entries: int = DEFAULT_NONCE_CACHE_SIZE,
        clock: Callable[[], float] = time.time,
        timestamp_window_seconds: int = REQUEST_CLOCK_SKEW_SECONDS,
    ) -> None:
        if window_seconds <= 0 or max_entries <= 0 or timestamp_window_seconds < 0:
            raise ValueError("nonce cache limits must be positive")
        self.window_seconds = window_seconds
        self.max_entries = max_entries
        self.timestamp_window_seconds = timestamp_window_seconds
        self._clock = clock
        self._entries: dict[str, float] = {}

    def accept(self, nonce: str, timestamp: int) -> None:
        now = self._clock()
        if abs(now - timestamp) > self.timestamp_window_seconds:
            raise AuthenticationError("request timestamp outside authentication window")
        self._purge(now)
        if nonce in self._entries:
            raise AuthenticationError("request nonce replay")
        if len(self._entries) >= self.max_entries:
            raise AuthenticationError("request nonce cache saturated")
        # Keep the nonce through the end of the entire timestamp-admissible
        # interval. A request carrying a future timestamp may still be replayed
        # after ``window_seconds`` has elapsed since arrival.
        self._entries[nonce] = timestamp + self.window_seconds

    def check_and_store(self, nonce: str, timestamp: int) -> None:
        self.accept(nonce, timestamp)

    def __len__(self) -> int:
        self._purge(self._clock())
        return len(self._entries)

    def _purge(self, now: float) -> None:
        for nonce, expiry in list(self._entries.items()):
            if expiry < now:
                del self._entries[nonce]

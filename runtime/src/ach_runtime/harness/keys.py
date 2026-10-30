# SPDX-License-Identifier: MIT
"""The control process's ACH credential.

`KeySource` holds a static ek_ (`keyFrom.secretKeyRef`) or a JWT exchanged for the pod's
projected ServiceAccount token (`keyFrom.tokenExchange`). Every consumer reads it per request,
through `current()` or through `auth` on its httpx client, so a rotation reaches clients and
MCP sessions that are already open. A rotation replaces one reference: a request already sent
keeps its JWT, which ACH accepts until it expires.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ach_runtime.observability.metrics import KEY_EXPIRY_TIMESTAMP, KEY_LIFETIME, KEY_REFRESH_FAILURES

log = logging.getLogger(__name__)

REFRESH_AT = 0.8
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
# ACH's Authn answers a credential it does not accept with 401 and this code, before it
# proxies anything; an upstream's own 401 carries another code and is never retried.
REJECTED_CODE = "expired_or_revoked"


class ExchangeRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"ACH token exchange refused: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Exchange:
    base_url: str
    identity: str
    environment: str
    token_path: str


def error_code(response: httpx.Response) -> str:
    """ACH's `{"error": {"code": ...}}`, else http_<status>."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else f"http_{response.status_code}"


async def _exchange(client: httpx.AsyncClient, exchange: Exchange) -> tuple[str, float]:
    # The kubelet renews the projected token in place: read it for every exchange.
    token = Path(exchange.token_path).read_text(encoding="utf-8").strip()
    response = await client.post(
        f"{exchange.base_url.rstrip('/')}/platform/keys/exchange",
        headers={"Authorization": f"Bearer {token}"},
        json={"identity": exchange.identity, "environment": exchange.environment},
        follow_redirects=False,
    )
    if response.status_code != 200:
        raise ExchangeRefused(error_code(response))
    try:
        body = response.json()
        jwt, expires_in = body["token"], float(body["expires_in"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ExchangeRefused("invalid_response") from exc
    if not isinstance(jwt, str) or not jwt or expires_in <= 0:
        raise ExchangeRefused("invalid_response")
    return jwt, expires_in


class KeySource:
    def __init__(
        self,
        value: str = "",
        *,
        exchange: Exchange | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._value = value
        self._exchange = exchange
        self._client = client
        self._clock, self._wall, self._sleep = clock, wall, sleep
        self._expires_at = float("inf")
        self._refresh_at = float("inf")
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.generation = 0
        self.auth = KeyAuth(self)

    def current(self) -> str:
        return self._value

    def ready(self) -> bool:
        """False once an exchanged credential expired with no replacement."""
        return bool(self._value) and self._clock() < self._expires_at

    async def start(self) -> None:
        """Exchange once (a refusal raises ExchangeRefused with ACH's code), then keep rotating."""
        if self._exchange is None:
            return
        await self._renew()
        self._task = asyncio.create_task(self._rotate())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    async def refresh_rejected(self, generation: int) -> bool:
        """ACH rejected a request sent with `generation`. Concurrent callers wait on one shared
        re-exchange; True when a newer credential is there to retry with."""
        if self._exchange is None:
            return False
        async with self._lock:
            if self.generation == generation:
                try:
                    await self._renew()
                except Exception as exc:  # noqa: BLE001 - the caller keeps ACH's answer
                    KEY_REFRESH_FAILURES.inc()
                    log.warning("ACH credential re-exchange failed (%s)", exc)
        return self.generation != generation

    async def _renew(self) -> None:
        jwt, expires_in = await _exchange(self._client, self._exchange)
        now = self._clock()
        self._value, self.generation = jwt, self.generation + 1
        self._expires_at, self._refresh_at = now + expires_in, now + REFRESH_AT * expires_in
        KEY_EXPIRY_TIMESTAMP.set(self._wall() + expires_in)
        KEY_LIFETIME.set(expires_in)

    async def _rotate(self) -> None:
        backoff = MIN_BACKOFF_SECONDS
        while True:
            wait = self._refresh_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
                continue
            try:
                async with self._lock:
                    await self._renew()
            except Exception as exc:  # noqa: BLE001 - retried until an exchange succeeds
                KEY_REFRESH_FAILURES.inc()
                log.warning("ACH credential refresh failed (%s)", exc)
                remaining = self._expires_at - self._clock()
                # Clamped so a retry still happens before the current credential expires.
                delay = min(backoff, max(remaining / 2, MIN_BACKOFF_SECONDS)) if remaining > 0 else backoff
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                await self._sleep(delay)
            else:
                backoff = MIN_BACKOFF_SECONDS


class KeyAuth(httpx.Auth):
    """Sets `x-ach-key` from the KeySource on every request. When ACH itself rejects the
    credential (401 `expired_or_revoked`, before proxying), waits for the shared re-exchange
    and resends once, so the upstream never sees a request twice."""

    def __init__(self, keys: KeySource) -> None:
        self._keys = keys

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        generation = self._keys.generation
        request.headers["x-ach-key"] = self._keys.current()
        response = yield request
        if response.status_code != 401 or "json" not in response.headers.get("content-type", ""):
            return
        await response.aread()
        if error_code(response) == REJECTED_CODE and await self._keys.refresh_rejected(generation):
            request.headers["x-ach-key"] = self._keys.current()
            yield request

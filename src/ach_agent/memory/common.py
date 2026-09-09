# SPDX-License-Identifier: Apache-2.0
"""Backend-neutral memory helpers.

The pieces every memory backend needs and none of them owns: resolving a configured secret,
probing a backend's health, and recording that memory went degraded. They live here so no
backend has to import another backend's module to get them — a backend module holds ONE
backend's protocol and nothing shared.

Nothing here knows which backend is calling. Anything that does belongs in that backend's
own module.
"""

from __future__ import annotations

import asyncio

import structlog

from ach_agent.config.schema import SecretSource

log = structlog.get_logger(__name__)


def resolve_memory_secret(auth: SecretSource | None) -> tuple[bool, str | None]:
    """(ok, secret) gate for any backend's configured credential.

    Takes the ``SecretSource`` itself rather than a params object, so it stays free of every
    backend's config type.

    (True, None)  → no auth configured (internal URL) — proceed unauthenticated.
    (False, None) → auth configured but the env var is unset (misconfig) — caller DEGRADES,
                    it does not proceed unauthenticated. Collapsing this into the first case
                    would silently turn a misconfigured secret into an anonymous call.
    (True, secret)→ auth resolved — proceed with Bearer.
    """
    from ach_agent.config.schema import resolve_secret

    if auth is None:
        return True, None
    secret = resolve_secret(auth)
    return (False, None) if secret is None else (True, secret)


async def probe_memory_endpoint(endpoint: str, timeout: float = 2.0) -> bool:
    """True if the memory backend answers on /health within ``timeout``.

    Any exception (network error, timeout, non-2xx/3xx) → False, never raises: the caller's
    fail-open contract (D-02) turns a False into a degraded note, not an aborted event.

    T-04-02/T-04-04: bounded 2s timeout; the probe targets only the operator-rendered config
    URL, never user input (SSRF mitigation).
    """
    import aiohttp  # direct dependency

    try:
        async with asyncio.timeout(timeout):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{endpoint.rstrip('/')}/health") as resp:
                    return resp.status < 500
    except Exception:
        return False


def inc_memory_degraded() -> None:
    """Increment the MEMORY_DEGRADED counter (RTR-06: deferred import, never top-level).

    The counter is declared in router/metrics.py. Importing it inside the function body keeps
    this module free of a top-level ``ach_agent.router`` dependency. Silently suppressed on
    any error — a metric must never be the reason an event fails.
    """
    try:
        from ach_agent.router.metrics import MEMORY_DEGRADED

        MEMORY_DEGRADED.inc()
    except Exception:
        pass

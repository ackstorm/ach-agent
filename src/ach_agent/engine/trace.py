# SPDX-License-Identifier: Apache-2.0
"""Trace + session correlation headers for the model proxy.

One agent invocation is N model calls made by the engine subprocess, which never
sets a correlation header of its own. The only per-server channel we control is
the model proxy's ``/t/{token}/…`` route, so this module keeps a token →
correlation registry that :mod:`ach_agent.engine.mcp_proxy` reads on every
forward.

Two grains, two headers:

``traceparent``
    W3C, one per INVOCATION, derived from the event's idempotency key. LiteLLM
    parents its own spans under it (``opentelemetry.py`` ``_get_span_context``,
    priority 2 — the HTTP traceparent header), so every model call of one
    invocation lands in a single Langfuse trace.

``x-agent-session-id``
    Derived from the session_key, so it is stable across the many invocations
    that share a pooled engine server. LiteLLM's generic ``^x-.+-session-id$``
    sniffer (``litellm_pre_call_utils._extract_generic_session_id_from_headers``)
    turns it into ``metadata.session_id`` → Langfuse ``sessionId``.

Header choice is forced by the ACH forwarder: ``internal/forwarder/headers/
strip.go`` deletes every ``x-ach-*`` and ``x-litellm-*`` key, so LiteLLM's
explicit ``x-litellm-session-id`` never survives the hop. The generic vendor form
and ``traceparent`` do.

Independent of cost accounting on purpose — correlation must work with
``cost.source=none``, where no CostAccountant exists at all.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass

# LiteLLM only accepts a session id matching ^[a-zA-Z0-9_\-]{8,}$, so a raw
# session_key ("gitlab:group/repo", "cron:nightly") has to be sanitized.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_SESSION_HEADER = "x-agent-session-id"
_READABLE_PREFIX = 40


@dataclass(slots=True)
class _Entry:
    """One pooled engine server's correlation state."""

    session_id: str
    traceparent: str = ""


# token → correlation. Module-level to mirror mcp_proxy's own `_MODEL_PROXIES`
# registry: the proxy is started by a free function and holds no wiring of ours.
_registry: dict[str, _Entry] = {}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_id_for(session_key: str) -> str:
    """Sanitize ``session_key`` into LiteLLM's session-id shape.

    Keeps a readable prefix so the Langfuse Sessions view is greppable, and
    appends a digest so two keys that sanitize alike stay distinct. Always
    >= 8 chars, so the value never fails LiteLLM's sniffer.
    """
    safe = _UNSAFE.sub("-", session_key)[:_READABLE_PREFIX]
    return f"{safe}-{_digest(session_key)[:8]}"


def traceparent_for(idempotency_key: str) -> str:
    """Build a W3C traceparent that is deterministic in ``idempotency_key``.

    Deterministic rather than random so a trace id can be recomputed offline
    from the inbound delivery id (``X-GitHub-Delivery``, the cron tick, …) and
    pasted straight into Langfuse. sha256 never yields the all-zero trace id
    the spec forbids.
    """
    d = _digest(idempotency_key)
    return f"00-{d[:32]}-{d[32:48]}-01"


def mint_token(session_key: str) -> str:
    """Mint the model proxy's per-server path token and register its session."""
    token = secrets.token_urlsafe(16)
    _registry[token] = _Entry(session_id=session_id_for(session_key))
    return token


def begin(token: str, idempotency_key: str) -> None:
    """Open a new invocation on ``token``'s server (no-op for an unknown token).

    Safe without a lock: the router serializes invocations per session_key
    (RTR-02, one in flight at a time) and a token belongs to exactly one key.
    """
    entry = _registry.get(token)
    if entry is not None:
        entry.traceparent = traceparent_for(idempotency_key)


def drop(token: str) -> None:
    """Forget a token (server stopped/replaced). Idempotent."""
    _registry.pop(token, None)


def headers(token: str) -> dict[str, str]:
    """Correlation headers to inject on a forward, empty for an unknown token."""
    entry = _registry.get(token)
    if entry is None:
        return {}
    out = {_SESSION_HEADER: entry.session_id}
    if entry.traceparent:
        out["traceparent"] = entry.traceparent
    return out


def reset_for_testing() -> None:
    """Clear registry state between tests; production never calls this."""
    _registry.clear()

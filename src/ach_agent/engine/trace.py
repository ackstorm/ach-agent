# SPDX-License-Identifier: Apache-2.0
"""Trace + session correlation headers for the model proxy.

One agent invocation is N model calls AND N tool calls made by the engine
subprocess, which never sets a correlation header of its own. The only
per-server channel we control is the ``/t/{token}/…`` route both localhost
proxies serve, so this module keeps a token → correlation registry that
:func:`ach_agent.engine.mcp_proxy._forward` reads on every forward — model wire
and MCP wire alike, so a tool call lands in the trace of the invocation that
asked for it.

Two grains, two headers:

``traceparent``
    W3C, one per INVOCATION, derived from the event's idempotency key. LiteLLM
    parents its own spans under it (``opentelemetry.py`` ``_get_span_context``,
    priority 2 — the HTTP traceparent header), so every model call of one
    invocation lands in a single Langfuse trace.

``x-agent-session-id``
    The ENGINE's own session id (opencode's ``ses_…``, Pi's session file, …),
    reported by the driver as it resolves the session for a turn. LiteLLM's
    generic ``^x-.+-session-id$`` sniffer
    (``litellm_pre_call_utils._extract_generic_session_id_from_headers``) turns
    it into ``metadata.session_id`` → Langfuse ``sessionId``, so Langfuse groups
    exactly what the agent itself considers one conversation.

    NOT derived from the harness session_key ("gitlab:group/repo", the PR the
    webhook came from): the mapping conv_key → engine session id already lives
    in the pool's persistent session map and in the ``engine: opencode session``
    log line, so the observability backend gets the opaque engine id and no
    workload identifier ever leaves the cluster.

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
from urllib.parse import urlsplit, urlunsplit

import structlog

log = structlog.get_logger(__name__)

# LiteLLM only accepts a session id matching ^[a-zA-Z0-9_\-]{8,}$. Opencode's
# `ses_…` passes as-is; Pi hands back a session FILE PATH, which does not.
_LITELLM_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_SESSION_HEADER = "x-agent-session-id"
_READABLE_PREFIX = 40
_TUI_SESSION_ID = "tui_session"


@dataclass(slots=True)
class _Entry:
    """One pooled engine server's correlation state."""

    session_id: str = ""
    traceparent: str = ""


# token → correlation. Module-level to mirror mcp_proxy's own `_MODEL_PROXIES`
# registry: the proxy is started by a free function and holds no wiring of ours.
_registry: dict[str, _Entry] = {}


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def session_id_for(session_ref: str) -> str:
    """Coerce an engine session ref into LiteLLM's session-id shape.

    A ref LiteLLM already accepts is passed through VERBATIM, so an opencode
    ``ses_…`` copied from a log line is an exact-match search in Langfuse. Only
    a ref that would be rejected (Pi's session file path) is rewritten: readable
    tail for grepping, digest suffix so two files that sanitize alike stay
    distinct. Always >= 8 chars, so the value never fails LiteLLM's sniffer.
    """
    if _LITELLM_SESSION_ID.match(session_ref):
        return session_ref
    tail = session_ref.rsplit("/", 1)[-1]
    return f"{_UNSAFE.sub('-', tail)[:_READABLE_PREFIX]}-{_digest(session_ref)[:8]}"


def traceparent_for(agent: str, channel: str, idempotency_key: str) -> str:
    """Build a W3C traceparent, deterministic in the invocation's identity.

    Deterministic rather than random so a trace id can be recomputed offline
    from the inbound delivery id (``X-GitHub-Delivery``, the cron tick, …) and
    pasted straight into Langfuse. sha256 never yields the all-zero trace id
    the spec forbids.

    Keyed on agent + channel + key, NOT the key alone: an idempotency key is
    only unique WITHIN a channel (the router's own dedup key is
    ``{channel_name}:{idempotency_key}``) and not at all across agents, so two
    agents — or two channels on one agent — handed the same X-GitHub-Delivery
    would otherwise merge into one Langfuse trace. Redeliveries beyond the
    dedup window still collide by construction; that is the same identity, so
    one trace is the right answer.
    """
    d = _digest(f"{agent}:{channel}:{idempotency_key}")
    return f"00-{d[:32]}-{d[32:48]}-01"


def tokenize_url(url: str, token: str) -> str:
    """Insert /t/<token> after the authority: http://h:p/v1 -> http://h:p/t/<tok>/v1.

    The one way a localhost proxy learns WHICH pooled engine server made a call:
    the engine subprocess is handed a per-server URL and never sets a header of
    its own. Applied to every proxied wire the engine is pointed at — the model
    base URL and each MCP server URL — so both land in the same trace/session.
    """
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/t/{token}{parts.path}", parts.query, parts.fragment)
    )


def mint_token() -> str:
    """Mint the model proxy's per-server path token.

    The session id is unknown until the engine resolves one — see
    :func:`set_session`, called by the driver before it sends the turn's prompt.
    """
    token = secrets.token_urlsafe(16)
    _registry[token] = _Entry()
    return token


def set_session(token: str, session_ref: str) -> None:
    """Record the engine's own session id for ``token``'s server.

    Drivers call this as they create/reuse/replace a session, BEFORE the prompt
    that triggers the turn's model calls — otherwise the first turn of every new
    session would ship uncorrelated. No-op for an unknown token or empty ref.
    """
    entry = _registry.get(token)
    if entry is not None and session_ref:
        entry.session_id = session_id_for(session_ref)
        # Correlation is otherwise invisible: the headers are set deep in the proxy
        # forward, so without this line the only way to tell whether a turn was
        # correlated is to query the observability backend. Logged once per session
        # resolution, never per forward. The token is NEVER logged — it is the model
        # proxy's `/t/{token}/` path secret.
        log.info("trace: session", session_id=entry.session_id)


def begin(token: str, agent: str, channel: str, idempotency_key: str) -> None:
    """Open an invocation on ``token``'s server (no-op for an unknown token).

    Safe without a lock because this is single-threaded asyncio and neither
    ``begin`` nor ``headers`` awaits between reading and writing the entry.
    (RTR-02 — one invocation in flight per session_key — is what keeps the
    VALUE meaningful, but it does not cover the reader: ``headers`` runs in the
    proxy's aiohttp handler, a different task from the lane. Do not move either
    off the event loop on RTR-02's authority.)
    """
    entry = _registry.get(token)
    if entry is not None:
        entry.traceparent = traceparent_for(agent, channel, idempotency_key)
        # `trace_id` is the middle W3C field and is what Langfuse indexes: it can be
        # pasted straight into the UI, while the full traceparent cannot. Both are
        # logged — the traceparent so a request can be replayed verbatim, the trace id
        # so an operator can jump to the trace without parsing anything.
        log.info(
            "trace: invocation",
            traceparent=entry.traceparent,
            trace_id=entry.traceparent.split("-")[1],
            channel=channel,
            idempotency_key=idempotency_key,
        )


def begin_tui(token: str) -> None:
    """Correlate a native-TUI console session: one invented trace, fixed session id.

    ``--tui`` has no inbound event to key a trace on and no turn boundary the
    harness can see — opencode attach and Pi native both drive their own loop,
    so ``run_turn`` never executes. So the WHOLE console session is one trace
    (random W3C trace id, minted at launch) under a constant session id. No
    attempt is made to learn the engine's real session: it would take an event
    stream subscription (opencode) or watching ``--session-dir`` (Pi), which is
    a lot of machinery for an interactive dev path.
    """
    entry = _registry.get(token)
    if entry is not None:
        entry.session_id = _TUI_SESSION_ID
        entry.traceparent = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
        log.info(
            "trace: invocation",
            traceparent=entry.traceparent,
            trace_id=entry.traceparent.split("-")[1],
            channel="tui",
            session_id=entry.session_id,
        )


def end(token: str) -> None:
    """Close the invocation: later calls carry the session but no trace.

    Without this a warm pooled server keeps stamping the finished invocation's
    traceparent on anything the engine does between turns (opencode's own
    title/summary/compaction calls, a straggler from the previous turn), which
    lands them in a closed trace. Mirrors the accountant's in_flight window.
    """
    entry = _registry.get(token)
    if entry is not None:
        entry.traceparent = ""


def drop(token: str) -> None:
    """Forget a token (server stopped/replaced). Idempotent."""
    _registry.pop(token, None)


def headers(token: str) -> dict[str, str]:
    """Correlation headers to inject on a forward, empty for an unknown token."""
    entry = _registry.get(token)
    if entry is None:
        return {}
    out: dict[str, str] = {}
    if entry.session_id:
        out[_SESSION_HEADER] = entry.session_id
    if entry.traceparent:
        out["traceparent"] = entry.traceparent
    return out


def reset_for_testing() -> None:
    """Clear registry state between tests; production never calls this."""
    _registry.clear()

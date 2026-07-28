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

``langfuse_session_id``
    The ENGINE's own session id (opencode's ``ses_…``, Pi's session file, …),
    reported by the driver as it resolves the session for a turn, so Langfuse
    groups exactly what the agent itself considers one conversation.

    NOT derived from the harness session_key ("gitlab:group/repo", the PR the
    webhook came from): the mapping conv_key → engine session id already lives
    in the pool's persistent session map and in the ``engine: opencode session``
    log line, so the observability backend gets the opaque engine id and no
    workload identifier ever leaves the cluster.

Two constraints pick these two names, and both were measured — do not "tidy"
them into something that reads better:

1. The ACH forwarder (``internal/forwarder/headers/strip.go``) deletes every
   ``x-ach-*`` and ``x-litellm-*`` key, so LiteLLM's own ``x-litellm-session-id``
   never survives the hop.
2. On the ``/gemini`` PASSTHROUGH, session id is a METADATA concept while
   traceparent is a HEADER one. ``/v1`` fills ``metadata["session_id"]`` from a
   generic ``^x-.+-session-id$`` header sniffer, but nothing in LiteLLM's
   pass-through path ever calls it — that path builds metadata from the API key
   and the request BODY only. A vendor ``x-…-session-id`` therefore works on
   ``/v1`` and is silently dropped on the passthrough. What DOES work on both is
   the ``langfuse_`` prefix: ``LangFuseLogger.add_metadata_from_header`` copies
   every ``langfuse_*`` request header into metadata, reading the raw header dict
   the passthrough does populate. traceparent needs no such help — OTel reads it
   straight off the raw headers on every route.

Send ONLY ``langfuse_session_id``. Adding a vendor ``x-…-session-id`` alongside
it makes ``/v1`` fill the metadata twice and log a "Overwriting Langfuse
session_id" WARNING per request.

⚠ The name carries UNDERSCORES. Prod fronts this with Istio/Envoy, which passes
them; nginx DROPS underscore headers unless ``underscores_in_headers on``. A
dev/e2e nginx shim in front of ACH will therefore lose the session silently while
prod is fine.

Independent of cost accounting on purpose — correlation must work with
``cost.source=none``, where no CostAccountant exists at all.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import structlog

log = structlog.get_logger(__name__)

# The shape a session id is kept in (see `session_id_for`). Opencode's `ses_…`
# passes as-is; Pi hands back a session FILE PATH, which does not.
_CLEAN_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_SESSION_HEADER = "langfuse_session_id"
_READABLE_PREFIX = 40
_TUI_SESSION_ID = "tui_session"

# Every header this module can set, lowercase. `_forward` drops these from the
# INBOUND request unconditionally — not only when it has a value of its own to
# put there — because the engine subprocess never has a legitimate reason to set
# them: correlation is ours to decide. Keying the drop on what we produce instead
# would leave the untokenized routes forwarding an engine-forged `traceparent`
# verbatim, letting a tool call claim any trace it likes.
CORRELATION_HEADERS = frozenset({"traceparent", _SESSION_HEADER})

# The subset that is W3C Trace Context, i.e. what travels in an MCP message's
# `params._meta` as well as on the wire. The session id is ours, not W3C, and has
# no place in that carrier — see :func:`inject_meta`.
_W3C_HEADERS = frozenset({"traceparent", "tracestate"})


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
    """Coerce an engine session ref into a well-behaved session id.

    A ref already in shape is passed through VERBATIM, so an opencode ``ses_…``
    copied from a log line is an exact-match search in Langfuse. Only a ref that
    would not be (Pi's session file path — slashes, dots) is rewritten: readable
    tail for grepping, digest suffix so two files that sanitize alike stay
    distinct.

    The shape is LiteLLM's old sniffer regex ``^[A-Za-z0-9_-]{8,}$``. Nothing
    validates it on the ``langfuse_session_id`` path — it is copied into metadata
    verbatim — so this is now self-imposed: a session id is a URL-visible
    grouping key in Langfuse, and a raw filesystem path is a poor one.
    """
    if _CLEAN_SESSION_ID.match(session_ref):
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


def inject_meta(body: bytes, token: str) -> bytes:
    """Put this token's W3C context in a JSON-RPC message's ``params._meta``.

    The MCP-native way to propagate a trace (SEP-414 / the OTel MCP semconv, and
    what Langfuse documents): an HTTP header correlates the TRANSPORT, but a
    streamable-HTTP session multiplexes many messages over one connection, so the
    span has to parent to the context carried by the MESSAGE or every message
    glues under the session's first request.

    Additive — the header still goes out. LiteLLM reads this carrier only with
    ``LITELLM_OTEL_V2`` enabled (``_mcp_meta_trace_carrier``), so today it is
    inert there; ``_meta`` is reserved by the MCP spec for exactly this, so a
    server that does not understand it must ignore it.

    Anything that is not a JSON-RPC request object is returned untouched:
    correlation must never break a tool call.
    """
    carrier = {name: value for name, value in headers(token).items() if name in _W3C_HEADERS}
    if not body or not carrier:
        return body
    try:
        message = json.loads(body)
    except (ValueError, TypeError):
        return body
    # Requests only (they carry `method`). Responses and JSON-RPC batches are left
    # alone — a batch would need per-message handling nobody has asked for.
    if not isinstance(message, dict) or "method" not in message:
        return body
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
        message["params"] = params
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        params["_meta"] = meta
    meta.update(carrier)
    return json.dumps(message).encode("utf-8")


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

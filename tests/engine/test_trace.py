# SPDX-License-Identifier: Apache-2.0
"""Trace/session correlation headers (ach_agent.engine.trace).

The two consumers of these header values live in other repos. What is asserted
here is a local TRANSCRIPTION of the shapes they require — enough to catch a
value this module could build wrong, not proof the contracts still read this way:

  * LiteLLM's ``langfuse_`` metadata prefix — ``LangFuseLogger.add_metadata_from_header``
    copies every ``langfuse_*`` request header into metadata, which is the ONLY
    session mechanism that works on the ``/gemini`` passthrough as well as on
    ``/v1`` (the passthrough never runs the header sniffer that fills
    ``metadata["session_id"]`` on ``/v1``).
  * The ACH forwarder's header strip — deletes every ``x-ach-*`` and
    ``x-litellm-*`` key (internal/forwarder/headers/strip.go), so a name that
    matches either prefix would silently never reach LiteLLM.

Verified end-to-end 2026-07-28 through the public path (Istio → ach-gateway →
forwarder → LiteLLM): both routes landed the probe's ``langfuse_session_id`` as
the Langfuse ``session_id``.
"""

from __future__ import annotations

import json
import re

import pytest
from structlog.testing import capture_logs

from ach_agent.engine import trace

# Mirrors of the two external contracts above.
LANGFUSE_METADATA_PREFIX = "langfuse_"
CLEAN_SESSION_VALUE_RE = re.compile(r"^[a-zA-Z0-9_\-]{8,}$")
W3C_TRACEPARENT_RE = re.compile(r"^00-[0-9a-f]{32}-[0-9a-f]{16}-0[01]$")
FORWARDER_STRIPPED_PREFIXES = ("x-ach-", "x-litellm-")


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    trace.reset_for_testing()


@pytest.mark.parametrize(
    "session_ref",
    [
        "ses_8a1b2c3d4e5f",  # opencode
        "/home/agent/pi/1a2b3c4d/sessions/2026-07-28T09-14-02.json",  # pi
        "/s/a.json",  # short tail — below LiteLLM's 8-char floor on its own
        "ünïcode.json",
        "x" * 300,
    ],
)
def test_session_id_always_satisfies_litellm(session_ref: str) -> None:
    assert CLEAN_SESSION_VALUE_RE.match(trace.session_id_for(session_ref))


def test_an_acceptable_ref_is_passed_through_verbatim() -> None:
    # An oc_session_id copied from the `engine: opencode session` log line has to
    # be an EXACT-match search in Langfuse, so it must not be rewritten.
    assert trace.session_id_for("ses_8a1b2c3d4e5f") == "ses_8a1b2c3d4e5f"


def test_two_pi_session_files_that_sanitize_alike_stay_distinct() -> None:
    a = trace.session_id_for("/home/agent/pi/aaaa/sessions/s.json")
    b = trace.session_id_for("/home/agent/pi/bbbb/sessions/s.json")
    assert a != b, "the tail is identical; only the digest of the full ref separates them"


def test_headers_survive_the_forwarder_strip() -> None:
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    trace.begin(token, "a1", "webhook", "delivery-1")
    assert trace.headers(token), "an empty dict would pass this vacuously"
    for name in trace.headers(token):
        assert not name.lower().startswith(FORWARDER_STRIPPED_PREFIXES), (
            f"{name} would be deleted by internal/forwarder/headers/strip.go"
        )


def test_the_session_header_uses_litellms_metadata_prefix() -> None:
    """The `langfuse_` prefix is what makes this work on the passthrough too.

    A vendor `x-…-session-id` name reaches `metadata["session_id"]` only on /v1,
    where LiteLLM runs a header sniffer; the pass-through path never calls it and
    the session is silently lost. `langfuse_*` headers are copied into metadata on
    BOTH.
    """
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    sent = trace.headers(token)
    session_headers = [name for name in sent if name != "traceparent"]
    assert session_headers == ["langfuse_session_id"], (
        "exactly one session header: sending a vendor x-…-session-id alongside it "
        "makes /v1 fill the metadata twice and log a warning per request"
    )
    assert session_headers[0].startswith(LANGFUSE_METADATA_PREFIX)


def test_traceparent_is_w3c_and_deterministic_in_the_invocation() -> None:
    assert W3C_TRACEPARENT_RE.match(trace.traceparent_for("a1", "webhook", "8f3a-delivery"))
    assert trace.traceparent_for("a1", "webhook", "8f3a") == trace.traceparent_for(
        "a1", "webhook", "8f3a"
    )
    assert trace.traceparent_for("a1", "webhook", "8f3a") != trace.traceparent_for(
        "a1", "webhook", "8f3b"
    )


def test_same_delivery_id_on_two_agents_or_channels_is_two_traces() -> None:
    # An idempotency key is unique only within a channel (the router's own dedup
    # key is "{channel}:{key}") and not at all across agents, so keying the trace
    # on it alone would merge unrelated invocations into one Langfuse trace.
    same = "X-GitHub-Delivery-1"
    assert trace.traceparent_for("a1", "webhook", same) != trace.traceparent_for(
        "a2", "webhook", same
    )
    assert trace.traceparent_for("a1", "webhook", same) != trace.traceparent_for(
        "a1", "gitlab", same
    )


def test_one_invocation_is_one_trace_across_many_calls() -> None:
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    trace.begin(token, "a1", "gitlab", "delivery-1")
    first = trace.headers(token)
    # Every model call of the same invocation reads the same registry entry.
    assert trace.headers(token) == first

    # A second comment on the same MR reuses the engine session but is a NEW
    # invocation: same sessionId, different trace — never yesterday's.
    trace.begin(token, "a1", "gitlab", "delivery-2")
    second = trace.headers(token)
    assert second["traceparent"] != first["traceparent"], "a new invocation is a new trace"
    assert second["langfuse_session_id"] == first["langfuse_session_id"], "session outlives it"


def test_a_replaced_engine_session_replaces_the_session_id() -> None:
    # Non-reuse turns and opencode's 404-recreate both hand the driver a new ref.
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    trace.set_session(token, "ses_0002")
    assert trace.headers(token)["langfuse_session_id"] == "ses_0002"


def test_no_headers_before_the_engine_resolves_anything() -> None:
    # A model call that somehow arrives before the driver has a session must not
    # carry a half-built traceparent, nor a session id we invented.
    token = trace.mint_token()
    assert trace.headers(token) == {}


def test_set_session_ignores_unknown_tokens_and_empty_refs() -> None:
    token = trace.mint_token()
    trace.set_session(token, "")
    assert trace.headers(token) == {}
    trace.set_session("never-minted", "ses_0001")  # no KeyError, nothing registered


def test_unknown_token_yields_no_headers() -> None:
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    trace.drop(token)
    assert trace.headers(token) == {}
    assert trace.headers("never-minted") == {}
    trace.drop(token)  # idempotent


def test_end_closes_the_trace_but_keeps_the_session() -> None:
    # A warm pooled server keeps serving between invocations (opencode's own
    # title/summary calls); those must not land in the finished trace.
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    trace.begin(token, "a1", "gitlab", "delivery-1")
    assert "traceparent" in trace.headers(token)

    trace.end(token)
    assert trace.headers(token) == {"langfuse_session_id": "ses_0001"}

    trace.begin(token, "a1", "gitlab", "delivery-2")
    assert "traceparent" in trace.headers(token), "the next invocation reopens it"


def test_end_on_an_unknown_token_is_a_no_op() -> None:
    trace.end("never-minted")


def test_tui_gets_an_invented_trace_and_a_constant_session() -> None:
    # --tui has no inbound event and no visible turn boundary, so the console
    # session is one invented trace under a fixed session id.
    a, b = trace.mint_token(), trace.mint_token()
    trace.begin_tui(a)
    trace.begin_tui(b)
    ha, hb = trace.headers(a), trace.headers(b)

    assert W3C_TRACEPARENT_RE.match(ha["traceparent"])
    assert ha["traceparent"] != hb["traceparent"], "invented per launch, not derived"
    assert ha["langfuse_session_id"] == hb["langfuse_session_id"] == "tui_session"
    assert CLEAN_SESSION_VALUE_RE.match(ha["langfuse_session_id"])


def test_begin_tui_on_an_unknown_token_is_a_no_op() -> None:
    trace.begin_tui("never-minted")


def test_tokenize_url_inserts_the_token_after_the_authority() -> None:
    assert trace.tokenize_url("http://127.0.0.1:9/v1", "T") == "http://127.0.0.1:9/t/T/v1"


def test_every_proxied_wire_tokenizes_the_same_way() -> None:
    # The model wire and an MCP wire must produce the SAME token segment, or a tool
    # call lands outside its invocation's trace — the defect this shape fixes.
    token = trace.mint_token()
    model = trace.tokenize_url("http://127.0.0.1:45495/gemini/v1beta", token)
    mcp = trace.tokenize_url("http://127.0.0.1:35057/mcp/mcp-zoho-desk", token)

    assert model == f"http://127.0.0.1:45495/t/{token}/gemini/v1beta"
    assert mcp == f"http://127.0.0.1:35057/t/{token}/mcp/mcp-zoho-desk"


def test_an_untokenized_wire_yields_no_correlation_rather_than_an_error() -> None:
    # The plain /mcp/<id> and /v1 routes stay valid: they forward uncorrelated.
    assert trace.headers("") == {}


def test_the_logged_trace_id_is_the_one_langfuse_indexes() -> None:
    # The operator pastes trace_id (the middle W3C field), never the traceparent,
    # into Langfuse — a wrong slice here makes the log line silently useless.
    token = trace.mint_token()
    with capture_logs() as logs:
        trace.begin(token, "agent", "webhook", "delivery-1")

    entry = next(record for record in logs if record["event"] == "trace: invocation")
    traceparent = trace.headers(token)["traceparent"]
    assert entry["traceparent"] == traceparent
    assert entry["trace_id"] == traceparent.split("-")[1]
    assert re.fullmatch(r"[0-9a-f]{32}", entry["trace_id"])


def test_the_session_id_is_logged_as_it_is_sent() -> None:
    token = trace.mint_token()
    with capture_logs() as logs:
        trace.set_session(token, "ses_0583b1827ffeaLtpVshBDEtCfe")

    entry = next(record for record in logs if record["event"] == "trace: session")
    assert entry["session_id"] == trace.headers(token)["langfuse_session_id"]


def test_the_proxy_path_token_is_never_logged() -> None:
    # `token` is the model proxy's /t/{token}/ path secret: anything that reaches
    # a log line here would leak a working route into stdout.
    token = trace.mint_token()
    with capture_logs() as logs:
        trace.begin(token, "agent", "webhook", "delivery-1")
        trace.set_session(token, "ses_0583b1827ffeaLtpVshBDEtCfe")
        trace.begin_tui(token)

    assert logs, "expected the correlation log lines"
    assert token not in repr(logs)


def _call(**params: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}).encode()


def test_inject_meta_carries_the_w3c_context_in_the_message() -> None:
    """SEP-414: the MCP span parents to the MESSAGE's context, not the transport's."""
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")

    out = json.loads(trace.inject_meta(_call(name="search"), token))

    assert out["params"]["_meta"]["traceparent"] == trace.traceparent_for(
        "agent", "webhook", "delivery-1"
    )
    assert out["params"]["name"] == "search", "the caller's params must survive intact"


def test_inject_meta_does_not_carry_the_session_id() -> None:
    """`_meta` is a W3C Trace Context carrier; the session id is ours, not W3C."""
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    trace.set_session(token, "ses_0583b1827ffeaLtpVshBDEtCfe")

    meta = json.loads(trace.inject_meta(_call(name="search"), token))["params"]["_meta"]

    assert set(meta) == {"traceparent"}


def test_inject_meta_creates_params_when_the_request_has_none() -> None:
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()

    out = json.loads(trace.inject_meta(body, token))

    assert out["params"]["_meta"]["traceparent"]


def test_inject_meta_never_breaks_a_call() -> None:
    """Correlation is best-effort: anything unparseable or non-request passes through."""
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")

    assert trace.inject_meta(b"", token) == b""
    assert trace.inject_meta(b"not json", token) == b"not json"
    assert trace.inject_meta(b"[1, 2]", token) == b"[1, 2]"  # batch: left alone
    response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
    assert trace.inject_meta(response, token) == response


def test_inject_meta_is_a_no_op_without_an_invocation() -> None:
    """Between turns trace.end clears the traceparent — nothing to propagate."""
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    trace.end(token)

    body = _call(name="search")
    assert trace.inject_meta(body, token) == body
    assert trace.inject_meta(body, "never-minted") == body

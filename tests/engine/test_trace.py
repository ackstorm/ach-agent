# SPDX-License-Identifier: Apache-2.0
"""Trace/session correlation headers (ach_agent.engine.trace).

The two consumers of these header values live in other repos. What is asserted
here is a local TRANSCRIPTION of the shapes they require — enough to catch a
value this module could build wrong, not proof the contracts still read this way:

  * LiteLLM's generic session sniffer — ``^x-.+-session-id$`` header name,
    ``^[a-zA-Z0-9_\\-]{8,}$`` value (litellm/proxy/litellm_pre_call_utils.py).
  * The ACH forwarder's header strip — deletes every ``x-ach-*`` and
    ``x-litellm-*`` key (internal/forwarder/headers/strip.go), so a name that
    matches either prefix would silently never reach LiteLLM.
"""

from __future__ import annotations

import re

import pytest
from structlog.testing import capture_logs

from ach_agent.engine import trace

# Mirrors of the two external contracts above.
LITELLM_SESSION_HEADER_RE = re.compile(r"^x-.+-session-id$", re.IGNORECASE)
LITELLM_SESSION_VALUE_RE = re.compile(r"^[a-zA-Z0-9_\-]{8,}$")
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
    assert LITELLM_SESSION_VALUE_RE.match(trace.session_id_for(session_ref))


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


def test_session_header_name_matches_litellms_sniffer() -> None:
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    assert LITELLM_SESSION_HEADER_RE.match("x-agent-session-id")
    assert "x-agent-session-id" in trace.headers(token)


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
    assert second["x-agent-session-id"] == first["x-agent-session-id"], "session outlives it"


def test_a_replaced_engine_session_replaces_the_session_id() -> None:
    # Non-reuse turns and opencode's 404-recreate both hand the driver a new ref.
    token = trace.mint_token()
    trace.set_session(token, "ses_0001")
    trace.set_session(token, "ses_0002")
    assert trace.headers(token)["x-agent-session-id"] == "ses_0002"


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
    assert trace.headers(token) == {"x-agent-session-id": "ses_0001"}

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
    assert ha["x-agent-session-id"] == hb["x-agent-session-id"] == "tui_session"
    assert LITELLM_SESSION_VALUE_RE.match(ha["x-agent-session-id"])


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
    assert entry["session_id"] == trace.headers(token)["x-agent-session-id"]


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

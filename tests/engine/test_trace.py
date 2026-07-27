# SPDX-License-Identifier: Apache-2.0
"""Trace/session correlation headers (ach_agent.engine.trace).

The two consumers of these header values live in other repos, so the shapes they
require are asserted here rather than trusted:

  * LiteLLM's generic session sniffer — ``^x-.+-session-id$`` header name,
    ``^[a-zA-Z0-9_\\-]{8,}$`` value (litellm/proxy/litellm_pre_call_utils.py).
  * The ACH forwarder's header strip — deletes every ``x-ach-*`` and
    ``x-litellm-*`` key (internal/forwarder/headers/strip.go), so a name that
    matches either prefix would silently never reach LiteLLM.
"""

from __future__ import annotations

import re

import pytest

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
    "session_key",
    [
        "gitlab:mygroup/myrepo",
        "cron:nightly-report",
        "tui-console",
        "a",  # shorter than LiteLLM's 8-char floor on its own
        "ünïcode/kéy",
        "x" * 200,
    ],
)
def test_session_id_always_satisfies_litellm(session_key: str) -> None:
    assert LITELLM_SESSION_VALUE_RE.match(trace.session_id_for(session_key))


def test_session_id_is_stable_and_distinct() -> None:
    assert trace.session_id_for("gitlab:a/b") == trace.session_id_for("gitlab:a/b")
    # Both sanitize to "gitlab-a-b"; the digest suffix keeps them apart.
    assert trace.session_id_for("gitlab:a/b") != trace.session_id_for("gitlab/a:b")


def test_headers_survive_the_forwarder_strip() -> None:
    token = trace.mint_token("tui-console")
    trace.begin(token, "delivery-1")
    for name in trace.headers(token):
        assert not name.lower().startswith(FORWARDER_STRIPPED_PREFIXES), (
            f"{name} would be deleted by internal/forwarder/headers/strip.go"
        )


def test_session_header_name_matches_litellms_sniffer() -> None:
    token = trace.mint_token("tui-console")
    assert LITELLM_SESSION_HEADER_RE.match("x-agent-session-id")
    assert "x-agent-session-id" in trace.headers(token)


def test_traceparent_is_w3c_and_deterministic_in_the_idempotency_key() -> None:
    assert W3C_TRACEPARENT_RE.match(trace.traceparent_for("8f3a-delivery"))
    assert trace.traceparent_for("8f3a") == trace.traceparent_for("8f3a")
    assert trace.traceparent_for("8f3a") != trace.traceparent_for("8f3b")


def test_one_invocation_is_one_trace_across_many_calls() -> None:
    token = trace.mint_token("gitlab:a/b")
    trace.begin(token, "delivery-1")
    first = trace.headers(token)
    # Every model call of the same invocation reads the same registry entry.
    assert trace.headers(token) == first

    trace.begin(token, "delivery-2")
    second = trace.headers(token)
    assert second["traceparent"] != first["traceparent"], "a new invocation is a new trace"
    assert second["x-agent-session-id"] == first["x-agent-session-id"], "session outlives it"


def test_no_traceparent_before_the_first_invocation() -> None:
    # A model call that somehow arrives before begin() still gets the session,
    # and must not carry a half-built traceparent.
    token = trace.mint_token("tui-console")
    assert trace.headers(token) == {"x-agent-session-id": trace.session_id_for("tui-console")}


def test_unknown_token_yields_no_headers() -> None:
    token = trace.mint_token("tui-console")
    trace.drop(token)
    assert trace.headers(token) == {}
    assert trace.headers("never-minted") == {}
    trace.drop(token)  # idempotent

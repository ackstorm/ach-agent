# SPDX-License-Identifier: Apache-2.0
"""ach-memory client: project resolution, standing context, the typed-retain spec."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

import ach_agent.memory.ach_memory as am
from ach_agent.config.schema import AchMemoryMemory, AchMemoryParams
from ach_agent.engine.hydrate import McpServer


def _cfg(**kw: object) -> AchMemoryMemory:
    return AchMemoryMemory.model_validate(
        {"type": "ach-memory", "achMemory": {"endpoint": "http://m", **kw}}
    )


# ---------------------------------------------------------------------------
# resolve_project — one bank per AGENT
# ---------------------------------------------------------------------------


def test_project_is_namespace_and_agent_name(monkeypatch) -> None:
    monkeypatch.setenv(am.NAMESPACE_ENV, "ach")
    assert am.resolve_project(AchMemoryParams(endpoint="http://m"), "gitlab-pr") == "ach-gitlab-pr"


def test_project_falls_back_to_the_agent_name_without_a_namespace(monkeypatch) -> None:
    monkeypatch.delenv(am.NAMESPACE_ENV, raising=False)
    assert am.resolve_project(AchMemoryParams(endpoint="http://m"), "gitlab-pr") == "gitlab-pr"


def test_an_explicit_project_overrides_the_derived_one(monkeypatch) -> None:
    monkeypatch.setenv(am.NAMESPACE_ENV, "ach")
    params = AchMemoryParams(endpoint="http://m", project="Shared Bank")
    assert am.resolve_project(params, "gitlab-pr") == "shared-bank"


def test_the_slug_is_pre_normalized_the_way_the_server_would(monkeypatch) -> None:
    """ach-memory's normalize_slug collapses '/' to '-' and lowercases. Pre-normalising
    means both ends agree on ONE string instead of the server silently rewriting ours."""
    monkeypatch.setenv(am.NAMESPACE_ENV, "ACH/Prod")
    assert am.resolve_project(AchMemoryParams(endpoint="http://m"), "GitLab_PR") == (
        "ach-prod-gitlab-pr"
    )


# ---------------------------------------------------------------------------
# fetch_context — server-assembled text, taken verbatim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_context_text_is_wrapped_and_never_re_rendered(monkeypatch) -> None:
    """ContextPayload.text is already ordered, budgeted and heading-safe."""
    captured: dict[str, object] = {}

    async def fake_call(endpoint, headers, tool, args):
        captured["tool"], captured["args"] = tool, args
        return json.dumps(
            {
                "text": "User · user-context\nPrefers uv.",
                "total_tokens": 5,
                "omissions": [],
                "overages": [],
            }
        )

    monkeypatch.setattr(am, "call_ach_memory", fake_call)
    section = await am.fetch_context("http://m", {}, "ach-gitlab-pr")

    assert section == "## Memory\n\nUser · user-context\nPrefers uv."
    assert captured["tool"] == am.ACH_MEMORY_LOAD_CONTEXT
    assert captured["args"] == {"project_slug": "ach-gitlab-pr"}


@pytest.mark.asyncio
async def test_load_context_failure_degrades_to_a_note(monkeypatch) -> None:
    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(am, "call_ach_memory", boom)
    section = await am.fetch_context("http://m", {}, "ach-gitlab-pr")
    assert section.startswith("## Memory") and "Unavailable" in section


@pytest.mark.asyncio
async def test_empty_context_is_not_an_error(monkeypatch) -> None:
    async def empty(*a, **k):
        return json.dumps({"text": "", "total_tokens": 0})

    monkeypatch.setattr(am, "call_ach_memory", empty)
    assert "No standing context" in await am.fetch_context("http://m", {}, "p")


# ---------------------------------------------------------------------------
# prepare_ach_memory — every branch fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_backend_is_fail_open(monkeypatch) -> None:
    """No probe to stub: load_context IS the reachability test, so an outage looks like this."""

    async def boom(endpoint, headers, tool, args):
        raise OSError("connection refused")

    monkeypatch.setattr(am, "call_ach_memory", boom)
    ok, section = await am.prepare_ach_memory(_cfg(), "ach-gitlab-pr", {})
    assert ok is False
    assert "Unavailable" in section


@pytest.mark.asyncio
async def test_reachable_backend_returns_the_context_section(monkeypatch) -> None:
    async def fake_fetch(endpoint, headers, project):
        return "## Memory\n\nok"

    monkeypatch.setattr(am, "fetch_context", fake_fetch)
    assert await am.prepare_ach_memory(_cfg(), "p", {}) == (True, "## Memory\n\nok")


# ---------------------------------------------------------------------------
# resolve_ach_memory_auth — two ways in, and they are not the same credential
# ---------------------------------------------------------------------------


def test_auth_absent_sends_no_header() -> None:
    assert am.resolve_ach_memory_auth(None, "ek_x") == (True, {})


def test_auth_ach_uses_the_ek_as_x_ach_key() -> None:
    """ACH's auth scheme IS the header — an Authorization: Bearer 401s there."""
    from ach_agent.config.schema import AchMemoryAuthAch

    ok, headers = am.resolve_ach_memory_auth(AchMemoryAuthAch(type="ach"), "ek_x")
    assert (ok, headers) == (True, {"x-ach-key": "ek_x"})


def test_auth_ach_without_an_ek_degrades_rather_than_calling_anonymously() -> None:
    from ach_agent.config.schema import AchMemoryAuthAch

    assert am.resolve_ach_memory_auth(AchMemoryAuthAch(type="ach"), None) == (False, {})


def test_auth_bearer_uses_the_configured_user_key(monkeypatch) -> None:
    from ach_agent.config.schema import AchMemoryAuthBearer

    monkeypatch.setenv("MEM_TOK", "sekret")
    auth = AchMemoryAuthBearer(type="bearer", env="MEM_TOK")
    assert am.resolve_ach_memory_auth(auth, "ek_x") == (True, {"Authorization": "Bearer sekret"})


def test_auth_bearer_with_an_unset_env_degrades(monkeypatch) -> None:
    """Configured-but-unset must never collapse into an anonymous call to a real backend."""
    from ach_agent.config.schema import AchMemoryAuthBearer

    monkeypatch.delenv("MEM_TOK", raising=False)
    auth = AchMemoryAuthBearer(type="bearer", env="MEM_TOK")
    assert am.resolve_ach_memory_auth(auth, "ek_x") == (False, {})


def test_auth_bearer_on_a_named_header_sends_the_raw_secret(monkeypatch) -> None:
    """ach-memory's platform provider reads whatever MEMORY_AUTH_PLATFORM_INCOMING_HEADER
    names, and forwards that value to its resolver. `Bearer ` belongs to `Authorization`
    alone — prepending it here would hand LiteLLM `Bearer sk-…` as if it were the key."""
    from ach_agent.config.schema import AchMemoryAuthBearer

    monkeypatch.setenv("MEM_TOK", "sk-abc")
    auth = AchMemoryAuthBearer(type="bearer", env="MEM_TOK", header="x-litellm-api-key")
    assert am.resolve_ach_memory_auth(auth, "ek_x") == (True, {"x-litellm-api-key": "sk-abc"})


def test_auth_bearer_header_rejects_header_injection() -> None:
    """Boot is the place to refuse CRLF in a header name, not the HTTP client."""
    import pydantic

    from ach_agent.config.schema import AchMemoryAuthBearer

    with pytest.raises(pydantic.ValidationError):
        AchMemoryAuthBearer(type="bearer", env="MEM_TOK", header="X-Bad\r\nInjected: 1")


# ---------------------------------------------------------------------------
# TOOLS_SPEC — the typed-retain contract
# ---------------------------------------------------------------------------


def test_tools_spec_states_the_retain_contract() -> None:
    """ach-memory REJECTS a retain missing any of these, so a spec that omits them
    produces an agent whose every retain bounces on validation."""
    spec = am.TOOLS_SPEC
    for required in ("memory_type", "basis", "trigger", "evidence"):
        assert required in spec
    for literal in ("convention", "gotcha", "human_explicit", "agent_proactive", "user_quote"):
        assert literal in spec
    assert "English" in spec
    assert "4 KiB" in spec


def test_tools_spec_never_mentions_scope_or_project_slug() -> None:
    """The agent does not pass them, so naming them only invites an attempt."""
    assert "project_slug" not in am.TOOLS_SPEC
    assert "scope=" not in am.TOOLS_SPEC


# ---------------------------------------------------------------------------
# the endpoint is the operator's, verbatim
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_configured_endpoint_is_used_verbatim(monkeypatch) -> None:
    """The operator configures the COMPLETE MCP endpoint. Appending `/mcp` here is how a
    gateway URL like `.../mcp/ach-memory` becomes `.../mcp/ach-memory/mcp/` and 404s — the
    same scar ach-memory's own cli.py carries. Trailing slash is the operator's call too."""
    seen: dict[str, object] = {}

    class _Result:
        content = ()
        isError = False

    class _Session:
        async def call_tool(self, tool, args):
            return _Result()

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session(endpoint, headers):
        seen["endpoint"], seen["headers"] = endpoint, headers
        yield _Session()

    monkeypatch.setattr(am, "mcp_session", fake_session)
    for configured in (
        "https://api.ackstorm.ai/mcp/ach-memory",
        "https://api.ackstorm.ai/mcp/ach-memory/",
        "http://ach-memory.svc:8000/mcp/",
    ):
        await am.call_ach_memory(configured, {"x-ach-key": "ek_x"}, "recall", {})
        assert seen["endpoint"] == configured


@pytest.mark.asyncio
async def test_every_call_carries_the_credential_and_an_identity(monkeypatch) -> None:
    """Same attribution as every other ACH hop (mcp_proxy, hydrate, a2a egress)."""
    seen: dict[str, object] = {}

    class _Result:
        content = ()
        isError = False

    class _Session:
        async def call_tool(self, tool, args):
            return _Result()

    import contextlib

    @contextlib.asynccontextmanager
    async def fake_session(endpoint, headers):
        seen["headers"] = headers
        yield _Session()

    monkeypatch.setattr(am, "mcp_session", fake_session)
    await am.call_ach_memory("http://m/mcp/", {"x-ach-key": "ek_x"}, "recall", {})

    headers = seen["headers"]
    assert headers["x-ach-key"] == "ek_x"
    assert "x-ach-agent" in headers and "x-ach-environment" in headers


# ---------------------------------------------------------------------------
# resolve_endpoint / excluded_mcp_server — one service, ONE path
# ---------------------------------------------------------------------------


def test_endpoint_comes_from_the_hydrated_server_when_named_by_id() -> None:
    """`mcpServerId` reads the address out of the same manifest that granted the server, so
    the facade cannot end up pointed somewhere ACH never handed this agent."""
    servers = [
        McpServer(id="gitlab", endpoint="https://api.ackstorm.ai/mcp/gitlab"),
        McpServer(id="ach-memory", endpoint="https://api.ackstorm.ai/mcp/ach-memory"),
    ]
    params = AchMemoryParams.model_validate({"mcpServerId": "ach-memory"})
    assert am.resolve_endpoint(params, servers) == "https://api.ackstorm.ai/mcp/ach-memory"


def test_an_unhydrated_server_id_yields_no_endpoint() -> None:
    """Absent from the manifest means not reachable. '' degrades memory (fail-open, D-02)
    rather than guessing a URL for a backend this environment did not grant."""
    params = AchMemoryParams.model_validate({"mcpServerId": "ach-memory"})
    assert am.resolve_endpoint(params, [McpServer(id="gitlab", endpoint="https://g")]) == ""


def test_endpoint_and_mcp_server_id_are_mutually_exclusive() -> None:
    """Two sources of truth for one address: the facade could front the URL while the
    exclusion aimed at a different server, leaving exactly the hole mcpServerId closes."""
    with pytest.raises(ValidationError):
        AchMemoryParams.model_validate({"endpoint": "http://m", "mcpServerId": "ach-memory"})
    with pytest.raises(ValidationError):
        AchMemoryParams.model_validate({})


def test_the_fronted_server_is_excluded_regardless_of_auth_or_endpoint() -> None:
    """THE point of the field. main() applies this BEFORE resolving auth or the endpoint, so
    a facade that never starts still removes the raw server: a memory degrade must mean no
    memory, never ach-memory's full unscoped surface with the ek_ attached."""
    cfg = AchMemoryMemory.model_validate(
        {"type": "ach-memory", "achMemory": {"mcpServerId": "ach-memory"}}
    )
    assert am.excluded_mcp_server(cfg) == "ach-memory"


def test_an_explicit_endpoint_excludes_nothing() -> None:
    """No id, nothing to exclude — and with no `memory` block at all, a hydrated ach-memory is
    proxied like any other server the operator granted. Exclude it iff we front it."""
    assert am.excluded_mcp_server(_cfg()) == ""
    assert am.excluded_mcp_server(None) == ""

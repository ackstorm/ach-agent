# SPDX-License-Identifier: MIT
"""facades/memory.py -- the exact five typed ach-memory tools over the real `mcp` SDK,
fixed project/scope injection, and raw-MCP-server exclusion even when degraded
(contract §5/§10; old-ach-agent memory/{ach_memory,ach_memory_facade}.py).
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ach_runtime.config.schema import AchMemoryAuthAch, AchMemoryAuthBearer
from ach_runtime.facades import _mcp_session
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.memory import (
    AchMemoryFacade,
    excluded_mcp_server_ids,
    resolve_ach_memory_auth,
    resolve_endpoint,
)
from ach_runtime.harness.tokens import facade_token
from fixtures.memory_mcp import RecordingUpstream

KEY = b"fixture-key"
IDENTITY = ProcessIdentity(agent="classifier", environment="platform")

# The upstream fixture is loopback-bound. The Harness facade also serves cluster DNS;
# the regression cases below exercise both addresses using the real MCP transport.
UPSTREAM = "http://localhost:8080/"
FACADE_BASE = "http://localhost:8081"


async def _owned(candidate: str) -> bool:
    return candidate == "owned-ws"


def _flipping(claim: str = "owned-ws") -> Any:
    """Owned-live callback that starts True and flips to False after each call -- proves
    ownership is re-checked per request, never cached (review focus 1)."""
    state = {"owned": True, "count": 0}

    async def owned(candidate: str) -> bool:
        state["count"] += 1
        current = state["owned"] and candidate == claim
        state["owned"] = False
        return current

    owned.state = state  # type: ignore[attr-defined]
    return owned


def _count_mounted_sdk_delegation(app: Any) -> list[str]:
    """Count requests reaching the real mounted FastMCP app after its auth guard."""
    guarded = app.routes[0].app
    mounted_sdk_app = guarded._app
    methods: list[str] = []

    async def observe(scope: Any, receive: Any, send: Any) -> None:
        methods.append(scope.get("method", ""))
        await mounted_sdk_app(scope, receive, send)

    guarded._app = observe
    return methods


def _togglable(claim: str = "owned-ws") -> Any:
    """Owned-live callback that stays True until the test explicitly flips `.state["owned"]`
    -- unlike `_flipping` above (which flips on every request, proving per-request
    re-verification), this lets a test perform a full successful call BEFORE revoking
    ownership (review I2)."""
    state = {"owned": True}

    async def owned(candidate: str) -> bool:
        return state["owned"] and candidate == claim

    owned.state = state  # type: ignore[attr-defined]
    return owned


@contextlib.asynccontextmanager
async def _routed_to(
    monkeypatch: pytest.MonkeyPatch,
    app: Any,
    *,
    capture_bodies: list[bytes] | None = None,
    capture_requests: list[httpx.Request] | None = None,
) -> AsyncIterator[None]:
    """Point the facade's OWN outbound call (facade -> upstream ach-memory) at an
    in-process fixture via ASGITransport -- replacing only the network transport, same as
    test_memory_client.py's `_routed_to`. Also runs the fixture app's own session-manager
    lifespan, required for its streamable-HTTP transport to serve any request at all.

    `capture_bodies`, when given, records every outgoing request body VERBATIM -- before
    FastMCP on the upstream side binds it against the tool's own Python defaults, which is
    the only way to tell an omitted field from an explicit wire null (review I4)."""

    def _patched(headers: dict[str, str] | None = None, **_: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=UPSTREAM, headers=headers
        )
        if capture_bodies is not None or capture_requests is not None:

            async def _hook(request: httpx.Request) -> None:
                if capture_bodies is not None:
                    capture_bodies.append(request.content)
                if capture_requests is not None:
                    capture_requests.append(request)

            client.event_hooks["request"] = [_hook]
        return client

    monkeypatch.setattr(_mcp_session, "create_mcp_http_client", _patched)
    async with app.router.lifespan_context(app):
        yield


@contextlib.asynccontextmanager
async def _client_session(
    app: Any, token: str, *, session_ids: list[str] | None = None,
    base_url: str = FACADE_BASE,
) -> AsyncIterator[ClientSession]:
    """A real, independently-constructed MCP client session against the facade under
    test -- deliberately NOT routed through the (possibly monkeypatched) production
    `_mcp_session.mcp_session`, so it never collides with `_routed_to` above."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as http_client:
        async with streamable_http_client(
            f"{base_url}/t/{token}/memory/", http_client=http_client
        ) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if session_ids is not None:
                    session_id = get_session_id()
                    assert session_id
                    session_ids.append(session_id)
                yield session


def _facade(
    *,
    endpoint: str = UPSTREAM,
    owned_workspace: Any = None,
    project: str = "acme-agent",
    headers: dict[str, str] | None = None,
) -> AchMemoryFacade:
    return AchMemoryFacade(
        endpoint=endpoint,
        headers=headers if headers is not None else {"x-ach-key": "ek-xyz"},
        project=project,
        key=KEY,
        owned_workspace=owned_workspace or _owned,
        identity=IDENTITY,
    )


# --- pure config-resolution helpers (re-exported from _memory_client) --------------


def test_resolve_endpoint_prefers_explicit_endpoint() -> None:
    assert (
        resolve_endpoint(
            endpoint="https://direct.example.com/mcp",
            mcp_server_id="",
            mcp_servers={"mem": "https://hydrated.example.com/mcp/mem"},
        )
        == "https://direct.example.com/mcp"
    )


def test_resolve_endpoint_falls_back_to_hydrated_server_id() -> None:
    assert (
        resolve_endpoint(
            endpoint="", mcp_server_id="mem", mcp_servers={"mem": "https://hydrated.example.com/mcp/mem"}
        )
        == "https://hydrated.example.com/mcp/mem"
    )


def test_resolve_endpoint_empty_when_neither_resolves() -> None:
    assert resolve_endpoint(endpoint="", mcp_server_id="missing", mcp_servers={}) == ""


def test_excluded_mcp_server_ids_by_explicit_server_id() -> None:
    assert excluded_mcp_server_ids(mcp_server_id="mem", endpoint="", mcp_servers={}) == {"mem"}


def test_excluded_mcp_server_ids_reconciles_explicit_endpoint_against_hydrated_catalog() -> None:
    servers = {"raw-mem": "https://ach.example.com/mcp/raw-mem", "other": "https://ach.example.com/mcp/other"}
    result = excluded_mcp_server_ids(
        mcp_server_id="", endpoint="https://ach.example.com/mcp/raw-mem", mcp_servers=servers
    )
    assert result == {"raw-mem"}


def test_excluded_mcp_server_ids_empty_when_neither_configured() -> None:
    assert excluded_mcp_server_ids(mcp_server_id="", endpoint="", mcp_servers={}) == set()


def test_resolve_ach_memory_auth_none_configured_is_ok_with_no_headers() -> None:
    ok, headers = resolve_ach_memory_auth(None, "ek-abc")
    assert ok is True
    assert headers == {}


def test_resolve_ach_memory_auth_ach_type_uses_ek() -> None:
    ok, headers = resolve_ach_memory_auth(AchMemoryAuthAch(type="ach"), "ek-abc")
    assert ok is True
    assert headers == {"x-ach-key": "ek-abc"}


def test_resolve_ach_memory_auth_ach_type_degrades_without_ek() -> None:
    ok, headers = resolve_ach_memory_auth(AchMemoryAuthAch(type="ach"), None)
    assert ok is False
    assert headers == {}


def test_resolve_ach_memory_auth_bearer_prepends_bearer_on_authorization_header() -> None:
    auth = AchMemoryAuthBearer(type="bearer", env="ACH_SECRET_MEMORY", header="Authorization")
    ok, headers = resolve_ach_memory_auth(auth, None, resolve_secret=lambda name: "user-secret")
    assert ok is True
    assert headers == {"Authorization": "Bearer user-secret"}


def test_resolve_ach_memory_auth_bearer_degrades_when_secret_env_unset() -> None:
    auth = AchMemoryAuthBearer(type="bearer", env="ACH_SECRET_MEMORY")
    ok, headers = resolve_ach_memory_auth(auth, None, resolve_secret=lambda name: None)
    assert ok is False
    assert headers == {}


# --- AchMemoryFacade: exact typed schema ---------------------------------------------


@pytest.mark.parametrize("base_url", [FACADE_BASE, "http://ach-control-agent.ach.svc:8081"])
async def test_tools_list_returns_exactly_five_typed_tools_without_any_upstream_call(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    upstream = RecordingUpstream()
    async with _routed_to(monkeypatch, upstream.app()):
        facade = _facade()
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token, base_url=base_url) as session:
                result = await session.list_tools()

    names = {t.name for t in result.tools}
    assert names == {
        "memory_recall",
        "memory_reflect",
        "memory_retain",
        "memory_get_mental_model",
        "memory_list_mental_models",
    }
    assert upstream.calls == []

    # Review I4: scope/project_slug are absent from EVERY schema, not just retain's.
    for tool in result.tools:
        schema_properties = tool.inputSchema.get("properties", {})
        assert "scope" not in schema_properties
        assert "project_slug" not in schema_properties

    retain = next(t for t in result.tools if t.name == "memory_retain")
    required = set(retain.inputSchema.get("required", []))
    assert required == {"content", "memory_type", "basis", "trigger", "evidence"}
    properties = retain.inputSchema["properties"]

    # Exact enums/bounds (review I4): the typed schema matches the legacy vocabulary, not
    # just a presence check.
    assert properties["memory_type"]["enum"] == [
        "preference",
        "constraint",
        "decision",
        "convention",
        "fact",
        "gotcha",
    ]
    assert properties["basis"]["enum"] == ["human_explicit", "agent_verified"]
    assert properties["trigger"]["enum"] == ["user_requested", "agent_proactive"]
    assert properties["content"]["maxLength"] == 4096
    evidence_schema = properties["evidence"]
    assert evidence_schema["minItems"] == 1
    assert evidence_schema["maxItems"] == 4

    evidence_item_schema = retain.inputSchema.get("$defs", {}).get("RetainEvidence", {})
    assert evidence_item_schema["properties"]["kind"]["enum"] == [
        "user_quote",
        "tool_result",
        "artifact_excerpt",
    ]
    assert evidence_item_schema["properties"]["raw"]["maxLength"] == 1024
    # `source_ref: str | None` renders as anyOf[{maxLength}, {type: null}], not a bare
    # maxLength key.
    source_ref_variants = evidence_item_schema["properties"]["source_ref"]["anyOf"]
    assert any(variant.get("maxLength") == 512 for variant in source_ref_variants)


# --- AchMemoryFacade: containment / injection (real upstream round-trip) ------------


@pytest.mark.parametrize("base_url", [FACADE_BASE, "http://ach-control-agent.ach.svc:8081"])
async def test_retain_injects_fixed_scope_and_project_overriding_client_values(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    upstream = RecordingUpstream()
    async with _routed_to(monkeypatch, upstream.app()):
        facade = _facade(project="acme-agent")
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token, base_url=base_url) as session:
                await session.call_tool(
                    "memory_retain",
                    {
                        "content": "x",
                        "memory_type": "fact",
                        "basis": "human_explicit",
                        "trigger": "user_requested",
                        "evidence": [{"kind": "user_quote", "raw": "said it"}],
                        "scope": "global",
                        "project_slug": "someone-elses-bank",
                    },
                )

    assert len(upstream.calls) == 1
    tool, args = upstream.calls[0]
    assert tool == "retain"
    assert args["scope"] == "project"
    assert args["project_slug"] == "acme-agent"
    assert args["content"] == "x"
    assert args["evidence"] == [{"kind": "user_quote", "raw": "said it"}]


async def test_recall_translates_tags_to_tags_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    async with _routed_to(monkeypatch, upstream.app()):
        facade = _facade()
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token) as session:
                await session.call_tool("memory_recall", {"query": "q", "tags": ["repo:x"]})

    tool, args = upstream.calls[0]
    assert tool == "recall"
    assert args["tags_filter"] == ["repo:x"]
    assert "tags" not in args


async def test_call_rejects_unregistered_tool_with_zero_upstream_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream()
    async with _routed_to(monkeypatch, upstream.app()):
        facade = _facade()
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token) as session:
                result = await session.call_tool("forget", {})

    assert result.isError is True
    assert upstream.calls == []


async def test_upstream_is_error_degrades_to_a_tool_result_not_a_raw_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream()
    upstream.raises.add("recall")
    async with _routed_to(monkeypatch, upstream.app()):
        facade = _facade()
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token) as session:
                result = await session.call_tool("memory_recall", {"query": "q"})

    assert result.isError is False
    text = getattr(result.content[0], "text", "")
    assert text == "Memory temporarily unavailable."


async def test_transport_failure_degrades_to_the_exact_unavailable_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I1: a genuine transport-level failure (no MCP server mounted at all -- unlike
    the tool-level `isError` case above, which requires a real initialized session first)
    degrades to the EXACT note, not a substring, with zero upstream tool calls."""
    from starlette.applications import Starlette

    async with _routed_to(monkeypatch, Starlette(routes=[])):
        facade = _facade()
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token) as session:
                result = await session.call_tool("memory_recall", {"query": "q"})

    assert result.isError is False
    text = getattr(result.content[0], "text", "")
    assert text == "Memory temporarily unavailable."


async def test_missing_endpoint_degrades_with_zero_upstream_egress_and_never_a_raw_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream()
    async with _routed_to(monkeypatch, upstream.app()):
        facade = _facade(endpoint="")
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token) as session:
                result = await session.call_tool("memory_recall", {"query": "q"})

    assert upstream.calls == []
    text = getattr(result.content[0], "text", "")
    assert "unavailable" in text.lower()


# --- AchMemoryFacade: HMAC guard on every request -----------------------------------


async def test_denies_malformed_token_with_zero_upstream_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    outgoing: list[httpx.Request] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_requests=outgoing):
        facade = _facade()
        app = facade.app()
        delegated = _count_mounted_sdk_delegation(app)
        async with app.router.lifespan_context(app):
            # Review I2: the raw HTTP status is exactly 403 (not just "some exception" at
            # the SDK layer, which cannot distinguish 403 from an unrelated 500).
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=FACADE_BASE) as client:
                resp = await client.post("/t/garbage/memory/", content=b"{}")
            assert resp.status_code == 403
            with pytest.raises(Exception):  # noqa: B017 - SDK-raised transport/session error
                async with _client_session(app, "garbage"):
                    pass
    assert upstream.calls == []
    assert delegated == []
    assert outgoing == []


async def test_denies_wrong_key_token_with_zero_upstream_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    outgoing: list[httpx.Request] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_requests=outgoing):
        facade = _facade()
        app = facade.app()
        delegated = _count_mounted_sdk_delegation(app)
        wrong_key_token = facade_token(b"a-different-key", "owned-ws")
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=FACADE_BASE) as client:
                resp = await client.post(f"/t/{wrong_key_token}/memory/", content=b"{}")
            assert resp.status_code == 403
            with pytest.raises(Exception):  # noqa: B017
                async with _client_session(app, wrong_key_token):
                    pass
    assert upstream.calls == []
    assert delegated == []
    assert outgoing == []


async def test_denies_non_owned_claim_with_zero_upstream_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    outgoing: list[httpx.Request] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_requests=outgoing):
        facade = _facade()
        app = facade.app()
        delegated = _count_mounted_sdk_delegation(app)
        non_owned_token = facade_token(KEY, "someone-elses-ws")
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url=FACADE_BASE) as client:
                resp = await client.post(f"/t/{non_owned_token}/memory/", content=b"{}")
            assert resp.status_code == 403
            with pytest.raises(Exception):  # noqa: B017
                async with _client_session(app, non_owned_token):
                    pass
    assert upstream.calls == []
    assert delegated == []
    assert outgoing == []


async def test_ownership_flip_denies_subsequent_post_get_delete_with_zero_upstream_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I2: perform one successful typed call while owned, flip ownership, then
    assert the SAME token's next POST, GET and DELETE each deny with exactly 403 and zero
    further SDK delegation/upstream traffic -- not just the first initialization."""
    upstream = RecordingUpstream()
    upstream.responses["recall"] = "ok"
    owned = _togglable("owned-ws")
    outgoing: list[httpx.Request] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_requests=outgoing):
        facade = _facade(owned_workspace=owned)
        app = facade.app()
        delegated = _count_mounted_sdk_delegation(app)
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            session_ids: list[str] = []
            async with _client_session(app, token, session_ids=session_ids) as session:
                result = await session.call_tool("memory_recall", {"query": "q"})
                assert result.isError is False
                assert upstream.calls  # the typed call actually reached upstream while owned
                assert outgoing
                calls_before = len(upstream.calls)
                requests_before = len(outgoing)
                delegated_before = len(delegated)

                owned.state["owned"] = False  # explicit revoke, same initialized session remains open
                session_headers = {"mcp-session-id": session_ids[0]}
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=FACADE_BASE
                ) as client:
                    post_resp = await client.post(
                        f"/t/{token}/memory/",
                        headers=session_headers,
                        content=json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "tools/call",
                                "id": 2,
                                "params": {
                                    "name": "memory_recall",
                                    "arguments": {"query": "valid follow-up"},
                                },
                            }
                        ),
                    )
                    get_resp = await client.get(f"/t/{token}/memory/", headers=session_headers)
                    delete_resp = await client.delete(
                        f"/t/{token}/memory/", headers=session_headers
                    )
                assert len(upstream.calls) == calls_before
                assert len(outgoing) == requests_before
                assert len(delegated) == delegated_before
    assert post_resp.status_code == 403
    assert get_resp.status_code == 403
    assert delete_resp.status_code == 403
    assert len(upstream.calls) == calls_before
    assert len(outgoing) == requests_before
    assert len(delegated) == delegated_before


async def test_a_valid_hmac_becomes_non_owned_between_two_calls() -> None:
    """Review focus 1: ownership is re-checked on every memory-facade request, never
    cached -- same token, the second independent session is denied."""
    owned = _flipping("owned-ws")
    facade = _facade(owned_workspace=owned)
    app = facade.app()
    token = facade_token(KEY, "owned-ws")

    async with app.router.lifespan_context(app):
        async with _client_session(app, token):
            pass  # first call: ownership check #1 (True) succeeds

        with pytest.raises(Exception):  # noqa: B017
            async with _client_session(app, token):
                pass  # second call, same token: ownership check #2 (now False) denies

    # The streamable-HTTP transport issues more than one HTTP request per logical "call"
    # (initialize, and a session-termination DELETE on teardown) -- each one re-verified,
    # never cached: the exact count is SDK protocol detail, the invariant under test is
    # that the FIRST check (while still owned) succeeded and EVERY check after the flip
    # (including the second session's) was denied.
    assert owned.state["count"] >= 2
    assert owned.state["owned"] is False


async def test_untokenized_route_is_404() -> None:
    facade = _facade()
    app = facade.app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=FACADE_BASE) as client:
            resp = await client.post("/", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert resp.status_code == 404


# --- AchMemoryFacade: typed validation and wire-null evidence (review I4) ------------


async def test_retain_invalid_typed_values_reject_locally_with_zero_upstream_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An invalid enum, an over-length content string, and an evidence list outside 1-4
    items each reject LOCALLY (SDK isError from the facade's own typed schema) with zero
    upstream calls -- the schema is enforced, not just advertised."""
    upstream = RecordingUpstream()
    outgoing: list[httpx.Request] = []
    base_args: dict[str, Any] = {
        "content": "x",
        "memory_type": "fact",
        "basis": "human_explicit",
        "trigger": "user_requested",
        "evidence": [{"kind": "user_quote", "raw": "said it"}],
    }
    invalid_variants = [
        {**base_args, "memory_type": "not-a-real-type"},
        {**base_args, "content": "x" * 4097},
        {**base_args, "evidence": []},
        {**base_args, "evidence": [{"kind": "user_quote", "raw": "r"}] * 5},
    ]
    async with _routed_to(monkeypatch, upstream.app(), capture_requests=outgoing):
        facade = _facade()
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token) as session:
                for args in invalid_variants:
                    result = await session.call_tool("memory_retain", args)
                    assert result.isError is True
    assert upstream.calls == []
    assert outgoing == []


async def test_retain_wire_arguments_omit_top_level_and_evidence_nulls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review I4: capture the actual outgoing `tools/call` JSON-RPC body -- before FastMCP
    on the upstream side binds it against the tool's own Python defaults, which would make
    an omitted field and an explicit wire null indistinguishable. An omitted top-level
    `tags` and an omitted evidence `source_ref` must both be ABSENT keys, never explicit
    nulls; retain's own `tags` (when given) keeps its name, unlike recall's rename."""
    upstream = RecordingUpstream()
    bodies: list[bytes] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_bodies=bodies):
        facade = _facade()
        app = facade.app()
        token = facade_token(KEY, "owned-ws")
        async with app.router.lifespan_context(app):
            async with _client_session(app, token) as session:
                # No `tags`, and no evidence `source_ref`: both omitted, neither given.
                await session.call_tool(
                    "memory_retain",
                    {
                        "content": "x",
                        "memory_type": "fact",
                        "basis": "human_explicit",
                        "trigger": "user_requested",
                        "evidence": [{"kind": "user_quote", "raw": "said it"}],
                    },
                )
                # `tags` given this time: retain must keep the key name, never rename it.
                await session.call_tool(
                    "memory_retain",
                    {
                        "content": "x",
                        "memory_type": "fact",
                        "basis": "human_explicit",
                        "trigger": "user_requested",
                        "evidence": [{"kind": "user_quote", "raw": "said it"}],
                        "tags": ["repo:x"],
                    },
                )

    messages = [json.loads(body) for body in bodies if body]
    tool_calls = [
        m
        for m in messages
        if m.get("method") == "tools/call" and m.get("params", {}).get("name") == "retain"
    ]
    assert len(tool_calls) == 2

    without_tags = tool_calls[0]["params"]["arguments"]
    assert "tags" not in without_tags  # omitted -- never sent as an explicit wire null
    assert "source_ref" not in without_tags["evidence"][0]
    assert without_tags["scope"] == "project"  # _invoke's injection, verified wire-side too

    with_tags = tool_calls[1]["params"]["arguments"]
    assert with_tags["tags"] == ["repo:x"]  # retain keeps "tags", never renamed


async def test_exclude_tools_removes_memory_tools_by_bare_name() -> None:
    facade = AchMemoryFacade(
        endpoint=UPSTREAM,
        headers={},
        project="acme-agent",
        key=KEY,
        owned_workspace=_owned,
        identity=IDENTITY,
        exclude_tools=frozenset({"memory_retain", "m1.unrelated_tool"}),
    )

    names = {t.name for t in await facade._mcp.list_tools()}
    assert names == {
        "memory_recall",
        "memory_reflect",
        "memory_get_mental_model",
        "memory_list_mental_models",
    }

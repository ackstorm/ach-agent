# SPDX-License-Identifier: MIT
"""_memory_client.py -- project/endpoint/auth resolution and the real SDK round-trip to a
fixture ach-memory upstream (contract §5/§10; old-ach-agent memory/ach_memory.py)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from ach_runtime.config.schema import AchMemoryAuthAch, AchMemoryAuthBearer, AchMemoryParams
from ach_runtime.facades import _mcp_session
from ach_runtime.facades._memory_client import (
    ACH_MEMORY_LOAD_CONTEXT,
    _describe,
    advertised_name,
    call_ach_memory,
    excluded_mcp_server_ids,
    fetch_context,
    normalize_slug,
    prepare_ach_memory,
    resolve_ach_memory_auth,
    resolve_endpoint,
    resolve_project,
)
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.model import public_catalog
from ach_runtime.harness.hydrate import HydratedCatalog, HydratedModel
from ach_runtime.harness.tokens import facade_token
from fixtures.memory_mcp import RecordingUpstream

IDENTITY = ProcessIdentity(agent="classifier", environment="platform")
KEY = b"fixture-key"

# FastMCP auto-enables DNS-rebinding protection for a loopback-bound server (the default),
# which rejects any Host header outside 127.0.0.1/localhost/[::1] -- so every in-process
# fixture/endpoint URL below must use one of those WITH an explicit port (the allowlist is
# "localhost:*" -- a bare, default-port Host header does not match it).
UPSTREAM = "http://localhost:8080/"


async def _owned(candidate: str) -> bool:
    return candidate == "owned-ws"


def _minimal_catalog() -> HydratedCatalog:
    """A HydratedCatalog with nothing but the fields public_catalog actually reads -- not a
    new fixture type, just the existing constructor's minimum (review I3)."""
    return HydratedCatalog(
        environment="platform",
        model=HydratedModel(id="m", endpoint="https://ach.example.com/v1", metadata=None),
        mcp_servers=(),
        skills=(),
        prompts=(),
        artifacts=(),
    )


@contextlib.asynccontextmanager
async def _routed_to(
    monkeypatch: pytest.MonkeyPatch,
    app: Any,
    *,
    record_headers: dict[str, str] | None = None,
    capture_clients: list[httpx.AsyncClient] | None = None,
    capture_requests: list[httpx.Request] | None = None,
) -> AsyncIterator[None]:
    """Point `_mcp_session.mcp_session`'s internal httpx client at an in-process fixture
    app via ASGITransport -- replacing ONLY the network transport; ClientSession,
    `list_tools`/`call_tool` and the SDK's own JSON-RPC handling are untouched. Also runs
    the fixture app's own session-manager lifespan, required for its streamable-HTTP
    transport to serve any request at all."""

    def _patched(headers: dict[str, str] | None = None, **_: Any) -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=UPSTREAM, headers=headers
        )
        if record_headers is not None or capture_requests is not None:

            async def _hook(request: httpx.Request) -> None:
                if record_headers is not None:
                    record_headers.update(dict(request.headers))
                if capture_requests is not None:
                    capture_requests.append(request)

            client.event_hooks["request"] = [_hook]
        if capture_clients is not None:
            capture_clients.append(client)
        return client

    monkeypatch.setattr(_mcp_session, "create_mcp_http_client", _patched)
    async with app.router.lifespan_context(app):
        yield


def _counting_proxy_client() -> tuple[httpx.AsyncClient, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    async def count_request(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(502)

    return httpx.AsyncClient(transport=httpx.MockTransport(count_request)), requests


# --- normalize_slug / resolve_project -----------------------------------------------


def test_normalize_slug_lowercases_collapses_and_caps() -> None:
    assert normalize_slug("  Acme/App Repo!! ") == "acme-app-repo"
    assert normalize_slug("***") == ""
    assert len(normalize_slug("x" * 500)) == 128


def test_resolve_project_prefers_explicit_override() -> None:
    params = AchMemoryParams(project="Custom Bank", mcpServerId="mem")
    assert resolve_project(params, "classifier", namespace="acme") == "custom-bank"


def test_resolve_project_falls_back_to_namespace_agent() -> None:
    params = AchMemoryParams(mcpServerId="mem")
    assert resolve_project(params, "classifier", namespace="acme") == "acme-classifier"


def test_resolve_project_falls_back_to_bare_agent_without_namespace() -> None:
    params = AchMemoryParams(mcpServerId="mem")
    assert resolve_project(params, "classifier", namespace="") == "classifier"


# --- resolve_endpoint / excluded_mcp_server_ids (trailing-slash aliasing) -----------


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
            endpoint="",
            mcp_server_id="mem",
            mcp_servers={"mem": "https://hydrated.example.com/mcp/mem"},
        )
        == "https://hydrated.example.com/mcp/mem"
    )


def test_resolve_endpoint_empty_when_neither_resolves() -> None:
    assert resolve_endpoint(endpoint="", mcp_server_id="missing", mcp_servers={}) == ""


def test_excluded_mcp_server_ids_by_id_includes_trailing_slash_alias() -> None:
    servers = {
        "mem": "https://ach.example.com/mcp/mem",
        "mem-alias": "https://ach.example.com/mcp/mem/",
        "other": "https://ach.example.com/mcp/other",
    }
    assert excluded_mcp_server_ids(mcp_server_id="mem", endpoint="", mcp_servers=servers) == {
        "mem",
        "mem-alias",
    }


def test_excluded_mcp_server_ids_by_id_absent_from_hydration() -> None:
    assert excluded_mcp_server_ids(mcp_server_id="mem", endpoint="", mcp_servers={}) == {"mem"}


def test_excluded_mcp_server_ids_by_endpoint_excludes_every_trailing_slash_alias() -> None:
    servers = {
        "raw-mem": "https://ach.example.com/mcp/raw-mem",
        "raw-mem-alias": "https://ach.example.com/mcp/raw-mem/",
        "other": "https://ach.example.com/mcp/other",
    }
    result = excluded_mcp_server_ids(
        mcp_server_id="", endpoint="https://ach.example.com/mcp/raw-mem", mcp_servers=servers
    )
    assert result == {"raw-mem", "raw-mem-alias"}


def test_excluded_mcp_server_ids_empty_when_neither_configured() -> None:
    assert excluded_mcp_server_ids(mcp_server_id="", endpoint="", mcp_servers={}) == set()


# --- resolve_ach_memory_auth --------------------------------------------------------


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


def test_resolve_ach_memory_auth_bearer_custom_header_sends_raw_secret() -> None:
    auth = AchMemoryAuthBearer(type="bearer", env="ACH_SECRET_MEMORY", header="x-litellm-api-key")
    ok, headers = resolve_ach_memory_auth(auth, None, resolve_secret=lambda name: "user-secret")
    assert ok is True
    assert headers == {"x-litellm-api-key": "user-secret"}


def test_resolve_ach_memory_auth_bearer_degrades_when_secret_env_unset() -> None:
    auth = AchMemoryAuthBearer(type="bearer", env="ACH_SECRET_MEMORY")
    ok, headers = resolve_ach_memory_auth(auth, None, resolve_secret=lambda name: None)
    assert ok is False
    assert headers == {}


# --- advertised_name -----------------------------------------------------------------


def test_advertised_name_exact_match_wins() -> None:
    assert advertised_name({"recall", "ach-memory.recall"}, "recall") == "recall"


def test_advertised_name_unique_dot_prefix_matches() -> None:
    assert advertised_name({"ach-memory.recall"}, "recall") == "ach-memory.recall"


def test_advertised_name_unique_hyphen_and_slash_prefix_matches() -> None:
    assert advertised_name({"ach-memory-recall"}, "recall") == "ach-memory-recall"
    assert advertised_name({"ach-memory/recall"}, "recall") == "ach-memory/recall"


def test_advertised_name_ambiguous_prefix_falls_back_to_bare() -> None:
    assert advertised_name({"a.recall", "b.recall"}, "recall") == "recall"


def test_advertised_name_unadvertised_falls_back_to_bare() -> None:
    assert advertised_name({"reflect"}, "recall") == "recall"


def test_advertised_name_sync_retain_never_answers_retain() -> None:
    """`_` is deliberately not a separator: `sync_retain` must never be mistaken for a
    prefixed `retain` (legacy invariant)."""
    assert advertised_name({"sync_retain"}, "retain") == "retain"


# --- call_ach_memory / fetch_context / prepare_ach_memory (real SDK round-trip) -----


async def test_call_ach_memory_direct_upstream_typed_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    upstream.responses["recall"] = "the answer"
    async with _routed_to(monkeypatch, upstream.app()):
        text = await call_ach_memory(
            UPSTREAM,
            {},
            "recall",
            {"query": "q", "scope": "project", "project_slug": "acme-agent"},
            identity=IDENTITY,
        )
    assert text == "the answer"
    assert upstream.calls == [
        ("recall", {"query": "q", "scope": "project", "project_slug": "acme-agent"})
    ]


async def test_call_ach_memory_gateway_prefixed_upstream_resolves_advertised_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream(prefix="ach-memory")
    upstream.responses["retain"] = "stored"
    async with _routed_to(monkeypatch, upstream.app()):
        text = await call_ach_memory(
            UPSTREAM,
            {},
            "retain",
            {"content": "x", "scope": "project", "project_slug": "p"},
            identity=IDENTITY,
        )
    assert text == "stored"
    assert upstream.calls == [("retain", {"content": "x", "scope": "project", "project_slug": "p"})]


async def test_call_ach_memory_is_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    upstream.raises.add("recall")
    async with _routed_to(monkeypatch, upstream.app()):
        # The SDK's own ClientSession/TaskGroup teardown may surface our `raise
        # RuntimeError` either directly or wrapped in an (Base)ExceptionGroup depending on
        # exactly where in the async-with chain it is re-raised -- `_describe` (the same
        # helper `_invoke`'s degrade path uses) flattens either shape to the real message.
        with pytest.raises(Exception) as exc_info:  # noqa: B017 - see above
            await call_ach_memory(UPSTREAM, {}, "recall", {"query": "q"}, identity=IDENTITY)
        assert "recall" in _describe(exc_info.value)


async def test_call_ach_memory_stamps_identity_and_credential_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream()
    seen: dict[str, str] = {}
    async with _routed_to(monkeypatch, upstream.app(), record_headers=seen):
        await call_ach_memory(
            UPSTREAM, {"x-ach-key": "ek-xyz"}, "recall", {"query": "q"}, identity=IDENTITY
        )
    assert seen["x-ach-key"] == "ek-xyz"
    assert seen["x-ach-agent"] == "classifier"
    assert seen["x-ach-environment"] == "platform"


async def test_fetch_context_wraps_text_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    upstream.responses[ACH_MEMORY_LOAD_CONTEXT] = '{"text": "## project-context\\n\\nStuff."}'
    async with _routed_to(monkeypatch, upstream.app()):
        section = await fetch_context(UPSTREAM, {}, "acme-agent", identity=IDENTITY)
    assert section == "## Memory\n\n## project-context\n\nStuff."


async def test_fetch_context_empty_text_reports_no_standing_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream()
    upstream.responses[ACH_MEMORY_LOAD_CONTEXT] = '{"text": ""}'
    async with _routed_to(monkeypatch, upstream.app()):
        section = await fetch_context(UPSTREAM, {}, "acme-agent", identity=IDENTITY)
    assert section == "## Memory\n\nNo standing context yet."


async def test_fetch_context_degrades_on_upstream_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    upstream.raises.add(ACH_MEMORY_LOAD_CONTEXT)
    async with _routed_to(monkeypatch, upstream.app()):
        section = await fetch_context(UPSTREAM, {}, "acme-agent", identity=IDENTITY)
    assert section == "## Memory\n\nUnavailable (context load failed)."


async def test_prepare_ach_memory_no_endpoint_reports_unavailable_without_sdk_traffic() -> None:
    available, section = await prepare_ach_memory("", "acme-agent", {}, identity=IDENTITY)
    assert available is False
    assert section == "## Memory\n\nUnavailable (no endpoint)."


async def test_prepare_ach_memory_available_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = RecordingUpstream()
    upstream.responses[ACH_MEMORY_LOAD_CONTEXT] = '{"text": "hi"}'
    async with _routed_to(monkeypatch, upstream.app()):
        available, section = await prepare_ach_memory(UPSTREAM, "acme-agent", {}, identity=IDENTITY)
    assert available is True
    assert section == "## Memory\n\nhi"


# --- owned-resource cleanup / cancellation (review I1) -------------------------------


async def test_call_ach_memory_closes_owned_http_client_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream()
    upstream.responses["recall"] = "hi"
    clients: list[httpx.AsyncClient] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_clients=clients):
        await call_ach_memory(UPSTREAM, {}, "recall", {"query": "q"}, identity=IDENTITY)
    assert clients and all(c.is_closed for c in clients)


async def test_call_ach_memory_closes_owned_http_client_on_tool_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = RecordingUpstream()
    upstream.raises.add("recall")
    clients: list[httpx.AsyncClient] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_clients=clients):
        with pytest.raises(Exception):  # noqa: B017 - see test_call_ach_memory_is_error_raises
            await call_ach_memory(UPSTREAM, {}, "recall", {"query": "q"}, identity=IDENTITY)
    assert clients and all(c.is_closed for c in clients)


async def test_call_ach_memory_transport_failure_closes_owned_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine transport-level failure (no MCP server mounted at all, not a tool-level
    `isError`) still closes the owned httpx client -- distinct from the tool-failure case
    above, which fails only after a real session is established."""
    from starlette.applications import Starlette

    clients: list[httpx.AsyncClient] = []
    async with _routed_to(monkeypatch, Starlette(routes=[]), capture_clients=clients):
        with pytest.raises(Exception):  # noqa: B017 - SDK-raised transport/initialize error
            await call_ach_memory(UPSTREAM, {}, "recall", {"query": "q"}, identity=IDENTITY)
    assert clients and all(c.is_closed for c in clients)


async def test_call_ach_memory_cancellation_propagates_and_closes_owned_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review focus (I1): cancelling an in-flight call against a bounded blocked upstream
    tool propagates CancelledError and the owned httpx client settles (closes) within 2
    seconds -- never a leaked transport."""
    upstream = RecordingUpstream()
    upstream.blocking.add("recall")
    clients: list[httpx.AsyncClient] = []
    async with _routed_to(monkeypatch, upstream.app(), capture_clients=clients):
        task = asyncio.create_task(
            call_ach_memory(UPSTREAM, {}, "recall", {"query": "q"}, identity=IDENTITY)
        )
        for _ in range(100):
            if upstream.calls:
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("call never reached the blocked upstream tool")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    assert clients and all(c.is_closed for c in clients)


# --- degraded raw-route/catalog containment, composed with the existing facades (I3) --


async def test_excluded_server_id_aliases_stay_404_and_absent_from_catalog_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """excluded_mcp_server_ids is computed BEFORE auth/probe, then composed with the
    existing McpFacade and public_catalog: both trailing-slash-equivalent hydrated ids stay
    404/zero-egress on the generic MCP facade and absent from the published catalog once
    memory itself is available (review focus 5)."""
    servers = {
        "mem": "http://localhost:8080/",
        "mem-alias": "http://localhost:8080",
        "other": "http://localhost:9000/",
    }
    exclude = excluded_mcp_server_ids(mcp_server_id="mem", endpoint="", mcp_servers=servers)
    assert exclude == {"mem", "mem-alias"}

    ok, auth_headers = resolve_ach_memory_auth(AchMemoryAuthAch(type="ach"), "ek-xyz")
    assert ok is True
    memory_upstream = RecordingUpstream()
    memory_upstream.responses[ACH_MEMORY_LOAD_CONTEXT] = '{"text": "hi"}'
    sdk_requests: list[httpx.Request] = []
    async with _routed_to(monkeypatch, memory_upstream.app(), capture_requests=sdk_requests):
        available, _ = await prepare_ach_memory(UPSTREAM, "acme-agent", auth_headers, identity=IDENTITY)
    assert available is True
    assert sdk_requests

    proxy_client, proxy_requests = _counting_proxy_client()
    mcp_facade = McpFacade(
        servers=servers,
        server_headers={sid: {"x-ach-key": "ek-xyz"} for sid in servers},
        key=KEY,
        owned_workspace=_owned,
        exclude=frozenset(exclude),
        exclude_tools=frozenset(),
        identity=IDENTITY,
        client=proxy_client,
    )
    mcp_app = mcp_facade.app()
    token = facade_token(KEY, "owned-ws")
    async with mcp_app.router.lifespan_context(mcp_app):
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://facade") as client:
            mem_resp = await client.post(f"/t/{token}/mcp/mem", content=b"{}")
            alias_resp = await client.post(f"/t/{token}/mcp/mem-alias", content=b"{}")
    assert mem_resp.status_code == 404
    assert alias_resp.status_code == 404

    granted = {sid: ep for sid, ep in servers.items() if sid not in exclude}
    catalog = public_catalog(
        _minimal_catalog(),
        provider_id="acme",
        model_url="http://facade/t/model-token",
        mcp_urls={sid: f"http://facade/t/{token}/mcp/{sid}" for sid in granted},
        memory_url=f"http://facade/t/{token}/memory/" if available else None,
    )
    assert set(catalog["mcp"]) == {"other", "memory"}
    assert proxy_requests == []
    await proxy_client.aclose()


async def test_excluded_endpoint_aliases_stay_404_and_absent_from_catalog_when_degraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same containment, but selected by explicit endpoint (not server id) and with memory
    DEGRADED (missing bearer secret, never resolved) -- the exclusion and the published
    catalog's absence of a "memory" entry hold independently of that failure (review focus
    5; Important #3)."""
    servers = {
        "raw-mem": "http://localhost:8080/",
        "raw-mem-alias": "http://localhost:8080",
        "other": "http://localhost:9000/",
    }
    exclude = excluded_mcp_server_ids(
        mcp_server_id="", endpoint="http://localhost:8080/", mcp_servers=servers
    )
    assert exclude == {"raw-mem", "raw-mem-alias"}

    ok, _ = resolve_ach_memory_auth(
        AchMemoryAuthBearer(type="bearer", env="ACH_SECRET_MEMORY"), None, resolve_secret=lambda n: None
    )
    assert ok is False  # missing bearer secret -- memory degrades before any probe
    sdk_requests: list[httpx.Request] = []
    if ok:
        upstream = RecordingUpstream()
        async with _routed_to(monkeypatch, upstream.app(), capture_requests=sdk_requests):
            await prepare_ach_memory(UPSTREAM, "acme-agent", {}, identity=IDENTITY)
    assert sdk_requests == []

    proxy_client, proxy_requests = _counting_proxy_client()
    mcp_facade = McpFacade(
        servers=servers,
        server_headers={sid: {"x-ach-key": "ek-xyz"} for sid in servers},
        key=KEY,
        owned_workspace=_owned,
        exclude=frozenset(exclude),
        exclude_tools=frozenset(),
        identity=IDENTITY,
        client=proxy_client,
    )
    mcp_app = mcp_facade.app()
    token = facade_token(KEY, "owned-ws")
    async with mcp_app.router.lifespan_context(mcp_app):
        transport = httpx.ASGITransport(app=mcp_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://facade") as client:
            raw_resp = await client.post(f"/t/{token}/mcp/raw-mem", content=b"{}")
            alias_resp = await client.post(f"/t/{token}/mcp/raw-mem-alias", content=b"{}")
    assert raw_resp.status_code == 404
    assert alias_resp.status_code == 404

    granted = {sid: ep for sid, ep in servers.items() if sid not in exclude}
    catalog = public_catalog(
        _minimal_catalog(),
        provider_id="acme",
        model_url="http://facade/t/model-token",
        mcp_urls={sid: f"http://facade/t/{token}/mcp/{sid}" for sid in granted},
        memory_url=None,  # degraded: auth never resolved, nothing to publish
    )
    assert set(catalog["mcp"]) == {"other"}
    assert proxy_requests == []
    await proxy_client.aclose()


async def test_failed_context_composition_keeps_aliases_excluded_without_generic_proxy_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real failed load_context still composes exclusions through McpFacade/public_catalog;
    neither raw alias reaches the generic proxy transport."""
    servers = {
        "raw-memory": "http://localhost:8080/",
        "raw-memory-alias": "http://localhost:8080",
        "other": "http://localhost:9000/",
    }
    exclude = excluded_mcp_server_ids(
        mcp_server_id="", endpoint="http://localhost:8080/", mcp_servers=servers
    )
    assert exclude == {"raw-memory", "raw-memory-alias"}

    upstream = RecordingUpstream()
    upstream.raises.add(ACH_MEMORY_LOAD_CONTEXT)
    sdk_requests: list[httpx.Request] = []
    ok, auth_headers = resolve_ach_memory_auth(AchMemoryAuthAch(type="ach"), "ek-xyz")
    assert ok is True
    async with _routed_to(monkeypatch, upstream.app(), capture_requests=sdk_requests):
        available, section = await prepare_ach_memory(
            UPSTREAM, "acme-agent", auth_headers, identity=IDENTITY
        )
    assert available is False
    assert section == "## Memory\n\nUnavailable (context load failed)."
    assert sdk_requests  # load_context was actually attempted and failed through the SDK

    proxy_requests: list[httpx.Request] = []

    async def count_proxy_request(request: httpx.Request) -> httpx.Response:
        proxy_requests.append(request)
        return httpx.Response(502)

    async with httpx.AsyncClient(transport=httpx.MockTransport(count_proxy_request)) as proxy_client:
        mcp_facade = McpFacade(
            servers=servers,
            server_headers={sid: {"x-ach-key": "ek-xyz"} for sid in servers},
            key=KEY,
            owned_workspace=_owned,
            exclude=frozenset(exclude),
            exclude_tools=frozenset(),
            identity=IDENTITY,
            client=proxy_client,
        )
        mcp_app = mcp_facade.app()
        token = facade_token(KEY, "owned-ws")
        async with mcp_app.router.lifespan_context(mcp_app):
            transport = httpx.ASGITransport(app=mcp_app)
            async with httpx.AsyncClient(transport=transport, base_url="http://facade") as client:
                raw_resp = await client.post(f"/t/{token}/mcp/raw-memory", content=b"{}")
                alias_resp = await client.post(f"/t/{token}/mcp/raw-memory-alias", content=b"{}")
        assert raw_resp.status_code == 404
        assert alias_resp.status_code == 404

        granted = {sid: ep for sid, ep in servers.items() if sid not in exclude}
        catalog = public_catalog(
            _minimal_catalog(),
            provider_id="acme",
            model_url="http://facade/t/model-token",
            mcp_urls={sid: f"http://facade/t/{token}/mcp/{sid}" for sid in granted},
            memory_url=None,
        )
        assert set(catalog["mcp"]) == {"other"}
        assert proxy_requests == []

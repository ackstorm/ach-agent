# SPDX-License-Identifier: MIT
"""Conditional peer tools are mounted and invoked through the assembled Harness."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.types.a2a_pb2 import StreamResponse
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.harness.control import assemble, create_facade_app
from ach_runtime.harness.tokens import facade_token
from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

KEY_ENV = {"ACH_SANDBOX_KEY": "harness-private-key", "ACH_SECRET_IDENTITY": "ek-private-value"}
FACADE_BASE = "http://localhost:8081"


def _in_process_url(url: str) -> str:
    return f"{FACADE_BASE}{urlsplit(url).path}"


def _config(*, peers: bool = True, excluded: list[str] | None = None):
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["capability"]["filter"]["exclude"]["tools"] = excluded or []
    wire["configVersion"] = compute_config_version(wire)
    config = parse_config(wire)
    return config


def _catalog_handler(peers: bool = True, raw_a2a: bool = False):
    entries = ([{"id": "reviewer", "endpoint": "https://private-peer.example/a2a"},
                {"id": "planner", "endpoint": "https://private-planner.example/a2a"}] if peers else [])
    mcp_servers = ([{"id": "a2a", "endpoint": "https://raw-a2a.example/mcp"}]
                   if raw_a2a else [])

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json={
                "environment": "engineering-prod",
                "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                            "mcpServers": mcp_servers, "a2aAgents": entries},
                "context": {"skills": [], "prompts": [], "artifacts": []},
            })
        if request.url.path == "/v2/model/info":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"unexpected ACH request {request.url}")

    return handler


async def _client_session(app: Any, token: str):
    http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=FACADE_BASE)
    client_cm = streamable_http_client(
        f"{FACADE_BASE}/facades/a2a/t/{token}/a2a/", http_client=http_client
    )
    read, write, _ = await client_cm.__aenter__()
    session = ClientSession(read, write)
    await session.__aenter__()
    await session.initialize()
    return http_client, client_cm, session


async def test_nonempty_peers_publish_guarded_a2a_catalog(monkeypatch):
    calls: list[tuple[str, str]] = []

    class PeerClient:
        async def send_message(self, request: Any):
            prompt = request.message.parts[0].text
            calls.append(("peer", prompt))
            response = StreamResponse()
            response.message.parts.add().text = "peer says hello"
            yield response

        async def get_task(self, request: Any):
            raise AssertionError("not used")

    async def fake_create_client(*, agent: str, client_config: Any):
        calls.append(("create", agent))
        return PeerClient()

    monkeypatch.setattr("a2a.client.create_client", fake_create_client)
    control = await assemble(
        _config(), environ=KEY_ENV, ach_transport=httpx.MockTransport(_catalog_handler(raw_a2a=True)),
        facade_transport=httpx.MockTransport(lambda request: httpx.Response(500)),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, "a" * 64),
        "ach-workspace", "pod-1", "http://mini",
    )
    monkeypatch.setattr(control.manager, "active_endpoints", lambda: {endpoint.ref.workspace_ref: endpoint})
    async def owns_workspace(claim: str) -> bool:
        return claim == endpoint.name

    control.a2a_facade._owned_workspace = owns_workspace
    catalog = control._catalog_for(endpoint)
    # M2: the full expected route, derived independently from the configured
    # facadeEndpoint and the current `facade_token(key, endpoint.name)` -- not a
    # tautology extracted from the URL under test -- asserted BEFORE `_in_process_url`'s
    # origin rewrite below.
    url = catalog["mcp"]["a2a"]["url"]
    expected_token = facade_token(control.key, endpoint.name)
    expected_url = (
        f"{control.public.infrastructure.facade_endpoint.rstrip('/')}/a2a/t/{expected_token}/a2a/"
    )
    assert url == expected_url
    assert catalog["mcp"]["a2a"]["headers"] == {}
    assert "raw-a2a.example" not in url
    assert "harness-private-key" not in json.dumps(catalog)
    assert "ek-private-value" not in json.dumps(catalog)
    assert "private-peer.example" not in json.dumps(catalog)
    assert "raw-a2a.example" not in json.dumps(catalog)
    public_json = control.public.model_dump_json(by_alias=True)
    assert "harness-private-key" not in public_json
    assert "ek-private-value" not in public_json
    assert "private-peer.example" not in public_json

    app = create_facade_app(control)
    async with app.router.lifespan_context(app):
        http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=FACADE_BASE)
        async with http_client:
            async with streamable_http_client(_in_process_url(url), http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    assert {tool.name for tool in listed.tools} == {
                        "a2a_reviewer", "a2a_reviewer_async", "a2a_reviewer_status",
                        "a2a_planner", "a2a_planner_async", "a2a_planner_status",
                    }
                    result = await session.call_tool("a2a_reviewer", {"prompt": "review this"})
                    assert result.isError is False
                    assert "peer says hello" in result.content[0].text
    assert calls == [("create", "https://private-peer.example/a2a"), ("peer", "review this")]
    await control.close()


async def test_empty_peers_publish_no_a2a() -> None:
    control = await assemble(
        _config(), environ=KEY_ENV, ach_transport=httpx.MockTransport(_catalog_handler(False)),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, "b" * 64),
        "ach-workspace", "pod-1", "http://mini",
    )
    catalog = control._catalog_for(endpoint)
    assert "a2a" not in catalog["mcp"]
    assert control.a2a_facade is None
    app = create_facade_app(control)
    assert all(getattr(route, "path", "") != "/facades/a2a" for route in app.routes)
    await control.close()


async def test_a2a_excluded_tool_is_not_advertised_or_callable(monkeypatch) -> None:
    control = await assemble(
        _config(excluded=["a2a_reviewer"]), environ=KEY_ENV,
        ach_transport=httpx.MockTransport(_catalog_handler()),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, "c" * 64),
        "ach-workspace", "pod-1", "http://mini",
    )
    monkeypatch.setattr(control.manager, "active_endpoints", lambda: {endpoint.ref.workspace_ref: endpoint})
    async def owns_workspace(claim: str) -> bool:
        return claim == endpoint.name

    control.a2a_facade._owned_workspace = owns_workspace
    app = create_facade_app(control)
    catalog = control._catalog_for(endpoint)
    async with app.router.lifespan_context(app):
        http_client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=FACADE_BASE)
        async with http_client:
            async with streamable_http_client(_in_process_url(catalog["mcp"]["a2a"]["url"]), http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert "a2a_reviewer" not in names
                    result = await session.call_tool("a2a_reviewer", {"prompt": "x"})
                    assert result.isError is True
    await control.close()


async def test_a2a_lifespan_and_control_close_close_clients(monkeypatch):
    constructed: list[Any] = []

    class Client:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.closed = 0
            constructed.append(self)

        async def close(self) -> None:
            self.closed += 1

    from ach_runtime.facades.a2a import A2AEgressFacade
    monkeypatch.setattr(A2AEgressFacade, "_default_client", staticmethod(lambda entry, auth, identity: Client()))
    control = await assemble(
        _config(), environ=KEY_ENV, ach_transport=httpx.MockTransport(_catalog_handler()),
    )
    facade = control.a2a_facade
    assert facade is not None and len(constructed) == 2
    failed = await facade._tools[0].handler(prompt="tool error path")
    assert failed["ok"] is False
    app = create_facade_app(control)
    async with app.router.lifespan_context(app):
        pass
    await control.close()
    assert [client.closed for client in constructed] == [1, 1]

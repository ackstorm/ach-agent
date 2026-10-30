# SPDX-License-Identifier: MIT
"""Model, MCP, memory and A2A clients pick up every new ACH credential generation without a
restart, and retry once only when ACH itself rejected the credential."""

from __future__ import annotations

import asyncio
import itertools

import httpx

from ach_runtime.facades import _mcp_session
from ach_runtime.facades._memory_client import call_ach_memory
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.a2a import A2AAgentClient
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.keys import Exchange, KeySource
from ach_runtime.harness.tokens import facade_token
from fixtures.memory_mcp import RecordingUpstream

KEY = b"k" * 32
IDENTITY = ProcessIdentity(agent="a", environment="prod")
TOKEN = facade_token(KEY, "ws")
REJECTED = {"error": {"code": "expired_or_revoked", "message": "key expired or revoked"}, "request_id": "req_1"}


async def _owned(claim: str) -> bool:
    return claim == "ws"


async def _keys(tmp_path, exchanges: list[httpx.Request]) -> KeySource:
    """An exchanging KeySource whose n-th exchange returns jwt-<n>; already holds jwt-1."""
    token = tmp_path / "token"
    token.write_text("sa-token")

    def handler(request: httpx.Request) -> httpx.Response:
        exchanges.append(request)
        return httpx.Response(200, json={"token": f"jwt-{len(exchanges)}", "expires_in": 10**6, "key_id": "ekid_1"})

    ticks = itertools.count(step=11.0)  # every read of the clock is past the rejection cooldown
    keys = KeySource(exchange=Exchange("https://ach.example", "ai.run@example.com", "prod", str(token)),
                     client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
                     clock=lambda: next(ticks))
    assert await keys.refresh_rejected(0)
    return keys


def _model(keys: KeySource, upstream) -> ModelFacade:
    return ModelFacade(endpoint="https://ach.example/v1", headers={}, api="openai", model="m", key=KEY,
                       owned_workspace=_owned, identity=IDENTITY, auth=keys.auth,
                       client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))


async def _chat(facade: ModelFacade) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=facade.app()), base_url="http://f") as client:
        return await client.post(f"/t/{TOKEN}/chat/completions", json={"model": "m", "messages": []},
                                 headers={"x-ach-key": "sandbox-guess"})


async def test_model_and_mcp_facades_send_the_generation_current_at_each_request(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    model = _model(keys, upstream)
    mcp = McpFacade(servers={"s": "https://ach.example/mcp/s"}, server_headers={"s": {}}, key=KEY,
                    owned_workspace=_owned, exclude=frozenset(), exclude_tools=frozenset(), identity=IDENTITY,
                    auth=keys.auth, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp.app()), base_url="http://f") as client:
        assert (await _chat(model)).status_code == 200
        assert (await client.post(f"/t/{TOKEN}/mcp/s", json=ping)).status_code == 200
        assert await keys.refresh_rejected(keys.generation)  # a rotation: jwt-2
        assert (await _chat(model)).status_code == 200
        assert (await client.post(f"/t/{TOKEN}/mcp/s", json=ping)).status_code == 200
    assert seen == ["jwt-1", "jwt-1", "jwt-2", "jwt-2"]
    await keys.close()


async def test_a_credential_ach_rejects_is_reexchanged_and_the_request_sent_once_more(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        if request.headers["x-ach-key"] == "jwt-1":
            return httpx.Response(401, json=REJECTED)
        return httpx.Response(200, json={"ok": True})

    response = await _chat(_model(keys, upstream))
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert seen == ["jwt-1", "jwt-2"] and len(exchanges) == 2
    await keys.close()


async def test_an_upstream_401_is_passed_through_without_a_retry(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return httpx.Response(401, json={"error": {"message": "invalid api key", "type": "auth_error", "code": "401"}})

    assert (await _chat(_model(keys, upstream))).status_code == 401
    assert seen == ["jwt-1"] and len(exchanges) == 1
    await keys.close()


async def test_concurrent_requests_ach_rejects_share_one_reexchange(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)
    gate = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        if request.headers["x-ach-key"] == "jwt-1":
            await gate.wait()
            return httpx.Response(401, json=REJECTED)
        return httpx.Response(200, json={})

    model = _model(keys, upstream)
    tasks = [asyncio.create_task(_chat(model)) for _ in range(5)]
    async with asyncio.timeout(5):
        while seen.count("jwt-1") < 5:
            await asyncio.sleep(0.01)
    gate.set()
    responses = await asyncio.gather(*tasks)
    assert [r.status_code for r in responses] == [200] * 5
    assert len(exchanges) == 2 and seen.count("jwt-2") == 5
    await keys.close()


async def test_an_open_memory_mcp_session_carries_the_new_generation(tmp_path, monkeypatch) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)
    upstream = RecordingUpstream()
    upstream.responses["recall"] = "remembered"
    app = upstream.app()

    async def record(request: httpx.Request) -> None:
        seen.append(request.headers["x-ach-key"])
        if len(seen) == 1:  # rotate right after the session's initialize was sent
            await keys.refresh_rejected(keys.generation)

    def client(headers=None, auth=None, **_):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080/",
                                 headers=headers, auth=auth, event_hooks={"request": [record]})

    monkeypatch.setattr(_mcp_session, "create_mcp_http_client", client)
    async with app.router.lifespan_context(app):
        text = await call_ach_memory("http://localhost:8080/", {}, "recall", {"query": "q"},
                                     identity=IDENTITY, auth=keys.auth)
    assert text == "remembered"
    assert seen[0] == "jwt-1" and len(seen) >= 3 and set(seen[1:]) == {"jwt-2"}
    await keys.close()


async def test_the_a2a_client_built_once_sends_each_new_generation(tmp_path, monkeypatch) -> None:
    import a2a.client

    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)
    real = httpx.AsyncClient
    built: dict[str, httpx.AsyncClient] = {}

    def peer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return httpx.Response(200, json={})

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        built["http"] = client_config.httpx_client
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(peer), **kw))
    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://ach.example/a2a/peer", auth=keys.auth, identity=IDENTITY)
    await client._ensure_client()
    await built["http"].get("https://ach.example/a2a/peer")
    await keys.refresh_rejected(keys.generation)
    await built["http"].get("https://ach.example/a2a/peer")
    await client.close()
    assert seen == ["jwt-1", "jwt-2"]
    await keys.close()

# SPDX-License-Identifier: MIT
"""facades/{model,mcp}.py — ek_/identity injection only on the Harness->ACH hop, the
accepted Workspace HMAC authority (unknown/wrong-key/non-owned token = zero upstream
egress), and MCP server/tool exclusion (contract §5/§11; old-ach-agent
engine/mcp_proxy.py, identity.py, engine/trace.py).

Real httpx request plumbing end to end: an httpx.AsyncClient over httpx.ASGITransport
drives the real Starlette app, which forwards through a real (but MockTransport-backed)
httpx.AsyncClient to a fake upstream — only the two TCP sockets are stubbed out.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.tokens import facade_token

KEY = b"fixture-key"
IDENTITY = ProcessIdentity(agent="classifier", environment="platform")


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _streamed(status: int, data: bytes, *, headers: dict[str, str] | None = None) -> httpx.Response:
    """A genuinely lazy httpx.Response body (an AsyncIterable), not eagerly-read bytes —
    MockTransport marks a plain `content=bytes`/`json=` response as already consumed, so
    `forward()`'s real `stream=True` + `aiter_bytes()` (exercised against a real transport in
    production) would otherwise raise `httpx.StreamConsumed` here in tests only."""
    return httpx.Response(status, headers=headers or {}, content=_one_chunk(data))


def _owned_once(claim: str = "owned-ws") -> Any:
    """An async owned-live callback that is always `True` for `claim`, and records how
    many times it was asked — the HMAC check never caches, so one call per request."""
    calls: list[str] = []

    async def owned(candidate: str) -> bool:
        calls.append(candidate)
        return candidate == claim

    owned.calls = calls  # type: ignore[attr-defined]
    return owned


def _flipping(claim: str = "owned-ws") -> Any:
    """Owned-live callback that starts `True` and flips to `False` after each call."""
    state = {"owned": True, "count": 0}

    async def owned(candidate: str) -> bool:
        state["count"] += 1
        current = state["owned"] and candidate == claim
        state["owned"] = False
        return current

    owned.state = state  # type: ignore[attr-defined]
    return owned


async def _call(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://facade") as client:
        return await client.request(method, path, **kwargs)


# --- ModelFacade ----------------------------------------------------------------


def _model_facade(*, handler: Any, owned_workspace: Any = None, correlation_headers: Any = None) -> ModelFacade:
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ModelFacade(
        endpoint="https://ach.example.com/v1",
        ek="ek-xyz",
        key=KEY,
        owned_workspace=owned_workspace or _owned_once(),
        identity=IDENTITY,
        correlation_headers=correlation_headers,
        client=upstream,
    )


async def test_model_facade_injects_ek_and_identity_strips_inbound_auth_and_impersonation() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["url"] = str(request.url)
        return _streamed(200, b'{"ok": true}')

    facade = _model_facade(handler=handler)
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/chat/completions",
        json={"hello": "world"},
        headers={
            "Authorization": "Bearer dummy-client-token",
            "x-ach-key": "client-supplied-fake",
            "x-goog-api-key": "client-supplied-google",
            "X-Ach-Agent": "spoofed-agent",
            "x-ACH-Environment": "spoofed-environment",
        },
    )

    assert resp.status_code == 200
    headers = seen["headers"]
    assert headers["x-ach-key"] == "ek-xyz"
    assert headers["x-ach-agent"] == "classifier"
    assert headers["x-ach-environment"] == "platform"
    assert "authorization" not in headers
    assert "x-goog-api-key" not in headers
    assert seen["url"].endswith("/v1/chat/completions")


async def test_model_facade_denies_malformed_token_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    facade = _model_facade(handler=handler)
    resp = await _call(facade.app(), "POST", "/t/no-dot-here/chat/completions", json={})

    assert resp.status_code == 403
    assert called is False


async def test_model_facade_denies_wrong_key_token_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    facade = _model_facade(handler=handler)
    wrong_key_token = facade_token(b"a-different-key", "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{wrong_key_token}/chat/completions", json={})

    assert resp.status_code == 403
    assert called is False


async def test_model_facade_denies_non_owned_claim_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    owned = _owned_once("owned-ws")
    facade = _model_facade(handler=handler, owned_workspace=owned)
    token = facade_token(KEY, "someone-elses-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/chat/completions", json={})

    assert resp.status_code == 403
    assert called is False
    assert owned.calls == ["someone-elses-ws"]


async def test_model_facade_untokenized_route_is_404() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"ok": True})

    facade = _model_facade(handler=handler)
    resp = await _call(facade.app(), "POST", "/chat/completions", json={})

    assert resp.status_code == 404
    assert called is False


async def test_model_facade_a_valid_hmac_becomes_non_owned_between_two_calls() -> None:
    """Review focus 1: ownership is checked on every request, never cached."""
    called_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_count
        called_count += 1
        return _streamed(200, b'{"ok": true}')

    owned = _flipping("owned-ws")
    facade = _model_facade(handler=handler, owned_workspace=owned)
    token = facade_token(KEY, "owned-ws")

    first = await _call(facade.app(), "POST", f"/t/{token}/chat/completions", json={})
    assert first.status_code == 200
    assert called_count == 1

    second = await _call(facade.app(), "POST", f"/t/{token}/chat/completions", json={})
    assert second.status_code == 403
    assert called_count == 1, "second call must reach zero additional upstream egress"
    assert owned.state["count"] == 2


async def test_model_facade_streams_response_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(
            200,
            b"data: one\n\ndata: two\n\n",
            headers={"content-type": "text/event-stream"},
        )

    facade = _model_facade(handler=handler)
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/responses", json={"stream": True})

    assert resp.status_code == 200
    assert resp.content == b"data: one\n\ndata: two\n\n"


async def test_model_facade_strips_caller_correlation_when_no_trusted_carrier() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw"] = request.headers.raw
        return _streamed(200, b"{}")

    facade = _model_facade(handler=handler, correlation_headers=None)
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/chat/completions",
        json={},
        headers={
            "Traceparent": "00-" + "f" * 32 + "-" + "f" * 16 + "-01",
            "Tracestate": "vendor=forged",
            "langfuse_trace_id": "forged-trace",
            "x-litellm-session-id": "forged-session",
            "x-litellm-trace-id": "forged-litellm-trace",
        },
    )

    assert resp.status_code == 200
    names = {name.decode().lower() for name, _ in seen["raw"]}
    assert "traceparent" not in names
    assert "tracestate" not in names
    assert "langfuse_trace_id" not in names
    assert "x-litellm-session-id" not in names
    assert "x-litellm-trace-id" not in names


async def test_model_facade_trusted_correlation_carrier_overwrites_case_variant() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["raw"] = request.headers.raw
        return _streamed(200, b"{}")

    carrier = {"traceparent": "00-" + "a" * 32 + "-" + "a" * 16 + "-01", "langfuse_session_id": "ses-real"}
    facade = _model_facade(handler=handler, correlation_headers=lambda: carrier)
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/chat/completions",
        json={},
        headers={
            "TraceParent": "00-" + "f" * 32 + "-" + "f" * 16 + "-01",
            "X-Litellm-Trace-Id": "forged-litellm-trace",
        },
    )

    assert resp.status_code == 200
    pairs = [(name.decode().lower(), value.decode()) for name, value in seen["raw"]]
    assert [v for k, v in pairs if k == "traceparent"] == [carrier["traceparent"]]
    assert [v for k, v in pairs if k == "langfuse_session_id"] == ["ses-real"]
    assert not any(k == "x-litellm-trace-id" for k, _ in pairs)
    assert not any(v == "forged-litellm-trace" for _, v in pairs)


async def test_model_facade_mixed_case_identity_and_auth_headers_survive_a_naive_update() -> None:
    """Review focus 2: only one identity/credential pair reaches upstream, caller auth is
    fully gone, regardless of case variation on the inbound request."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["pairs"] = request.headers.raw
        return _streamed(200, b"{}")

    facade = _model_facade(handler=handler)
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/chat/completions",
        json={},
        headers={
            "AUTHORIZATION": "Bearer dummy",
            "X-ACH-KEY": "fake",
            "X-GOOG-API-KEY": "fake-google",
            "x-Ach-agent": "spoofed",
            "X-ach-ENVIRONMENT": "spoofed",
            "X-Litellm-Trace-Id": "forged-litellm-trace",
        },
    )

    assert resp.status_code == 200
    lowered = [(name.decode().lower(), value.decode()) for name, value in seen["pairs"]]
    assert [v for k, v in lowered if k == "x-ach-key"] == ["ek-xyz"]
    assert [v for k, v in lowered if k == "x-ach-agent"] == ["classifier"]
    assert [v for k, v in lowered if k == "x-ach-environment"] == ["platform"]
    assert not any(v in ("fake", "fake-google", "spoofed") for _, v in lowered)
    assert not any(k in ("authorization", "x-goog-api-key", "x-litellm-trace-id") for k, _ in lowered)


async def test_model_facade_a_chunked_inbound_body_is_forwarded_with_one_framing() -> None:
    """Legacy chunked-body oracle: the engine's gateway may stream a request body, but the
    facade re-sends it buffered — a double Content-Length+Transfer-Encoding framing upstream
    is rejected by real intermediaries."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        framing = {"content-length", "transfer-encoding"}
        seen["framing"] = sorted(k.lower() for k in request.headers if k.lower() in framing)
        seen["body"] = request.read()
        return _streamed(200, b'{"ok": true}')

    facade = _model_facade(handler=handler)
    token = facade_token(KEY, "owned-ws")

    async def chunks() -> AsyncIterator[bytes]:
        yield b'{"hello":'
        yield b'"world"}'

    resp = await _call(facade.app(), "POST", f"/t/{token}/chat/completions", content=chunks())

    assert resp.status_code == 200
    assert seen["framing"] == ["content-length"]
    assert seen["body"] == b'{"hello":"world"}'


# --- McpFacade --------------------------------------------------------------------


def _mcp_facade(
    *,
    handler: Any,
    servers: dict[str, str],
    exclude: frozenset[str] = frozenset(),
    exclude_tools: frozenset[str] = frozenset(),
    owned_workspace: Any = None,
    correlation_headers: Any = None,
) -> McpFacade:
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return McpFacade(
        servers=servers,
        ek="ek-xyz",
        key=KEY,
        owned_workspace=owned_workspace or _owned_once(),
        exclude=exclude,
        exclude_tools=exclude_tools,
        identity=IDENTITY,
        correlation_headers=correlation_headers,
        client=upstream,
    )


async def test_mcp_facade_injects_ek_and_identity_for_granted_server() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return _streamed(200, b'{"result": "ok"}')

    facade = _mcp_facade(handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"})
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/mcp/m1",
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
        headers={"Authorization": "Bearer dummy", "x-ach-key": "fake"},
    )

    assert resp.status_code == 200
    headers = seen["headers"]
    assert headers["x-ach-key"] == "ek-xyz"
    assert headers["x-ach-agent"] == "classifier"
    assert headers["x-ach-environment"] == "platform"
    assert "authorization" not in headers


async def test_mcp_facade_excludes_listed_server_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    facade = _mcp_facade(
        handler=handler,
        servers={"raw-memory-mcp": "https://ach.example.com/mcp/raw-memory-mcp"},
        exclude=frozenset({"raw-memory-mcp"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/raw-memory-mcp", json={})

    assert resp.status_code == 404
    assert called is False


async def test_mcp_facade_denies_unknown_token_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    facade = _mcp_facade(handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"})
    resp = await _call(facade.app(), "POST", "/t/garbage/mcp/m1", json={})

    assert resp.status_code == 403
    assert called is False


async def test_mcp_facade_denies_wrong_key_token_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    facade = _mcp_facade(handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"})
    wrong_key_token = facade_token(b"a-different-key", "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{wrong_key_token}/mcp/m1", json={})

    assert resp.status_code == 403
    assert called is False


async def test_mcp_facade_denies_non_owned_claim_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    owned = _owned_once("owned-ws")
    facade = _mcp_facade(
        handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"}, owned_workspace=owned
    )
    token = facade_token(KEY, "someone-elses-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/m1", json={})

    assert resp.status_code == 403
    assert called is False
    assert owned.calls == ["someone-elses-ws"]


async def test_mcp_facade_a_valid_hmac_becomes_non_owned_between_two_calls() -> None:
    """Review focus 1 / Important5: ownership is checked on every MCP request, never
    cached — same token, second call denies with zero additional upstream egress."""
    called_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called_count
        called_count += 1
        return _streamed(200, b'{"result": "ok"}')

    owned = _flipping("owned-ws")
    facade = _mcp_facade(
        handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"}, owned_workspace=owned
    )
    token = facade_token(KEY, "owned-ws")

    first = await _call(facade.app(), "POST", f"/t/{token}/mcp/m1", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert first.status_code == 200
    assert called_count == 1

    second = await _call(facade.app(), "POST", f"/t/{token}/mcp/m1", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1})
    assert second.status_code == 403
    assert called_count == 1, "second call must reach zero additional upstream egress"
    assert owned.state["count"] == 2


async def test_mcp_facade_untokenized_route_is_404() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    facade = _mcp_facade(handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"})
    resp = await _call(facade.app(), "POST", "/mcp/m1", json={})

    assert resp.status_code == 404
    assert called is False


async def test_mcp_facade_blocks_excluded_tool_call_with_zero_upstream_egress() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    facade = _mcp_facade(
        handler=handler,
        servers={"m1": "https://ach.example.com/mcp/m1"},
        exclude_tools=frozenset({"dangerous_tool"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/mcp/m1",
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "dangerous_tool"}, "id": 1},
    )

    assert resp.status_code == 403
    assert called is False


@pytest.mark.parametrize("sse", [False, True])
async def test_mcp_facade_hides_excluded_tools_from_tools_list(sse: bool) -> None:
    reply = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "m1.safe_tool"}, {"name": "m1.dangerous_tool"}]}}
    ).encode()
    content_type = "text/event-stream" if sse else "application/json"
    body = b"event: message\ndata: " + reply + b"\n\n" if sse else reply

    facade = _mcp_facade(
        handler=lambda request: _streamed(200, body, headers={"content-type": content_type}),
        servers={"m1": "https://ach.example.com/mcp/m1"},
        exclude_tools=frozenset({"m1.dangerous_tool"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(), "POST", f"/t/{token}/mcp/m1", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith(content_type)
    text = resp.text
    if sse:
        assert text.startswith("event: message\ndata: ")
        text = text.split("data: ", 1)[1]
    assert [t["name"] for t in json.loads(text)["result"]["tools"]] == ["m1.safe_tool"]


async def test_mcp_facade_tools_list_filter_keeps_sse_crlf_and_meta() -> None:
    meta = {"litellm.ai/server_outcomes": {"m1": "ok"}}
    reply = {"jsonrpc": "2.0", "id": 1, "result": {"_meta": meta, "tools": [{"name": "m1.safe_tool"}, {"name": "m1.dangerous_tool"}]}}
    body = b"event: message\r\ndata: " + json.dumps(reply).encode() + b"\r\n\r\n"

    facade = _mcp_facade(
        handler=lambda request: _streamed(200, body, headers={"content-type": "text/event-stream"}),
        servers={"m1": "https://ach.example.com/mcp/m1"},
        exclude_tools=frozenset({"m1.dangerous_tool"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(), "POST", f"/t/{token}/mcp/m1", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    )

    event, data, end = resp.content.split(b"\r\n", 2)
    assert event == b"event: message"
    assert end == b"\r\n"
    result = json.loads(data.removeprefix(b"data: "))["result"]
    assert result["_meta"] == meta
    assert [t["name"] for t in result["tools"]] == ["m1.safe_tool"]


@pytest.mark.parametrize(
    "reply",
    [b"<html>bad gateway</html>", b'{"result": [1]}', b'{"result": {"tools": [{"name": ["m1.dangerous_tool"]}]}}'],
)
async def test_mcp_facade_tools_list_unfilterable_reply_passes_through(reply: bytes) -> None:
    facade = _mcp_facade(
        handler=lambda request: _streamed(200, reply, headers={"content-type": "application/json"}),
        servers={"m1": "https://ach.example.com/mcp/m1"},
        exclude_tools=frozenset({"m1.dangerous_tool"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(), "POST", f"/t/{token}/mcp/m1", json={"jsonrpc": "2.0", "method": "tools/list", "id": 1}
    )

    assert resp.status_code == 200
    assert resp.content == reply


@pytest.mark.parametrize(
    ("exclude_tools", "streams"), [(frozenset(), True), (frozenset({"m1.dangerous_tool"}), False)]
)
async def test_mcp_facade_tools_list_streams_unless_filtering(exclude_tools: frozenset[str], streams: bool) -> None:
    """`_call` reads the whole body at once, so it cannot tell streamed from buffered: ask
    the handler directly which response type it built. Only a filtered `tools/list` buffers."""
    facade = _mcp_facade(
        handler=lambda request: _streamed(200, b'{"result": {"tools": []}}'),
        servers={"m1": "https://ach.example.com/mcp/m1"},
        exclude_tools=exclude_tools,
    )
    token = facade_token(KEY, "owned-ws")
    body = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "id": 1}).encode()
    messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    messages.put_nowait({"type": "http.request", "body": body, "more_body": False})

    scope = {
        "type": "http",
        "method": "POST",
        "path": f"/t/{token}/mcp/m1",
        "query_string": b"",
        "headers": [],
        "path_params": {"token": token, "server_id": "m1"},
    }
    response = await facade._handle(Request(scope, messages.get))
    try:
        assert isinstance(response, StreamingResponse) is streams
    finally:
        await facade.aclose()


async def test_mcp_facade_allows_non_excluded_tool_call() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _streamed(200, b'{"result": "ok"}')

    facade = _mcp_facade(
        handler=handler,
        servers={"m1": "https://ach.example.com/mcp/m1"},
        exclude_tools=frozenset({"dangerous_tool"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/mcp/m1",
        json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "safe_tool"}, "id": 1},
    )

    assert resp.status_code == 200
    assert called is True


async def test_mcp_facade_injects_trusted_correlation_into_message_meta_without_touching_arguments() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.read())
        return _streamed(200, b'{"result": "ok"}')

    carrier = {"traceparent": "00-" + "a" * 32 + "-" + "a" * 16 + "-01", "langfuse_session_id": "ses-1"}
    facade = _mcp_facade(
        handler=handler,
        servers={"m1": "https://ach.example.com/mcp/m1"},
        correlation_headers=lambda: carrier,
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/mcp/m1",
        json={"jsonrpc": "2.0", "method": "tools/call", "id": 1, "params": {"name": "search", "arguments": {"q": "x"}}},
    )

    assert resp.status_code == 200
    body = seen["body"]
    assert body["params"]["_meta"] == carrier
    assert body["params"]["arguments"] == {"q": "x"}


async def test_mcp_facade_meta_injection_is_a_no_op_on_a_non_object_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return _streamed(200, b'{"result": "ok"}')

    carrier = {"traceparent": "00-" + "a" * 32 + "-" + "a" * 16 + "-01"}
    facade = _mcp_facade(
        handler=handler,
        servers={"m1": "https://ach.example.com/mcp/m1"},
        correlation_headers=lambda: carrier,
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/m1", content=b"not-json-at-all")

    assert resp.status_code == 200
    assert seen["body"] == b"not-json-at-all"


async def test_mcp_facade_meta_injection_is_a_no_op_on_a_json_array_body() -> None:
    """A JSON-RPC batch (a top-level array) is valid JSON but not a single request object
    — inject_meta must leave it untouched rather than raising or guessing a target."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return _streamed(200, b'{"result": "ok"}')

    carrier = {"traceparent": "00-" + "a" * 32 + "-" + "a" * 16 + "-01"}
    facade = _mcp_facade(
        handler=handler,
        servers={"m1": "https://ach.example.com/mcp/m1"},
        correlation_headers=lambda: carrier,
    )
    token = facade_token(KEY, "owned-ws")
    batch_body = b'[{"jsonrpc": "2.0", "method": "tools/list", "id": 1}]'
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/m1", content=batch_body)

    assert resp.status_code == 200
    assert seen["body"] == batch_body


async def test_mcp_facade_meta_injection_is_a_no_op_on_a_response_shaped_body() -> None:
    """A JSON-RPC response (has `result`, no `method`) must not get `_meta` injected —
    `inject_meta` only ever touches request objects."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.read()
        return _streamed(200, b'{"result": "ok"}')

    carrier = {"traceparent": "00-" + "a" * 32 + "-" + "a" * 16 + "-01"}
    facade = _mcp_facade(
        handler=handler,
        servers={"m1": "https://ach.example.com/mcp/m1"},
        correlation_headers=lambda: carrier,
    )
    token = facade_token(KEY, "owned-ws")
    response_body = b'{"jsonrpc": "2.0", "id": 1, "result": {"ok": true}}'
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/m1", content=response_body)

    assert resp.status_code == 200
    assert seen["body"] == response_body


async def test_mcp_facade_relays_query_non_200_status_and_preserves_session_header() -> None:
    """Review focus 5: exact query-string relay, a non-200 upstream status passed through
    unchanged, and Mcp-Session-Id preserved in the response (while the drop set still
    removes framing headers)."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(httpx.QueryParams(request.url.query))
        return httpx.Response(
            201,
            headers={
                "Mcp-Session-Id": "sess-xyz",
                "Content-Length": "2",
                "Transfer-Encoding": "chunked",
            },
            content=_one_chunk(b"{}"),
        )

    facade = _mcp_facade(handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"})
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/mcp/m1?sessionId=abc&foo=bar",
        json={"jsonrpc": "2.0", "method": "tools/list", "id": 1},
    )

    assert resp.status_code == 201
    assert seen["query"] == {"sessionId": "abc", "foo": "bar"}
    header_names = {k.lower() for k in resp.headers}
    assert resp.headers["mcp-session-id"] == "sess-xyz"
    assert "content-length" not in header_names
    assert "transfer-encoding" not in header_names
    assert "content-encoding" not in header_names


# --- Security review 2026-10-26: path escape, batch exclusion, compressed replies --------


@pytest.mark.parametrize("tail", ["%2e%2e/platform/hydrate", "a/%2E%2E/%2e%2e/platform", "%2e%2e", "%252e%252e/platform/hydrate"])
async def test_model_facade_refuses_a_tail_that_escapes_the_endpoint(tail: str) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _streamed(200, b"{}")

    facade = _model_facade(handler=handler)
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/{tail}")

    assert resp.status_code == 400
    assert called is False


async def test_mcp_facade_refuses_a_tail_that_reaches_an_excluded_server() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _streamed(200, b"{}")

    facade = _mcp_facade(
        handler=handler,
        servers={"allowed": "https://ach.example.com/mcp/allowed", "excluded": "https://ach.example.com/mcp/excluded"},
        exclude=frozenset({"excluded"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/allowed/%2e%2e/excluded")

    assert resp.status_code == 400
    assert called is False


async def test_mcp_facade_keeps_a_tail_inside_the_server_path() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _streamed(200, b"{}")

    facade = _mcp_facade(handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"})
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/m1/sub/./path")

    assert resp.status_code == 200
    assert seen == ["https://ach.example.com/mcp/m1/sub/path"]


async def test_mcp_facade_blocks_excluded_tool_call_inside_a_batch() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _streamed(200, b"[]")

    facade = _mcp_facade(
        handler=handler,
        servers={"m1": "https://ach.example.com/mcp/m1"},
        exclude_tools=frozenset({"delete_all"}),
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/mcp/m1",
        json=[
            {"jsonrpc": "2.0", "method": "tools/list", "id": 1},
            {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "delete_all"}, "id": 2},
        ],
    )

    assert resp.status_code == 403
    assert called is False


async def test_model_facade_decodes_a_gzip_reply_it_strips_content_encoding_from() -> None:
    import gzip

    def handler(request: httpx.Request) -> httpx.Response:
        return _streamed(200, gzip.compress(b'{"ok": true}'), headers={"content-encoding": "gzip"})

    facade = _model_facade(handler=handler)
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/chat/completions")

    assert "content-encoding" not in resp.headers
    assert resp.json() == {"ok": True}


async def test_mcp_facade_refuses_a_double_encoded_escape() -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _streamed(200, b"{}")

    facade = _mcp_facade(handler=handler, servers={"allowed": "https://ach.example.com/mcp/allowed"})
    token = facade_token(KEY, "owned-ws")
    resp = await _call(facade.app(), "POST", f"/t/{token}/mcp/allowed/%252e%252e/excluded")

    assert resp.status_code == 400
    assert called is False


@pytest.mark.parametrize(
    "params",
    [{"name": ["delete_all"]}, {"name": {"x": 1}}, {"name": 7}],
)
async def test_mcp_facade_denies_a_non_string_tool_name_when_tools_are_excluded(params: dict[str, Any]) -> None:
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _streamed(200, b"{}")

    facade = _mcp_facade(
        handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"}, exclude_tools=frozenset({"delete_all"})
    )
    token = facade_token(KEY, "owned-ws")
    resp = await _call(
        facade.app(),
        "POST",
        f"/t/{token}/mcp/m1",
        json={"jsonrpc": "2.0", "method": "tools/call", "params": params, "id": 1},
    )

    assert resp.status_code == 403
    assert called is False


async def test_mcp_facade_refuses_an_encoded_body_when_tools_are_excluded() -> None:
    import gzip

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return _streamed(200, b"{}")

    facade = _mcp_facade(
        handler=handler, servers={"m1": "https://ach.example.com/mcp/m1"}, exclude_tools=frozenset({"delete_all"})
    )
    token = facade_token(KEY, "owned-ws")
    body = gzip.compress(json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "delete_all"}}).encode())
    resp = await _call(
        facade.app(), "POST", f"/t/{token}/mcp/m1", content=body, headers={"content-encoding": "gzip"}
    )

    assert resp.status_code == 415
    assert called is False


async def test_facade_does_not_forward_the_client_accept_encoding() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["accept-encoding"] = request.headers.get("accept-encoding", "")
        return _streamed(200, b"{}")

    facade = _model_facade(handler=handler)
    token = facade_token(KEY, "owned-ws")
    await _call(facade.app(), "POST", f"/t/{token}/chat/completions", headers={"accept-encoding": "x-unsupported"})

    assert "x-unsupported" not in seen["accept-encoding"]

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

import stub


@pytest.fixture
def servers():
    state = stub.State()
    servers = stub.make_servers(state, {"model": 0, "mcp": 0, "ach": 0})
    for s in servers.values():
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield {k: f"http://127.0.0.1:{s.server_address[1]}" for k, s in servers.items()}, state
    for s in servers.values():
        s.shutdown()
        s.server_close()


def call(url, method="GET", headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_credential_required_and_accepted(servers):
    urls, _ = servers
    chat = {"model": "m", "messages": []}
    assert call(urls["model"] + "/v1/chat/completions", "POST", body=chat)[0] == 401
    assert call(urls["model"] + "/v1/chat/completions", "POST", {"x-litellm-api-key": "nope"}, chat)[0] == 401
    status, text = call(urls["model"] + "/v1/chat/completions", "POST", {"x-litellm-api-key": "e2e-model-key"}, chat)
    assert status == 200 and json.loads(text)["choices"][0]["message"]["content"] == stub.REPLY
    status, text = call(urls["model"] + "/v1/chat/completions", "POST", {"x-litellm-api-key": "e2e-model-key"},
                        {**chat, "stream": True})
    assert status == 200 and text.endswith("data: [DONE]\n\n")

    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    assert call(urls["mcp"] + "/mcp", "POST", body=init)[0] == 401
    status, text = call(urls["mcp"] + "/mcp", "POST", {"X-API-KEY": "e2e-serper-key"}, init)
    assert status == 200 and "protocolVersion" in json.loads(text)["result"]
    listing = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
    assert json.loads(call(urls["mcp"] + "/mcp", "POST", {"x-api-key": "e2e-serper-key"}, listing)[1])["result"]["tools"][0]["name"] == "echo"

    assert call(urls["ach"] + "/platform/hydrate", "POST", body={})[0] == 401
    status, text = call(urls["ach"] + "/platform/hydrate", "POST", {"x-ach-key": "e2e-ek"}, {})
    assert status == 200 and json.loads(text)["runtime"]["models"][0]["id"] == "e2e-model"


def test_requests_are_recorded_and_cleared(servers):
    urls, _ = servers
    port = int(urls["mcp"].rsplit(":", 1)[1])
    call(urls["mcp"] + "/mcp", "POST", {"X-API-KEY": "e2e-serper-key"}, {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    call(urls["mcp"] + "/mcp", "POST", {"X-API-KEY": "wrong"}, {})
    recorded = json.loads(call(f"{urls['mcp']}/_requests?port={port}")[1])
    assert [r["headers"]["x-api-key"] for r in recorded] == ["e2e-serper-key", "wrong"]
    assert call(urls["mcp"] + "/_requests", "DELETE")[0] == 200
    assert json.loads(call(f"{urls['mcp']}/_requests?port={port}")[1]) == []


def test_credential_rotation(servers):
    urls, _ = servers
    chat = {"model": "m"}
    assert call(urls["model"] + "/_creds", "PUT", body={"model": "k2"})[0] == 200
    assert call(urls["model"] + "/v1/chat/completions", "POST", {"x-litellm-api-key": "e2e-model-key"}, chat)[0] == 401
    assert call(urls["model"] + "/v1/chat/completions", "POST", {"x-litellm-api-key": "k2"}, chat)[0] == 200


def test_duplicate_headers_are_kept_in_raw_headers(servers):
    urls, _ = servers
    port = int(urls["model"].rsplit(":", 1)[1])
    req = urllib.request.Request(urls["model"] + "/v1/chat/completions", data=b"{}", method="POST")
    req.add_header("x-litellm-api-key", "e2e-model-key")
    req.add_unredirected_header("x-dup", "a")
    req.add_header("x-dup2", "b")
    urllib.request.urlopen(req, timeout=5).read()
    recorded = json.loads(call(f"{urls['model']}/_requests?port={port}")[1])
    assert ["x-litellm-api-key", "e2e-model-key"] in recorded[0]["raw_headers"]


GRANT = {"identity": stub.EXCHANGE_IDENTITY, "environment": "e2e"}


@pytest.fixture
def exchange():
    reviews = []

    def review(token):
        reviews.append(token)
        return {"authenticated": token == "good-sa", "audiences": [stub.EXCHANGE_BASE],
                "user": {"username": "system:serviceaccount:ach-agent-e2e:ach-agent-control-x",
                         "extra": {"authentication.kubernetes.io/pod-name": ["agent-x-0"]}}}

    state = stub.State(review=review)
    server = stub.make_servers(state, {"exchange": 0})["exchange"]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state, reviews
    server.shutdown()
    server.server_close()


def test_exchange_issues_a_jwt_accepted_until_it_expires(exchange):
    url, state, reviews = exchange
    status, text = call(url + "/platform/keys/exchange", "POST", {"Authorization": "Bearer good-sa"}, GRANT)
    assert status == 200
    reply = json.loads(text)
    assert reply["expires_in"] == stub.JWT_TTL and reply["token"].startswith("eyJ") and reviews == ["good-sa"]
    chat = {"model": "e2e-model", "messages": []}
    assert call(url + "/v1/chat/completions", "POST", {"x-ach-key": reply["token"]}, chat)[0] == 200
    state.jwts[reply["token"]] = 0  # expired
    status, text = call(url + "/v1/chat/completions", "POST", {"x-ach-key": reply["token"]}, chat)
    assert status == 401 and json.loads(text)["error"]["code"] == "expired_or_revoked"


def test_exchange_refusals_carry_achs_codes(exchange):
    url, _, _ = exchange
    status, text = call(url + "/platform/keys/exchange", "POST", {"Authorization": "Bearer bad-sa"}, GRANT)
    assert status == 401 and json.loads(text)["error"]["code"] == "invalid_token"
    status, text = call(url + "/platform/keys/exchange", "POST", {"Authorization": "Bearer good-sa"},
                        {**GRANT, "identity": "ceo@example.com"})
    assert status == 403 and json.loads(text)["error"]["code"] == "not_granted"
    assert call(url + "/platform/keys/exchange", "POST", {}, GRANT)[0] == 401


def test_exchange_mcp_needs_the_session_it_opened(exchange):
    url, state, _ = exchange
    jwt = state.issue()
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    req = urllib.request.Request(url + "/mcp/e2e", data=json.dumps(init).encode(), method="POST",
                                 headers={"x-ach-key": jwt})
    with urllib.request.urlopen(req, timeout=5) as r:
        sid = r.headers["Mcp-Session-Id"]
    echo = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}}
    assert call(url + "/mcp/e2e", "POST", {"x-ach-key": jwt}, echo)[0] == 404
    status, text = call(url + "/mcp/e2e", "POST", {"x-ach-key": jwt, "Mcp-Session-Id": sid}, echo)
    assert status == 200 and json.loads(text)["result"]["content"][0]["text"] == "hi"


def test_a_slow_stream_drips_one_chunk_per_second(exchange):
    url, state, _ = exchange
    started = time.monotonic()
    status, text = call(url + "/v1/chat/completions", "POST", {"x-ach-key": state.issue()},
                        {"model": "e2e-model", "messages": [], "stream": True, "e2e_stream_seconds": 2})
    assert status == 200 and text.endswith("data: [DONE]\n\n") and text.count("data: ") == 2 + 4
    assert time.monotonic() - started >= 2

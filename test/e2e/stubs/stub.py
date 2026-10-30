# SPDX-License-Identifier: MIT
"""Stub upstreams for the ach-agent e2e: an OpenAI-compatible model (8090), a streamable-HTTP MCP
server (8091), an ACH server keyed by a static ek_ (8092) and an HTTPS ACH server keyed by JWTs it
issues in exchange for a ServiceAccount token it checks with a real TokenReview (8093). Each
records every request, headers included, so tests assert what reached the upstream. stdlib only."""

import base64
import json
import os
import secrets
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REPLY = "E2E-OK"
BODY_LIMIT = 64 * 1024
ACH_BASE = os.environ.get("STUB_ACH_BASE", "http://e2e-stub.ach-agent-e2e.svc.cluster.local:8092")
# The exchange server's URL is also the audience the Agent's projected token must carry.
EXCHANGE_BASE = os.environ.get("STUB_EXCHANGE_BASE", "https://e2e-stub.ach-agent-e2e.svc.cluster.local:8093")
EXCHANGE_IDENTITY = os.environ.get("STUB_EXCHANGE_IDENTITY", "ai.run@example.com")
# The e2e profile names no control ServiceAccount, so Agents run as ach-agent-control-<uid>.
EXCHANGE_SA_PREFIX = "system:serviceaccount:ach-agent-e2e:ach-agent-control-"
JWT_TTL = int(os.environ.get("STUB_JWT_TTL_SECONDS", "60"))
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# kind -> (header, accepted value); PUT /_creds replaces a value.
DEFAULT_CREDS = {
    "model": ("x-litellm-api-key", "e2e-model-key"),
    "mcp": ("x-api-key", "e2e-serper-key"),
    "ach": ("x-ach-key", "e2e-ek"),
}
REJECTED = {"error": {"code": "expired_or_revoked", "message": "key expired or revoked"}, "request_id": "req_e2e"}


def token_review(token):
    """A real TokenReview with the stub's own ServiceAccount (bound to system:auth-delegator)."""
    with open(f"{SA_DIR}/token") as f:
        own = f.read().strip()
    body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
                       "spec": {"token": token, "audiences": [EXCHANGE_BASE]}}).encode()
    req = urllib.request.Request("https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews",
                                 data=body, method="POST",
                                 headers={"Authorization": f"Bearer {own}", "Content-Type": "application/json"})
    context = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    with urllib.request.urlopen(req, context=context, timeout=10) as r:
        return json.loads(r.read())["status"]


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class State:
    def __init__(self, review=token_review):
        self.lock = threading.Lock()
        self.creds = dict(DEFAULT_CREDS)
        self.records = {}  # port -> [request dict]
        self.review = review
        self.jwts = {}  # issued JWT -> monotonic expiry
        self.sessions = set()

    def record(self, port, entry):
        with self.lock:
            self.records.setdefault(port, []).append(entry)
        return entry

    def listing(self, port):
        with self.lock:
            return list(self.records.get(port, []))

    def clear(self):
        with self.lock:
            self.records.clear()

    def issue(self):
        jwt = ".".join((_b64(b'{"alg":"none","typ":"JWT"}'), _b64(b'{"aud":"ach-ek"}'), secrets.token_urlsafe(16)))
        with self.lock:
            self.jwts[jwt] = time.monotonic() + JWT_TTL
        return jwt

    def jwt_valid(self, jwt):
        with self.lock:
            return time.monotonic() < self.jwts.get(jwt, 0)

    def open_session(self):
        sid = secrets.token_hex(8)
        with self.lock:
            self.sessions.add(sid)
        return sid


def _chat(model, stream):
    base = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    if not stream:
        return {**base, "object": "chat.completion", "usage": usage, "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": REPLY}}]}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"content": REPLY}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _mcp(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion", "2025-03-26")
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "e2e-stub", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo the text",
                             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        text = ((msg.get("params") or {}).get("arguments") or {}).get("text", "")
        result = {"content": [{"type": "text", "text": text}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def handler_for(state, kind):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status, body=b"", ctype="application/json", headers=None):
            if not isinstance(body, bytes):
                body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _admin(self, url):
            query = parse_qs(url.query)
            if self.command == "GET":
                port = int(query["port"][0])
                return self._send(200, state.listing(port))
            if self.command == "DELETE":
                state.clear()
                return self._send(200, {"cleared": True})
            if self.command == "PUT":  # /_creds {"model": "new", ...}
                for k, v in json.loads(self._body()).items():
                    state.creds[k] = (state.creds[k][0], v)
                return self._send(200, {"ok": True})
            return self._send(405)

        def _stream(self, model, seconds):
            """An SSE reply that sends one chunk per second for `seconds`, then ends normally."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(text):
                data = text.encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

            tick = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model, "choices": [{"index": 0, "delta": {"content": "."}, "finish_reason": None}]}
            for _ in range(seconds):
                chunk(f"data: {json.dumps(tick)}\n\n")
                time.sleep(1)
            chunk(_chat(model, True))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _exchange(self, body):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return self._send(401, {"error": {"code": "invalid_token", "message": "bearer token required"}})
            try:
                status = state.review(auth[len("Bearer "):])
            except Exception as exc:  # noqa: BLE001 - the API server did not answer the review
                return self._send(503, {"error": {"code": "not_ready", "message": type(exc).__name__}})
            user = status.get("user") or {}
            if not (status.get("authenticated") and EXCHANGE_BASE in (status.get("audiences") or [])
                    and user.get("username", "").startswith(EXCHANGE_SA_PREFIX)
                    and (user.get("extra") or {}).get("authentication.kubernetes.io/pod-name")):
                return self._send(401, {"error": {"code": "invalid_token", "message": "token review failed"}})
            doc = json.loads(body or b"{}")
            if doc.get("identity") != EXCHANGE_IDENTITY or doc.get("environment") != "e2e":
                return self._send(403, {"error": {"code": "not_granted", "message": "no grant"}})
            return self._send(200, {"token": state.issue(), "expires_in": JWT_TTL, "key_id": "ekid_e2e"})

        def _mcp_session(self, doc, headers):
            if self.command != "POST":
                return self._send(405)
            sid = headers.get("mcp-session-id", "")
            if doc.get("method") == "initialize":
                sid = state.open_session()
            elif sid not in state.sessions:
                return self._send(404, {"error": "unknown MCP session"})
            reply = _mcp(doc)
            return self._send(202 if reply is None else 200, b"" if reply is None else reply,
                              headers={"Mcp-Session-Id": sid})

        def _handle(self):
            url = urlsplit(self.path)
            if url.path in ("/_requests", "/_creds"):
                return self._admin(url)
            body = self._body()
            headers = {k.lower(): v for k, v in self.headers.items()}
            entry = state.record(self.server.server_address[1], {
                "method": self.command, "path": url.path, "headers": headers,
                "raw_headers": [[k.lower(), v] for k, v in self.headers.items()],
                "body": body[:BODY_LIMIT].decode("utf-8", "replace"), "time": time.time()})
            if url.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if kind == "exchange":
                if url.path == "/platform/keys/exchange" and self.command == "POST":
                    return self._exchange(body)
                if not state.jwt_valid(headers.get("x-ach-key", "")):
                    # Listed with the request, so a test can tell a rotated client (never rejected)
                    # from one that was rejected and re-exchanged.
                    entry["rejected"] = REJECTED["error"]["code"]
                    return self._send(401, REJECTED)
            else:
                name, want = state.creds[kind]
                if headers.get(name) != want:
                    return self._send(401, {"error": f"{name} missing or wrong"})
            try:
                doc = json.loads(body) if body else {}
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            if kind != "mcp" and url.path.endswith("/chat/completions") and self.command == "POST":
                seconds = int(doc.get("e2e_stream_seconds") or 0)
                if doc.get("stream") and seconds:
                    return self._stream(doc.get("model", ""), seconds)
                if doc.get("stream"):
                    return self._send(200, _chat(doc.get("model", ""), True), "text/event-stream")
                return self._send(200, _chat(doc.get("model", ""), False))
            if kind == "mcp" and url.path == "/mcp":
                if self.command == "POST":
                    reply = _mcp(doc)
                    return self._send(202) if reply is None else self._send(200, reply)
                return self._send(405) if self.command == "GET" else self._send(200)
            if kind == "exchange" and url.path == "/mcp/e2e":
                return self._mcp_session(doc, headers)
            if kind in ("ach", "exchange") and url.path == "/platform/hydrate" and self.command == "POST":
                base = EXCHANGE_BASE if kind == "exchange" else ACH_BASE
                mcp = [{"id": "e2e-mcp", "endpoint": f"{EXCHANGE_BASE}/mcp/e2e"}] if kind == "exchange" else []
                return self._send(200, {
                    "environment": "e2e",
                    "runtime": {"models": [{"id": "e2e-model", "endpoint": f"{base}/v1"}],
                                "mcpServers": mcp, "a2aAgents": []},
                    "context": {"skills": [], "prompts": [], "artifacts": []}})
            if kind in ("ach", "exchange") and url.path == "/v2/model/info":
                return self._send(200, {"data": []})
            return self._send(404, {"error": "not found"})

        do_GET = do_POST = do_PUT = do_DELETE = _handle

    return Handler


def make_servers(state, ports):
    """ports: kind -> port (0 picks one). Returns kind -> ThreadingHTTPServer, not yet serving."""
    return {kind: ThreadingHTTPServer(("0.0.0.0", port), handler_for(state, kind)) for kind, port in ports.items()}


if __name__ == "__main__":
    state = State()
    servers = make_servers(state, {"model": 8090, "mcp": 8091, "ach": 8092, "exchange": 8093})
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(os.environ.get("STUB_TLS_CERT", "/tls/tls.crt"), os.environ.get("STUB_TLS_KEY", "/tls/tls.key"))
    servers["exchange"].socket = tls.wrap_socket(servers["exchange"].socket, server_side=True)
    for s in list(servers.values())[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers["model"].serve_forever()

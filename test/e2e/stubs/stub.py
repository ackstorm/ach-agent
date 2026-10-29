"""Stub upstreams for the ach-agent e2e: an OpenAI-compatible model (8090), a streamable-HTTP MCP
server (8091) and an ACH server (8092). Each requires its credential and records every request,
headers included, so tests assert what reached the upstream. stdlib only."""

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REPLY = "E2E-OK"
BODY_LIMIT = 64 * 1024
ACH_BASE = os.environ.get("STUB_ACH_BASE", "http://e2e-stub.ach-agent-e2e.svc.cluster.local:8092")
# kind -> (header, accepted value); PUT /_creds replaces a value.
DEFAULT_CREDS = {
    "model": ("x-litellm-api-key", "e2e-model-key"),
    "mcp": ("x-api-key", "e2e-serper-key"),
    "ach": ("x-ach-key", "e2e-ek"),
}


class State:
    def __init__(self):
        self.lock = threading.Lock()
        self.creds = dict(DEFAULT_CREDS)
        self.records = {}  # port -> [request dict]

    def record(self, port, entry):
        with self.lock:
            self.records.setdefault(port, []).append(entry)

    def listing(self, port):
        with self.lock:
            return list(self.records.get(port, []))

    def clear(self):
        with self.lock:
            self.records.clear()


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

        def _send(self, status, body=b"", ctype="application/json"):
            if not isinstance(body, bytes):
                body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
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

        def _handle(self):
            url = urlsplit(self.path)
            if url.path in ("/_requests", "/_creds"):
                return self._admin(url)
            body = self._body()
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.record(self.server.server_address[1], {
                "method": self.command, "path": url.path, "headers": headers,
                "raw_headers": [[k.lower(), v] for k, v in self.headers.items()],
                "body": body[:BODY_LIMIT].decode("utf-8", "replace"), "time": time.time()})
            if url.path == "/healthz":
                return self._send(200, {"status": "ok"})
            name, want = state.creds[kind]
            if headers.get(name) != want:
                return self._send(401, {"error": f"{name} missing or wrong"})
            try:
                doc = json.loads(body) if body else {}
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            if kind in ("model", "ach") and url.path.endswith("/chat/completions") and self.command == "POST":
                if doc.get("stream"):
                    return self._send(200, _chat(doc.get("model", ""), True), "text/event-stream")
                return self._send(200, _chat(doc.get("model", ""), False))
            if kind == "mcp" and url.path == "/mcp":
                if self.command == "POST":
                    reply = _mcp(doc)
                    return self._send(202) if reply is None else self._send(200, reply)
                return self._send(405) if self.command == "GET" else self._send(200)
            if kind == "ach" and url.path == "/platform/hydrate" and self.command == "POST":
                return self._send(200, {
                    "environment": "e2e",
                    "runtime": {"models": [{"id": "e2e-model", "endpoint": f"{ACH_BASE}/v1"}],
                                "mcpServers": [], "a2aAgents": []},
                    "context": {"skills": [], "prompts": [], "artifacts": []}})
            if kind == "ach" and url.path == "/v2/model/info":
                return self._send(200, {"data": []})
            return self._send(404, {"error": "not found"})

        do_GET = do_POST = do_PUT = do_DELETE = _handle

    return Handler


def make_servers(state, ports):
    """ports: kind -> port (0 picks one). Returns kind -> ThreadingHTTPServer, not yet serving."""
    return {kind: ThreadingHTTPServer(("0.0.0.0", port), handler_for(state, kind)) for kind, port in ports.items()}


if __name__ == "__main__":
    state = State()
    servers = make_servers(state, {"model": 8090, "mcp": 8091, "ach": 8092})
    for s in list(servers.values())[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers["model"].serve_forever()

# SPDX-License-Identifier: MIT
"""Acceptance load across credential rotations: model calls, MCP calls on one session kept open
throughout, and one long model stream, all through an Agent's facades. Prints one JSON summary
line. stdlib only."""

import argparse
import json
import threading
import time
import urllib.request


def _post(url, body, headers=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


class Tally:
    def __init__(self):
        self.lock = threading.Lock()
        self.ok = {"model": 0, "mcp": 0}
        self.failed = {"model": 0, "mcp": 0}
        self.errors = []

    def mark(self, kind, error=None):
        with self.lock:
            if error is None:
                self.ok[kind] += 1
                return
            self.failed[kind] += 1
            if len(self.errors) < 10:
                self.errors.append(f"{kind}: {error}")


def model_loop(base, model, deadline, tally):
    while time.monotonic() < deadline:
        try:
            with _post(f"{base}/chat/completions", {"model": model, "messages": [{"role": "user", "content": "hi"}]}) as r:
                body = json.loads(r.read())
            if body["choices"][0]["message"]["content"] != "E2E-OK":
                raise ValueError(body)
            tally.mark("model")
        except Exception as exc:  # noqa: BLE001 - every failure counts
            tally.mark("model", exc)
        time.sleep(0.5)


def mcp_loop(url, deadline, tally):
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "e2e-load", "version": "1"}}}
    try:
        with _post(url, init) as r:
            session = r.headers["Mcp-Session-Id"]
            r.read()
        with _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"Mcp-Session-Id": session}) as r:
            r.read()
    except Exception as exc:  # noqa: BLE001
        tally.mark("mcp", exc)
        return
    n = 0
    while time.monotonic() < deadline:
        n += 1
        call = {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": f"n{n}"}}}
        try:
            with _post(url, call, {"Mcp-Session-Id": session}) as r:
                body = json.loads(r.read())
            if body["result"]["content"][0]["text"] != f"n{n}":
                raise ValueError(body)
            tally.mark("mcp")
        except Exception as exc:  # noqa: BLE001
            tally.mark("mcp", exc)
        time.sleep(0.5)


def stream(base, model, seconds, result):
    body = {"model": model, "stream": True, "e2e_stream_seconds": seconds, "messages": [{"role": "user", "content": "hi"}]}
    try:
        with _post(f"{base}/chat/completions", body, timeout=60) as r:
            text = r.read().decode()
        result["ok"] = text.rstrip().endswith("data: [DONE]") and text.count("data: ") > seconds
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--facade", required=True)  # http://agent-<name>.<ns>.svc.cluster.local:8081/facades
    p.add_argument("--token", required=True)
    p.add_argument("--model", default="e2e-model")
    p.add_argument("--mcp", default="e2e-mcp")
    p.add_argument("--seconds", type=int, default=200)
    a = p.parse_args()
    base = f"{a.facade}/model/t/{a.token}"
    deadline = time.monotonic() + a.seconds
    tally, streamed = Tally(), {"ok": False}
    threads = [
        threading.Thread(target=model_loop, args=(base, a.model, deadline, tally)),
        threading.Thread(target=mcp_loop, args=(f"{a.facade}/mcp/t/{a.token}/mcp/{a.mcp}", deadline, tally)),
        threading.Thread(target=stream, args=(base, a.model, a.seconds, streamed)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(json.dumps({"model_ok": tally.ok["model"], "model_failed": tally.failed["model"],
                      "mcp_ok": tally.ok["mcp"], "mcp_failed": tally.failed["mcp"],
                      "stream_ok": streamed["ok"], "stream_error": streamed.get("error", ""), "errors": tally.errors}))


if __name__ == "__main__":
    main()

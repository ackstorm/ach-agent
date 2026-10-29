#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""A real loopback process standing in for `opencode2 serve --stdio`'s HTTP/SSE wire,
scripted over stdin and introspected over `/_control/state` — not the real binary, because
these tests need to deterministically withhold responses, inject out-of-order/stale
`durable.seq` events, and return malformed bodies, none of which the real binary can be
coerced into on demand.

Event/receipt shapes here (`durable.aggregateID`/`seq`, `session.execution.started/
succeeded/failed/interrupted`, `session.inbox.enqueued/delivered`, the `{data:[...]}`
inbox envelope, `{"interrupted": true}` on interrupt) are copied from a live capture
against the real pinned `@opencode/cli@2.0.20` binary, not invented.

Protocol:
  - stdout: one JSON line `{"url": "http://127.0.0.1:PORT"}` once listening, then silent
    unless `flood` writes more (never closed by us).
  - stdin: newline-delimited JSON commands (see `_handle_command`); EOF is the documented
    clean-stop signal — the process exits 0.
  - HTTP: the opencode2 subset `_NativeSession`/`OpenCodeEngine` actually calls, plus
    `/_control/state` (GET) for test-side synchronization.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import time

HOST = "127.0.0.1"


class _Session:
    def __init__(self, sid: str) -> None:
        self.sid = sid
        self.seq = 0
        self.inbox: dict[str, dict] = {}

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq


class Server:
    def __init__(self) -> None:
        self.sessions: dict[str, _Session] = {}
        self._session_counter = 0
        self._sse_queues: list[asyncio.Queue[bytes]] = []
        self._received: dict[str, int] = {}
        self._stalls: dict[str, dict] = {}
        self._once: dict[str, list[dict]] = {}
        self._children: list[int] = []

    # ---------------------------------------------------------------- control (stdin)

    async def read_commands(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            line = await reader.readline()
            if not line:
                return  # EOF: the documented clean-stop signal
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            await self._handle_command(cmd)

    async def _handle_command(self, cmd: dict) -> None:
        op = cmd.get("op")
        if op == "emit":
            sid = cmd["sid"]
            session = self.sessions.setdefault(sid, _Session(sid))
            seq = cmd.get("seq")
            if seq is None:
                seq = session.next_seq()
            event = {
                "id": f"evt_{seq}",
                "created": int(time.time() * 1000),
                "type": cmd["type"],
                "data": cmd.get("data", {}),
                "durable": {"aggregateID": sid, "seq": seq, "version": 1},
            }
            self._publish(event)
        elif op == "stall":
            key = f"{cmd['method']} {cmd['path']}"
            self._stalls[cmd["token"]] = {"key": key, "future": None}
        elif op == "release":
            token = cmd["token"]
            entry = self._stalls.get(token)
            if entry is not None and entry.get("future") is not None:
                entry["future"].set_result((cmd.get("status", 200), cmd.get("body")))
        elif op == "respond_once":
            key = f"{cmd['method']} {cmd['path']}"
            self._once.setdefault(key, []).append(
                {"status": cmd.get("status", 200), "body": cmd.get("body")}
            )
        elif op == "ignore_sigterm":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        elif op == "spawn_detached_child":
            pid = os.fork()
            if pid == 0:
                os.setsid()
                with contextlib.suppress(Exception):
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                while True:
                    time.sleep(3600)
            else:
                self._children.append(pid)
        elif op == "flood":
            stream = sys.stdout if cmd.get("stream") == "stdout" else sys.stderr
            stream.write("x" * int(cmd.get("bytes", 0)))
            stream.flush()
        elif op == "exit":
            os._exit(int(cmd.get("code", 0)))

    def _publish(self, event: dict) -> None:
        payload = f"data: {json.dumps(event)}\n\n".encode()
        for queue in self._sse_queues:
            queue.put_nowait(payload)

    # ---------------------------------------------------------------- HTTP

    def _record(self, method: str, path: str) -> None:
        key = f"{method} {path}"
        self._received[key] = self._received.get(key, 0) + 1

    async def _maybe_stall(self, method: str, path: str) -> tuple[int, dict | None] | None:
        key = f"{method} {path}"
        for token, entry in self._stalls.items():
            if entry.get("key") == key and entry.get("future") is None:
                fut: asyncio.Future = asyncio.get_running_loop().create_future()
                entry["future"] = fut
                return await fut
        return None

    def _maybe_once(self, method: str, path: str) -> tuple[int, dict | None] | None:
        key = f"{method} {path}"
        queued = self._once.get(key)
        if queued:
            item = queued.pop(0)
            return item["status"], item["body"]
        return None

    async def handle(self, method: str, path: str, body: bytes) -> tuple[int, bytes, dict]:
        self._record(method, path)
        parsed = json.loads(body) if body else None
        path_only, _, query = path.partition("?")

        stalled = await self._maybe_stall(method, path_only)
        if stalled is not None:
            status, resp = stalled
            return status, json.dumps(resp).encode() if resp is not None else b"", {}
        once = self._maybe_once(method, path_only)
        if once is not None:
            status, resp = once
            return status, json.dumps(resp).encode() if resp is not None else b"", {}

        if method == "GET" and path_only == "/_control/state":
            return (
                200,
                json.dumps(
                    {
                        "received": self._received,
                        "children": self._children,
                        "sessions": {sid: s.seq for sid, s in self.sessions.items()},
                    }
                ).encode(),
                {},
            )
        if method == "GET" and path_only in ("/api/config",):
            return 200, b"[]", {}
        if method == "GET" and path_only == "/api/model":
            return 200, b"{}", {}
        if method == "GET" and path_only.startswith("/api/agent/"):
            return 200, b"{}", {}
        if method == "POST" and path_only == "/api/session":
            self._session_counter += 1
            sid = f"ses_fake{self._session_counter}"
            self.sessions[sid] = _Session(sid)
            return 200, json.dumps({"data": {"id": sid}}).encode(), {}
        if method == "GET" and path_only.startswith("/api/session/") and path_only.count("/") == 3:
            sid = path_only.rsplit("/", 1)[-1]
            if sid in self.sessions:
                return 200, json.dumps({"data": {"id": sid}}).encode(), {}
            return 404, b"{}", {}
        if method == "POST" and path_only.endswith("/prompt"):
            sid = path_only.split("/")[3]
            session = self.sessions.setdefault(sid, _Session(sid))
            self._session_counter += 1
            input_id = f"msg_{self._session_counter}"
            session.inbox[input_id] = parsed or {}
            text = (parsed or {}).get("text", "")
            seq = session.next_seq()
            self._publish(
                {
                    "id": f"evt_{seq}",
                    "type": "session.inbox.enqueued",
                    "data": {
                        "inboxID": input_id,
                        "sessionID": sid,
                        "item": {"type": "user", "payload": {"text": text}, "delivery": "queue"},
                    },
                    "durable": {"aggregateID": sid, "seq": seq, "version": 1},
                }
            )
            return (
                200,
                json.dumps(
                    {
                        "data": {
                            "id": input_id,
                            "sessionID": sid,
                            "type": "user",
                            "delivery": "queue",
                            "payload": {"text": text},
                        }
                    }
                ).encode(),
                {},
            )
        if method == "POST" and "/interrupt" in path_only:
            return 200, json.dumps({"interrupted": True}).encode(), {}
        if method == "DELETE" and "/inbox/" in path_only:
            sid = path_only.split("/")[3]
            input_id = path_only.rsplit("/", 1)[-1]
            self.sessions.setdefault(sid, _Session(sid)).inbox.pop(input_id, None)
            return 204, b"", {}
        if method == "POST" and path_only.endswith("/wait"):
            return 204, b"", {}
        if method == "GET" and path_only.endswith("/inbox"):
            sid = path_only.split("/")[3]
            session = self.sessions.setdefault(sid, _Session(sid))
            return (
                200,
                json.dumps({"data": [{"id": iid} for iid in session.inbox]}).encode(),
                {},
            )
        return 404, b"{}", {}

    async def handle_event_stream(self, writer: asyncio.StreamWriter) -> None:
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\nTransfer-Encoding: chunked\r\n"
            b"Connection: keep-alive\r\n\r\n"
        )
        await writer.drain()
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._sse_queues.append(queue)
        try:
            while True:
                chunk = await queue.get()
                writer.write(f"{len(chunk):X}\r\n".encode() + chunk + b"\r\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            return
        finally:
            with contextlib.suppress(ValueError):
                self._sse_queues.remove(queue)


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, bytes] | None:
    line = await reader.readline()
    if not line:
        return None
    try:
        method, path, _version = line.decode().strip().split(" ", 2)
    except ValueError:
        return None
    headers: dict[str, str] = {}
    while True:
        header_line = await reader.readline()
        if header_line in (b"\r\n", b"\n", b""):
            break
        name, _, value = header_line.decode().partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(length) if length else b""
    return method, path, body


async def _serve_connection(server: Server, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            request = await _read_request(reader)
            if request is None:
                return
            method, path, body = request
            if method == "GET" and path == "/api/event":
                await server.handle_event_stream(writer)
                return
            status, resp_body, _extra_headers = await server.handle(method, path, body)
            reason = {200: "OK", 204: "No Content", 404: "Not Found"}.get(status, "OK")
            writer.write(
                f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(resp_body)}\r\nConnection: keep-alive\r\n\r\n".encode()
                + resp_body
            )
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        return
    finally:
        with contextlib.suppress(Exception):
            writer.close()


async def main(server: Server | None = None) -> None:
    server = Server() if server is None else server
    # `asyncio.start_server` already serves in the background once awaited — no separate
    # `serve_forever()` task needed. On the clean-stop signal (stdin EOF) we hard-exit
    # rather than go through graceful `Server.close()`/`wait_closed()`: a still-open SSE
    # connection's handler task would otherwise make `wait_closed()` hang forever, and this
    # fixture only needs a prompt, correct-exit-code stop, not a graceful drain.
    asyncio_server = await asyncio.start_server(
        lambda r, w: _serve_connection(server, r, w), HOST, 0
    )
    port = asyncio_server.sockets[0].getsockname()[1]
    sys.stdout.write(json.dumps({"url": f"http://{HOST}:{port}"}) + "\n")
    sys.stdout.flush()
    await server.read_commands()
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())

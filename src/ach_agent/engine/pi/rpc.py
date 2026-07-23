# SPDX-License-Identifier: Apache-2.0
"""JSONL RPC over a ``pi --mode rpc`` subprocess's stdin/stdout.

Framing is deliberately byte-oriented: split only on LF and strip a trailing CR.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from ach_agent.engine.pi.protocol import EV_EOF

if TYPE_CHECKING:
    from asyncio.subprocess import Process


class PiRpcError(Exception):
    """Malformed JSONL from pi, or the process ended mid-turn."""


class PiRpcClient:
    def __init__(self, proc: Process) -> None:
        self._proc = proc
        self._q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())

    async def send(self, cmd: dict[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            raise PiRpcError("pi stdin is closed")
        data = (json.dumps(cmd, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        stdin.write(data)
        await stdin.drain()

    async def recv(self) -> dict[str, Any]:
        item = await self._q.get()
        if item.get("type") == EV_EOF:
            return item
        if "__error__" in item:
            raise PiRpcError(str(item["__error__"]))
        return item

    async def _read_loop(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        buf = b""
        while True:
            chunk = await stdout.read(65536)
            if not chunk:
                if buf.strip():
                    await self._emit(buf)
                await self._q.put({"type": EV_EOF})
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if line.strip():
                    await self._emit(line)

    async def _emit(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("event is not a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            await self._q.put({"__error__": f"invalid JSONL from pi: {raw!r} ({exc})"})
            return
        await self._q.put(obj)

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        stdin = self._proc.stdin
        if stdin is not None:
            with contextlib.suppress(Exception):
                stdin.close()

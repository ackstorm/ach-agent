# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from ach_agent.engine.pi.rpc import PiRpcClient, PiRpcError


class _FakeStdout:
    """Feeds pre-baked bytes, then EOF, mimicking asyncio.StreamReader.read(n)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


class _FakeProc:
    def __init__(self, stdout: _FakeStdout) -> None:
        self.stdout = stdout
        self.stdin = _FakeStdin()


async def test_recv_parses_lf_framed_json_across_chunk_boundaries() -> None:
    proc = _FakeProc(_FakeStdout([b'{"type":"a","x":', b'1}\r\n{"type":"b"}\n']))
    client = PiRpcClient(proc)  # type: ignore[arg-type]
    assert await client.recv() == {"type": "a", "x": 1}
    assert await client.recv() == {"type": "b"}
    eof = await client.recv()
    assert eof["type"] == "__eof__"
    await client.close()


async def test_send_writes_one_lf_terminated_line() -> None:
    proc = _FakeProc(_FakeStdout([]))
    client = PiRpcClient(proc)  # type: ignore[arg-type]
    await client.send({"type": "prompt", "message": "hi"})
    assert proc.stdin.written == [b'{"type":"prompt","message":"hi"}\n']
    await client.close()


async def test_invalid_json_line_raises() -> None:
    proc = _FakeProc(_FakeStdout([b"not json\n"]))
    client = PiRpcClient(proc)  # type: ignore[arg-type]
    with pytest.raises(PiRpcError):
        await client.recv()
    await client.close()

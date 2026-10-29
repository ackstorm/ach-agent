# SPDX-License-Identifier: MIT
"""Spawns the Task 1 spike's proven fake model+MCP fixture (tests/fixtures/
fake-facades.mjs, via tests/engine/fixtures/fake_facades_server.mjs) so the REAL opencode2
binary can run against a scripted, loopback backend instead of an unreachable HTTPS
facade — review finding evidence requirement: "the existing fake facade was available for
those required focused cases."
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

_SERVER_SCRIPT = Path(__file__).parent / "fixtures" / "fake_facades_server.mjs"


@dataclass
class FakeFacades:
    model_url: str
    mcp_url: str
    proc: asyncio.subprocess.Process

    async def control(self, command: dict[str, object]) -> dict[str, object]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(command).encode() + b"\n")
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=10.0)
        return json.loads(line)


@asynccontextmanager
async def fake_facades(scenario: str = "text") -> AsyncIterator[FakeFacades]:
    proc = await asyncio.create_subprocess_exec(
        "node",
        str(_SERVER_SCRIPT),
        scenario,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=8 * 1024 * 1024,
    )
    assert proc.stdout is not None
    try:
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=10.0)
        urls = json.loads(line)
        yield FakeFacades(model_url=urls["modelUrl"], mcp_url=urls["mcpUrl"], proc=proc)
    finally:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()

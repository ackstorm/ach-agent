# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
from typing import Any

import httpx

from ach_agent.engine.a2a_egress import A2AEgressFacade, ToolSpec


async def _noop(prompt: str) -> dict[str, Any]:
    return {"ok": True}


async def test_facade_starts_on_loopback_and_stops_clean() -> None:
    tools = [ToolSpec(name="a2a_peer", description="call peer", handler=_noop)]
    facade = A2AEgressFacade(tools)
    url = await facade.start()
    try:
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/mcp", url)
        # The streamable-http MCP endpoint is live (GET without a session -> 4xx, not a refusal).
        async with httpx.AsyncClient() as c:
            resp = await c.get(url)
        assert resp.status_code < 500
    finally:
        await facade.stop()


async def test_facade_with_no_tools_still_starts() -> None:
    facade = A2AEgressFacade([])
    url = await facade.start()
    await facade.stop()
    assert url.endswith("/mcp")

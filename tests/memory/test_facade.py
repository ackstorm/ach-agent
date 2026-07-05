# SPDX-License-Identifier: Apache-2.0
import socket

import pytest

from ach_agent.memory import hindsight as hs
from ach_agent.memory.facade import MemoryFacade


@pytest.mark.asyncio
async def test_invoke_injects_bank_id_and_maps_tool(monkeypatch):
    seen = {}

    async def fake_call(endpoint, secret, tool, args):
        seen["endpoint"] = endpoint
        seen["secret"] = secret
        seen["tool"] = tool
        seen["args"] = args
        return "RESULT"

    monkeypatch.setattr("ach_agent.memory.facade.call_hindsight", fake_call)
    f = MemoryFacade("https://hs/mcp", "sekret", "bank-1")
    out = await f._invoke(hs.HINDSIGHT_RECALL, {"query": "q", "tags": ["repo:x"]})
    assert out == "RESULT"
    assert seen["tool"] == "hindsight_recall"
    assert seen["args"] == {"bank_id": "bank-1", "query": "q", "tags": ["repo:x"]}
    assert seen["secret"] == "sekret"


@pytest.mark.asyncio
async def test_registers_exactly_four_tools():
    f = MemoryFacade("https://hs/mcp", "sekret", "bank-1")
    tools = await f._mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "memory_get_mental_model",
        "memory_recall",
        "memory_reflect",
        "memory_retain",
    ]


@pytest.mark.asyncio
async def test_start_returns_reachable_url_and_stop_tears_down():
    f = MemoryFacade("https://hs/mcp", "sekret", "bank-1")
    url = await f.start()
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
    port = int(url.split(":")[2].split("/")[0])
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass  # connect succeeds → listening
    await f.stop()

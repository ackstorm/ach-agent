# SPDX-License-Identifier: Apache-2.0
"""Tests for select_memory_wiring_async — hindsight-only (2-tuple) + codemem/None pass-through.

Verifies that:
- codemem: select_memory_wiring_async returns ([], "") — codemem is NOT handled here (boot-time).
- hindsight: calls prepare_memory; returns ([endpoint], prompt) 2-tuple when reachable.
- None: returns ([], "").
"""

from __future__ import annotations

import pytest

from ach_agent.config.schema import CodememMemory, CodememParams, HindsightMemory, HindsightParams


async def test_codemem_type_returns_empty_2tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """codemem memory cfg must NOT invoke prepare_memory and must return ([], "")."""
    from ach_agent import main as m

    called = {"prepare_memory": False}

    async def _boom(_cfg: object) -> tuple[bool, str]:
        called["prepare_memory"] = True
        return (True, "## Memory\nx")

    monkeypatch.setattr(m, "prepare_memory", _boom)

    # CodememParams() is now valid — db_path defaults to None, project defaults to "ach-agent"
    params = CodememParams(db_path="/var/lib/codemem/a.db")
    cfg = CodememMemory(type="codemem", codemem=params)
    mcp_servers, memory_prompt = await m.select_memory_wiring_async(cfg, "http://facade/mcp")

    assert mcp_servers == []
    assert memory_prompt == ""
    assert called["prepare_memory"] is False


async def test_hindsight_type_uses_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """hindsight memory cfg must call prepare_memory and return (facade_url_list, prompt) 2-tuple.

    mcp_servers carries the harness FACADE url, never the raw hindsight endpoint.
    """
    from ach_agent import main as m

    async def _ok(_cfg: object) -> tuple[bool, str]:
        return (True, "## Memory\nx")

    monkeypatch.setattr(m, "prepare_memory", _ok)

    cfg = HindsightMemory(type="hindsight", hindsight=HindsightParams(endpoint="http://mem:8080"))
    mcp_servers, memory_prompt = await m.select_memory_wiring_async(cfg, "http://facade/mcp")

    assert mcp_servers == ["http://facade/mcp"]
    assert memory_prompt == "## Memory\nx"


async def test_none_memory_cfg() -> None:
    """None memory config must return ([], "") 2-tuple."""
    from ach_agent import main as m

    mcp_servers, memory_prompt = await m.select_memory_wiring_async(None, None)

    assert mcp_servers == []
    assert memory_prompt == ""

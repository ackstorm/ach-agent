# SPDX-License-Identifier: Apache-2.0
"""Tests for select_memory_wiring_async — branching by memory backend type.

Verifies that:
- codemem: does NOT call prepare_memory; returns ([], "", db_path) when binary is on PATH.
- hindsight: calls prepare_memory; returns ([endpoint], prompt, "") when reachable.
- None: returns ([], "", "").
"""

from __future__ import annotations

import pytest
from ach_agent.config.schema import CodememMemory, HindsightMemory


async def test_codemem_type_skips_hindsight_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """codemem memory cfg must NOT invoke prepare_memory and must return db_path."""
    from ach_agent import main as m

    called = {"prepare_memory": False}

    async def _boom(_cfg: object) -> tuple[bool, str]:
        called["prepare_memory"] = True
        return (True, "## Memory\nx")

    monkeypatch.setattr("ach_agent.memory.adapter.prepare_memory", _boom)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    cfg = CodememMemory(type="codemem", dbPath="/var/lib/codemem/a.db")
    mcp_servers, memory_prompt, codemem_db = await m.select_memory_wiring_async(cfg)

    assert mcp_servers == []
    assert memory_prompt == ""
    assert codemem_db == "/var/lib/codemem/a.db"
    assert called["prepare_memory"] is False


async def test_hindsight_type_uses_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """hindsight memory cfg must call prepare_memory and return endpoint + prompt."""
    from ach_agent import main as m

    async def _ok(_cfg: object) -> tuple[bool, str]:
        return (True, "## Memory\nx")

    monkeypatch.setattr("ach_agent.memory.adapter.prepare_memory", _ok)

    cfg = HindsightMemory(type="hindsight", endpoint="http://mem:8080")
    mcp_servers, memory_prompt, codemem_db = await m.select_memory_wiring_async(cfg)

    assert mcp_servers == ["http://mem:8080"]
    assert memory_prompt == "## Memory\nx"
    assert codemem_db == ""


async def test_none_memory_cfg() -> None:
    """None memory config must return empty tuple."""
    from ach_agent import main as m

    mcp_servers, memory_prompt, codemem_db = await m.select_memory_wiring_async(None)

    assert mcp_servers == []
    assert memory_prompt == ""
    assert codemem_db == ""

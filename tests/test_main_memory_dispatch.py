# SPDX-License-Identifier: Apache-2.0
"""Tests for select_memory_wiring_async — ach-memory (2-tuple) + codemem/None pass-through.

Verifies that:
- codemem: select_memory_wiring_async returns ([], "") — codemem is NOT handled here (boot-time).
- ach-memory: calls prepare_ach_memory; returns ([facade_url], prompt) when reachable.
- None: returns ([], "").
"""

from __future__ import annotations

import pytest

from ach_agent.config.schema import AchMemoryMemory, CodememMemory, CodememParams


async def test_codemem_type_returns_empty_2tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    """codemem memory cfg must NOT invoke prepare_ach_memory and must return ([], "")."""
    import ach_agent.boot.engine_runner as engine_runner_mod

    called = {"prepare": False}

    async def _boom(_cfg: object, _project: str) -> tuple[bool, str]:
        called["prepare"] = True
        return (True, "## Memory\nx")

    monkeypatch.setattr(engine_runner_mod, "prepare_ach_memory", _boom)

    # CodememParams() is now valid — db_path defaults to None, project defaults to "ach-agent"
    params = CodememParams(db_path="/var/lib/codemem/a.db")
    cfg = CodememMemory(type="codemem", codemem=params)
    mcp_servers, memory_prompt = await engine_runner_mod.select_memory_wiring_async(
        cfg, "http://facade/mcp"
    )

    assert mcp_servers == []
    assert memory_prompt == ""
    assert called["prepare"] is False


async def test_ach_memory_type_uses_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """ach-memory cfg must call prepare_ach_memory and return (facade_url_list, prompt).

    mcp_servers carries the harness FACADE url, never the raw ach-memory endpoint.
    """
    import ach_agent.boot.engine_runner as engine_runner_mod

    async def _ok(_cfg: object, _project: str, _headers: dict[str, str]) -> tuple[bool, str]:
        return (True, "## Memory\nx")

    monkeypatch.setattr(engine_runner_mod, "prepare_ach_memory", _ok)

    cfg = AchMemoryMemory.model_validate(
        {"type": "ach-memory", "achMemory": {"endpoint": "http://mem:8000"}}
    )
    mcp_servers, memory_prompt = await engine_runner_mod.select_memory_wiring_async(
        cfg, "http://facade/mcp"
    )

    assert mcp_servers == ["http://facade/mcp"]
    assert memory_prompt == "## Memory\nx"


async def test_none_memory_cfg() -> None:
    """None memory config must return ([], "") 2-tuple."""
    import ach_agent.boot.engine_runner as engine_runner_mod

    mcp_servers, memory_prompt = await engine_runner_mod.select_memory_wiring_async(None, None)

    assert mcp_servers == []
    assert memory_prompt == ""

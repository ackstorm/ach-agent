# SPDX-License-Identifier: Apache-2.0
"""Integration test: codemem memory config → select_memory_wiring_async → write_opencode_config.

Proves that a `codemem` memory config flows through the full config→opencode.json chain
in-process, without launching a live opencode process or calling any model.

WAL note (verified fact):
    codemem 0.37.1 uses SQLite WAL by default (verified: PRAGMA journal_mode='wal').
    Concurrent stdio MCP children sharing the same {repo}.db get N readers + 1 writer;
    writes serialize. No 'database is locked' under the model-managed (low) write rate,
    so no per-repo pool affinity is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ach_agent.config.schema import CodememMemory, CodememParams, HindsightMemory, HindsightParams
from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config
from ach_agent.main import select_memory_wiring_async

pytestmark = pytest.mark.integration  # opt-in; runs in normal suite unless deselected


def _opencode_json(home: Path) -> dict:  # type: ignore[type-arg]
    return json.loads((home / ".config" / "opencode" / "opencode.json").read_text())


async def test_codemem_config_flows_into_opencode_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codemem present on PATH → db_path propagates all the way into opencode.json's mcp block."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    params = CodememParams(db_path="/var/lib/codemem/agent.db", project="ach-agent")
    cfg = CodememMemory(type="codemem", codemem=params)
    mcp_servers, memory_prompt, codemem_db, codemem_project = await select_memory_wiring_async(cfg)

    # Wiring: no remote MCP, no memory prompt, db_path resolved
    assert mcp_servers == []
    assert memory_prompt == ""
    assert codemem_db == "/var/lib/codemem/agent.db"

    # Feed resolved wiring into write_opencode_config exactly as boot/engine_runner does
    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        mcp_servers=mcp_servers,
        codemem_db_path=codemem_db,
        codemem_project=codemem_project,
    )
    write_opencode_config(tmp_path, engine_cfg)

    mcp = _opencode_json(tmp_path)["mcp"]
    assert mcp["codemem"] == {
        "type": "local",
        "command": ["codemem", "mcp", "--db-path", "/var/lib/codemem/agent.db"],
        "enabled": True,
        "environment": {
            "CODEMEM_VIEWER": "0",
            "CODEMEM_VIEWER_AUTO": "0",
            "CODEMEM_PROJECT": "ach-agent",
        },
    }


async def test_codemem_absent_from_path_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codemem not on PATH → fail-open: codemem_db_path '' → no codemem entry in opencode.json."""
    monkeypatch.setattr("shutil.which", lambda name: None)

    params = CodememParams(db_path="/var/lib/codemem/agent.db", project="ach-agent")
    cfg = CodememMemory(type="codemem", codemem=params)
    _, _, codemem_db, codemem_project = await select_memory_wiring_async(cfg)

    assert codemem_db == ""

    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        codemem_db_path=codemem_db,
        codemem_project=codemem_project,
    )
    write_opencode_config(tmp_path, engine_cfg)

    assert "codemem" not in _opencode_json(tmp_path).get("mcp", {})


async def test_hindsight_path_produces_no_codemem_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """hindsight config → remote mcp server + prompt, and NO codemem entry in opencode.json.

    Remote MCP entries are keyed memory-{i} with shape {type:remote, url:..., enabled:True}
    (opencode 1.16 schema, verified in lifecycle.py line ~239).
    """

    async def _ok(_cfg: object) -> tuple[bool, str]:
        return (True, "## Memory\nx")

    monkeypatch.setattr("ach_agent.memory.adapter.prepare_memory", _ok)

    cfg = HindsightMemory(type="hindsight", hindsight=HindsightParams(endpoint="http://mem:8080"))
    mcp_servers, memory_prompt, codemem_db, codemem_project = await select_memory_wiring_async(cfg)

    assert mcp_servers == ["http://mem:8080"]
    assert memory_prompt == "## Memory\nx"
    assert codemem_db == ""

    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        mcp_servers=mcp_servers,
        codemem_db_path=codemem_db,
        codemem_project=codemem_project,
    )
    write_opencode_config(tmp_path, engine_cfg)

    oc_mcp = _opencode_json(tmp_path).get("mcp", {})

    # No codemem entry for hindsight path
    assert "codemem" not in oc_mcp

    # Hindsight endpoint registered as memory-0 with the correct remote shape
    assert oc_mcp.get("memory-0") == {
        "type": "remote",
        "url": "http://mem:8080",
        "enabled": True,
    }

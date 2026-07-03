# SPDX-License-Identifier: Apache-2.0
"""Integration test: resolve_codemem_wiring → EngineConfig → write_opencode_config.

Proves that a `codemem` memory config flows through the full
config → resolve_codemem_wiring → opencode.json chain in-process, without launching
a live opencode process or calling any model.

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

from ach_agent.config.schema import AgentConfig, HindsightMemory, HindsightParams
from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config
from ach_agent.main import resolve_codemem_wiring, select_memory_wiring_async

pytestmark = pytest.mark.integration  # opt-in; runs in normal suite unless deselected

# ---------------------------------------------------------------------------
# Minimal valid AgentConfig dict factory
# ---------------------------------------------------------------------------

_BASE_CFG: dict = {
    "schemaVersion": "1",
    "agent": {"name": "a"},
    "model": {"name": "openai.gpt-5", "type": "openai"},
    "capability": {"ach": {"baseUrl": "https://ach.example.com"}},
}


def _cfg(memory: dict | None = None, persistence: dict | None = None) -> AgentConfig:
    raw: dict = dict(_BASE_CFG)
    if memory is not None:
        raw["memory"] = memory
    if persistence is not None:
        raw["persistence"] = persistence
    return AgentConfig.model_validate(raw)


def _opencode_json(config_path: Path) -> dict:  # type: ignore[type-arg]
    return json.loads(config_path.read_text())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_codemem_config_flows_into_opencode_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codemem present on PATH + explicit dbPath → propagates into opencode.json mcp block."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    cfg = _cfg(
        memory={"type": "codemem", "codemem": {"dbPath": "/var/lib/codemem/agent.db", "project": "ach-agent"}}
    )
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == "/var/lib/codemem/agent.db"
    assert project == "ach-agent"

    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        codemem_db_path=db_path,
        codemem_project=project,
    )
    cfg_path = write_opencode_config(tmp_path, engine_cfg, "k1")

    mcp = _opencode_json(cfg_path)["mcp"]
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


async def test_codemem_derived_db_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codemem with no dbPath + persistence.enabled → db derives to <mountPath>/state/codemem.db."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    cfg = _cfg(
        memory={"type": "codemem", "codemem": {}},
        persistence={"enabled": True, "mountPath": "/var/lib/ach-agent"},
    )
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == "/var/lib/ach-agent/state/codemem.db"
    assert project == "ach-agent"


async def test_codemem_absent_from_path_degrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codemem not on PATH → fail-open: resolve returns ("","") → no codemem entry in opencode.json."""
    monkeypatch.setattr("shutil.which", lambda name: None)

    cfg = _cfg(memory={"type": "codemem", "codemem": {"dbPath": "/var/lib/codemem/agent.db"}})
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == ""

    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        codemem_db_path=db_path,
        codemem_project=project,
    )
    cfg_path = write_opencode_config(tmp_path, engine_cfg, "k1")

    assert "codemem" not in _opencode_json(cfg_path).get("mcp", {})


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

    cfg_mem = HindsightMemory(type="hindsight", hindsight=HindsightParams(endpoint="http://mem:8080"))
    mcp_servers, memory_prompt = await select_memory_wiring_async(cfg_mem)

    assert mcp_servers == ["http://mem:8080"]
    assert memory_prompt == "## Memory\nx"

    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        mcp_servers=mcp_servers,
        codemem_db_path="",
        codemem_project="",
    )
    cfg_path = write_opencode_config(tmp_path, engine_cfg, "k1")

    oc_mcp = _opencode_json(cfg_path).get("mcp", {})

    # No codemem entry for hindsight path
    assert "codemem" not in oc_mcp

    # Hindsight endpoint registered as memory-0 with the correct remote shape
    assert oc_mcp.get("memory-0") == {
        "type": "remote",
        "url": "http://mem:8080",
        "enabled": True,
    }

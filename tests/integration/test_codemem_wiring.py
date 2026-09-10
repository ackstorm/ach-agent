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

from ach_agent.boot.engine_runner import select_memory_wiring_async
from ach_agent.config.schema import AchMemoryMemory, AgentConfig
from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config
from ach_agent.main import resolve_codemem_wiring

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
        memory={
            "type": "codemem",
            "codemem": {"dbPath": "/var/lib/codemem/agent.db", "project": "ach-agent"},
        }
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


async def test_codemem_derived_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


async def test_ach_memory_path_produces_no_codemem_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ach-memory config → remote mcp server + prompt, and NO codemem entry in opencode.json.

    Remote MCP entries are keyed by what the server IS, with shape
    {type:remote, url:..., enabled:True} (opencode 1.16 schema, verified in lifecycle.py).
    """

    async def _ok(_cfg: object, _project: str, _headers: dict[str, str]) -> tuple[bool, str]:
        return (True, "## Memory\nx")

    import ach_agent.boot.engine_runner as engine_runner_mod

    monkeypatch.setattr(engine_runner_mod, "prepare_ach_memory", _ok)

    facade_url = "http://127.0.0.1:7/mcp"
    cfg_mem = AchMemoryMemory.model_validate(
        {"type": "ach-memory", "achMemory": {"endpoint": "http://mem:8000"}}
    )
    mcp_servers, memory_prompt = await select_memory_wiring_async(cfg_mem, facade_url)

    # mcp_servers carries the harness FACADE url, never the raw ach-memory endpoint.
    assert mcp_servers == {"memory": facade_url}
    assert memory_prompt == "## Memory\nx"

    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        mcp_servers=mcp_servers,
        codemem_db_path="",
        codemem_project="",
    )
    cfg_path = write_opencode_config(tmp_path, engine_cfg, "k1")

    oc_mcp = _opencode_json(cfg_path).get("mcp", {})

    # No codemem entry for the ach-memory path
    assert "codemem" not in oc_mcp

    # Facade url registered as `memory` with the correct remote shape. Named, not
    # enumerated: the id reaches the model, so it has to say what the server IS.
    assert oc_mcp.get("memory") == {
        "type": "remote",
        "url": facade_url,
        "enabled": True,
    }


def test_a_proxied_server_cannot_take_the_memory_facades_name(tmp_path: Path) -> None:
    """The two MCP key spaces are not disjoint: an ACH environment may list a server called
    `memory`, and one of the two is a containment boundary. Losing a third-party server to a
    name clash is recoverable; serving an UNPINNED ach-memory under the name the system prompt
    tells the agent to use is not — it would hand the model a `retain` that takes project_slug.
    """
    engine_cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        mcp_servers={"memory": "http://127.0.0.1:7/mcp"},
        mcp_local_urls={"memory": "http://127.0.0.1:8/proxied"},
        codemem_db_path="",
        codemem_project="",
    )
    oc_mcp = _opencode_json(write_opencode_config(tmp_path, engine_cfg, "k1")).get("mcp", {})

    assert oc_mcp["memory"]["url"] == "http://127.0.0.1:7/mcp"

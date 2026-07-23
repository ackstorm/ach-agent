# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

import pytest

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.mcp_json import build_mcp_json


def test_facades_and_proxy_are_remote_loopback_entries(monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_secret")
    cfg = EngineConfig(
        mcp_servers=["http://127.0.0.1:7001/mcp", "http://127.0.0.1:7002/mcp"],
        mcp_local_urls={"gitlab": "http://127.0.0.1:7003/mcp/gitlab"},
        exclude_tools=["dangerous_tool"],
    )
    doc = build_mcp_json(cfg)
    blob = json.dumps(doc)
    assert "ek_secret" not in blob
    servers = doc["mcpServers"]
    assert servers["facade-0"] == {"url": "http://127.0.0.1:7001/mcp"}
    assert servers["gitlab"] == {"url": "http://127.0.0.1:7003/mcp/gitlab"}
    assert doc["settings"]["directTools"] is True
    assert doc["settings"]["excludeTools"] == ["dangerous_tool"]
    assert doc["settings"]["sampling"] is False
    assert doc["settings"]["elicitation"] is False


def test_codemem_is_local_stdio() -> None:
    cfg = EngineConfig(codemem_db_path="/data/mem.db", codemem_project="proj")
    servers = build_mcp_json(cfg)["mcpServers"]
    assert servers["codemem"]["command"] == "codemem"
    assert servers["codemem"]["args"] == ["mcp", "--db-path", "/data/mem.db"]
    assert servers["codemem"]["env"]["CODEMEM_PROJECT"] == "proj"


def test_passthrough_opencode_local_entry_converted_to_pi_shape() -> None:
    cfg = EngineConfig(
        extra_mcp_servers={
            "fs": {
                "type": "local",
                "command": ["docker", "run", "mcp/fs"],
                "enabled": True,
                "environment": {"K": "v"},
            },
        }
    )
    servers = build_mcp_json(cfg)["mcpServers"]
    assert servers["fs"] == {
        "command": "docker",
        "args": ["run", "mcp/fs"],
        "env": {"K": "v"},
    }


def test_passthrough_ek_credential_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_materialized_secret")
    cfg = EngineConfig(
        extra_mcp_servers={
            "private": {
                "type": "remote",
                "url": "http://127.0.0.1:7004/mcp",
                "headers": {"Authorization": "Bearer ek_materialized_secret"},
            }
        }
    )
    with pytest.raises(ValueError, match="materialized ek_"):
        build_mcp_json(cfg)


def test_passthrough_unrelated_credential_is_preserved() -> None:
    cfg = EngineConfig(
        extra_mcp_servers={
            "private": {
                "type": "remote",
                "url": "http://127.0.0.1:7004/mcp",
                "headers": {"Authorization": "Bearer unrelated_secret"},
            }
        }
    )
    servers = build_mcp_json(cfg)["mcpServers"]
    assert servers["private"]["headers"]["Authorization"] == "Bearer unrelated_secret"

# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config


def _read_oc(config_path: Path) -> dict:
    return json.loads(config_path.read_text())


def test_codemem_local_entry_written(tmp_path):
    cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        codemem_db_path="/var/lib/codemem/a.db",
        codemem_project="ach-agent",
    )
    cfg_path = write_opencode_config(tmp_path, cfg, "k1")
    mcp = _read_oc(cfg_path)["mcp"]
    assert mcp["codemem"] == {
        "type": "local",
        "command": ["codemem", "mcp", "--db-path", "/var/lib/codemem/a.db"],
        "enabled": True,
        "environment": {
            "CODEMEM_VIEWER": "0",
            "CODEMEM_VIEWER_AUTO": "0",
            "CODEMEM_PROJECT": "ach-agent",
        },
    }


def test_no_codemem_entry_when_unset(tmp_path):
    cfg = EngineConfig(model_base_url="http://127.0.0.1:9/v1")
    cfg_path = write_opencode_config(tmp_path, cfg, "k1")
    oc = _read_oc(cfg_path)
    assert "codemem" not in oc.get("mcp", {})

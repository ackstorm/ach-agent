# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config


def _read_oc(home: Path) -> dict:
    return json.loads((home / ".config" / "opencode" / "opencode.json").read_text())


def test_codemem_local_entry_written(tmp_path):
    cfg = EngineConfig(
        model_base_url="http://127.0.0.1:9/v1",
        codemem_db_path="/var/lib/codemem/a.db",
        codemem_project="ach-agent",
    )
    write_opencode_config(tmp_path, cfg)
    mcp = _read_oc(tmp_path)["mcp"]
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
    write_opencode_config(tmp_path, cfg)
    oc = _read_oc(tmp_path)
    assert "codemem" not in oc.get("mcp", {})

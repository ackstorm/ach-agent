from __future__ import annotations

import json
from pathlib import Path

from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config


def test_write_config_includes_passthrough_mcp(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    cfg = EngineConfig(
        home=str(home),
        work_dir=str(home / "workspace"),
        model="m",
        model_type="openai",
        model_base_url="http://127.0.0.1:1/v1",
        extra_mcp_servers={
            "fs": {"type": "local", "command": ["docker", "run"], "enabled": True},
            "other": {"type": "remote", "url": "https://x/mcp", "enabled": True},
        },
    )
    path = write_opencode_config(home, cfg, "sess-key")
    written = json.loads(Path(path).read_text())
    assert written["mcp"]["fs"] == {"type": "local", "command": ["docker", "run"], "enabled": True}
    assert written["mcp"]["other"] == {"type": "remote", "url": "https://x/mcp", "enabled": True}

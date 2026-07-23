# SPDX-License-Identifier: Apache-2.0
"""Build the vendored pi-mcp-adapter's mcp.json."""

from __future__ import annotations

from typing import Any

from ach_agent.engine.base.driver import EngineConfig


def _passthrough_to_pi(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert an opencode-shaped passthrough entry into Pi adapter shape."""
    if entry.get("type") == "local":
        command = list(entry.get("command", []))
        out: dict[str, Any] = {
            "command": command[0] if command else "",
            "args": command[1:],
        }
        if entry.get("environment"):
            out["env"] = entry["environment"]
        return out
    out = {"url": entry.get("url", "")}
    if entry.get("headers"):
        out["headers"] = entry["headers"]
    return out


def build_mcp_json(cfg: EngineConfig) -> dict[str, Any]:
    servers: dict[str, dict[str, Any]] = {}
    for index, url in enumerate(cfg.mcp_servers):
        servers[f"facade-{index}"] = {"url": url}
    for server_id, url in cfg.mcp_local_urls.items():
        servers[server_id] = {"url": url}
    for name, entry in cfg.extra_mcp_servers.items():
        servers[name] = _passthrough_to_pi(entry)  # type: ignore[arg-type]
    if cfg.codemem_db_path:
        servers["codemem"] = {
            "command": "codemem",
            "args": ["mcp", "--db-path", cfg.codemem_db_path],
            "env": {
                "CODEMEM_VIEWER": "0",
                "CODEMEM_VIEWER_AUTO": "0",
                "CODEMEM_PROJECT": cfg.codemem_project,
            },
        }
    return {
        "mcpServers": servers,
        "settings": {
            "directTools": True,
            "excludeTools": list(cfg.exclude_tools),
            "sampling": False,
            "elicitation": False,
        },
    }

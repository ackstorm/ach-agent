# SPDX-License-Identifier: Apache-2.0
"""Build the vendored pi-mcp-adapter's mcp.json."""

from __future__ import annotations

import os
import re
from typing import Any

from ach_agent.engine.base.driver import EngineConfig

_EK_VALUE_MARKER = re.compile(r"(?<![A-Za-z0-9])ek_[A-Za-z0-9_.:/=-]+")


def _materialized_ek_values() -> tuple[str, ...]:
    return tuple(value for name in ("ACH_TOKEN", "ACH_API_KEY") if (value := os.environ.get(name)))


def _contains_materialized_ek(value: Any, secrets: tuple[str, ...]) -> bool:
    if isinstance(value, str):
        return bool(_EK_VALUE_MARKER.search(value)) or any(secret in value for secret in secrets)
    if isinstance(value, dict):
        return any(_contains_materialized_ek(item, secrets) for item in value.values())
    if isinstance(value, list):
        return any(_contains_materialized_ek(item, secrets) for item in value)
    return False


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
        servers[name] = _passthrough_to_pi(entry)
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
    if _contains_materialized_ek(servers, _materialized_ek_values()):
        raise ValueError("refusing to write materialized ek_ credential to Pi mcp.json")
    return {
        "mcpServers": servers,
        "settings": {
            "directTools": True,
            "excludeTools": list(cfg.exclude_tools),
            "sampling": False,
            "elicitation": False,
        },
    }

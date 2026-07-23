# SPDX-License-Identifier: Apache-2.0
"""Pi settings.json and clean-slate subprocess environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ach_agent.engine.base.driver import EngineConfig

_PI_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "SHELL", "USER", "LOGNAME", "HOSTNAME", "LANG", "LANGUAGE", "TERM", "TZ"}
)


def build_pi_settings(skills_dir: Path, mcp_adapter_path: str) -> dict[str, Any]:
    """Build Pi's settings with headless project trust and vendored adapter."""
    return {
        "skills": [str(skills_dir)],
        "defaultProjectTrust": "always",
        "packages": [mcp_adapter_path] if mcp_adapter_path else [],
    }


def build_pi_env(agent_dir: Path, cfg: EngineConfig) -> dict[str, str]:
    """Build a clean environment; only explicitly named extras are forwarded."""
    env: dict[str, str] = {
        name: os.environ[name] for name in _PI_ENV_ALLOWLIST if name in os.environ
    }
    for name in cfg.forward_env:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    env["HOME"] = str(agent_dir)
    env["TMPDIR"] = "/tmp"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    return env

# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.config import build_pi_env, build_pi_settings


def test_settings_defaults_trust_always_and_wires_skills_and_adapter() -> None:
    settings = build_pi_settings(Path("/home/skills"), "/vendor/pi-mcp-adapter")
    assert settings["defaultProjectTrust"] == "always"
    assert settings["skills"] == ["/home/skills"]
    assert settings["packages"] == ["/vendor/pi-mcp-adapter"]


def test_env_is_clean_slate_and_never_carries_ek(monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_CA", "/etc/ca.pem")
    cfg = EngineConfig(forward_env=["MY_CA"])
    env = build_pi_env(Path("/home/agent/pi/k1"), cfg)
    assert "ACH_TOKEN" not in env and "ek_secret" not in env.values()
    assert env["PATH"] == "/usr/bin"
    assert env["MY_CA"] == "/etc/ca.pem"
    assert env["PI_CODING_AGENT_DIR"] == "/home/agent/pi/k1"
    assert env["HOME"] == "/home/agent/pi/k1" and env["GIT_TERMINAL_PROMPT"] == "0"

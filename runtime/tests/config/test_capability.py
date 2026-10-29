# SPDX-License-Identifier: MIT
"""workspace-v3 capability block: server selection, headers, memory rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ach_runtime.config import ConfigError, parse_config, public_config
from ach_runtime.config.canonical import compute_config_version

FIXTURES = Path(__file__).parent / "fixtures"


def _wire(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _parse(wire: dict):
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def test_standalone_fixture_parses_and_projects_model():
    cfg = _parse(_wire("standalone-runtime.json"))
    assert cfg.schema_version == "workspace-v3"
    assert cfg.capability.server == "standalone"
    assert cfg.model.name == "anthropic/claude-sonnet-5.5"
    assert cfg.model.type == "openai"
    assert cfg.identity_environment == cfg.agent.namespace


def test_ach_fixture_reads_environment_from_ach_server():
    cfg = _parse(_wire("pr-review-runtime.json"))
    assert cfg.capability.server == "ach"
    assert cfg.identity_environment == cfg.capability.ach.environment
    assert cfg.model == cfg.capability.ach.model


@pytest.mark.parametrize("server,present", [("standalone", "ach"), ("ach", "standalone")])
def test_server_requires_its_block_and_forbids_the_other(server, present):
    wire = _wire("standalone-runtime.json" if server == "standalone" else "pr-review-runtime.json")
    other = _wire("pr-review-runtime.json" if server == "standalone" else "standalone-runtime.json")
    wire["capability"][present] = other["capability"][present]
    with pytest.raises(ConfigError, match="capability"):
        _parse(wire)


@pytest.mark.parametrize("name", ["Host", "x-ach-key", "X-ACH-Agent", "traceparent", "langfuse_session", "Connection"])
def test_reserved_header_names_are_rejected(name):
    wire = _wire("standalone-runtime.json")
    wire["capability"]["standalone"]["model"]["headers"] = {name: {"env": "ACH_AGENT_MODEL_H0"}}
    with pytest.raises(ConfigError, match="reserved"):
        _parse(wire)


def test_duplicate_header_names_differing_in_case_are_rejected():
    wire = _wire("standalone-runtime.json")
    wire["capability"]["standalone"]["mcpServers"][0]["headers"] = {
        "X-API-KEY": {"env": "ACH_AGENT_MCP_SERPER_H0"}, "x-api-key": {"env": "ACH_AGENT_MCP_SERPER_H1"}}
    with pytest.raises(ConfigError, match="duplicate"):
        _parse(wire)


def test_standalone_rejects_ach_memory_auth():
    wire = _wire("standalone-runtime.json")
    wire["memory"] = {"type": "ach-memory", "achMemory": {"mcpServerId": "serper", "auth": {"type": "ach"}}}
    with pytest.raises(ConfigError, match="auth.type"):
        _parse(wire)


def test_standalone_memory_server_must_be_a_standalone_mcp_server():
    wire = _wire("standalone-runtime.json")
    wire["memory"] = {"type": "ach-memory", "achMemory": {"mcpServerId": "nope"}}
    with pytest.raises(ConfigError, match="mcpServerId"):
        _parse(wire)


def test_standalone_rejects_skill_exclusions():
    wire = _wire("standalone-runtime.json")
    wire["capability"]["filter"] = {"exclude": {"skills": ["x"], "tools": [], "mcpServers": []}}
    with pytest.raises(ConfigError, match="skills"):
        _parse(wire)


def test_public_projection_never_carries_upstreams_or_headers():
    cfg = _parse(_wire("standalone-runtime.json"))
    dumped = json.dumps(public_config(cfg, {}).model_dump(by_alias=True, mode="json"))
    assert "litellm.example.com" not in dumped
    assert "mcp.serper.dev" not in dumped
    assert "ACH_AGENT_" not in dumped
    assert "baseUrl" not in dumped

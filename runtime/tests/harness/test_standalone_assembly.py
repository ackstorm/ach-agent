# SPDX-License-Identifier: MIT
"""Standalone assembly: no ACH round-trip, catalog from config, headers from env."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.harness.control import WorkspaceAssemblyUnavailable, assemble
from ach_runtime.harness.hydrate import standalone_catalog

FIXTURE = Path(__file__).parents[1] / "config" / "fixtures" / "standalone-runtime.json"
ENV = {"ACH_SANDBOX_KEY": "harness-key", "ACH_AGENT_MODEL_H0": "model-secret-1",
       "ACH_AGENT_MCP_SERPER_H0": "serper-secret-1"}


def _config():
    wire = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def test_standalone_catalog_comes_from_config():
    catalog = standalone_catalog(_config())
    assert catalog.model.id == "anthropic/claude-sonnet-5.5"
    assert catalog.model.endpoint == "https://litellm.example.com/v1"
    assert catalog.model.metadata.context_limit == 200000
    assert catalog.model.metadata.output_limit == 32000
    assert [(s.id, s.endpoint) for s in catalog.mcp_servers] == [("serper", "https://mcp.serper.dev/mcp")]
    assert catalog.skills == () and catalog.a2a_agents == ()


async def test_standalone_assembly_makes_no_ach_call():
    def ach(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected ACH call {request.url}")

    control = await assemble(_config(), environ=ENV, ach_transport=httpx.MockTransport(ach))
    try:
        assert control.catalog.model.id == "anthropic/claude-sonnet-5.5"
        assert set(control.upstream_secrets) == {"model-secret-1", "serper-secret-1"}
    finally:
        await control.close()


async def test_standalone_assembly_fails_closed_on_missing_header_env():
    env = {k: v for k, v in ENV.items() if k != "ACH_AGENT_MCP_SERPER_H0"}
    with pytest.raises(WorkspaceAssemblyUnavailable, match="ACH_AGENT_MCP_SERPER_H0"):
        await assemble(_config(), environ=env)


async def test_standalone_run_cost_is_unpriced_without_catalog_cost():
    control = await assemble(_config(), environ=ENV)
    try:
        assert control.catalog.model.metadata.input_cost_per_token is None
        assert control._priced is False
    finally:
        await control.close()


async def test_memory_bearer_secret_is_registered_for_redaction():
    from ach_runtime.observability.runs import redact

    wire = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["memory"] = {"type": "ach-memory", "achMemory": {
        "mcpServerId": "serper", "auth": {"type": "bearer", "env": "ACH_MEM_TOKEN"}}}
    wire["configVersion"] = compute_config_version(wire)
    control = await assemble(parse_config(wire), environ={**ENV, "ACH_MEM_TOKEN": "memory-token-value-1"})
    try:
        assert redact("log memory-token-value-1 line") == "log [redacted] line"
    finally:
        await control.close()


def _memory_config(auth):
    wire = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    params = {"mcpServerId": "serper"}
    if auth:
        params["auth"] = auth
    wire["memory"] = {"type": "ach-memory", "achMemory": params}
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


async def test_memory_on_a_standalone_server_keeps_that_servers_headers():
    control = await assemble(_memory_config(None), environ=ENV)
    try:
        assert control._memory_headers == {"X-API-KEY": "serper-secret-1"}
    finally:
        await control.close()


async def test_memory_auth_header_wins_over_the_servers_header_case_insensitively():
    auth = {"type": "bearer", "env": "ACH_MEM_TOKEN", "header": "x-api-key"}
    control = await assemble(_memory_config(auth), environ={**ENV, "ACH_MEM_TOKEN": "memory-token-value-1"})
    try:
        assert control._memory_headers == {"x-api-key": "memory-token-value-1"}
    finally:
        await control.close()

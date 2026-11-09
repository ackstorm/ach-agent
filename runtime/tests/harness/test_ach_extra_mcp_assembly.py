# SPDX-License-Identifier: Apache-2.0
"""capability.ach.mcpServers: added to the hydrated MCP servers with their own headers."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import ConfigError, parse_config
from ach_runtime.harness.control import WorkspaceAssemblyUnavailable, assemble

ENV = {"ACH_SANDBOX_KEY": "harness-private-key", "ACH_SECRET_IDENTITY": "ek-private-value",
       "EXTRA_AUTH": "Bearer extra-secret-1"}


def _wire(memory_auth: dict | None = None, *, memory: bool = False) -> dict:
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"] = {"type": "none"}
    wire["artifacts"]["enabled"] = False
    wire["capability"]["ach"]["mcpServers"] = [
        {"name": "extra", "url": "https://extra.example/mcp", "headers": {"Authorization": {"env": "EXTRA_AUTH"}}},
    ]
    if memory:
        wire["memory"] = {"type": "ach-memory", "achMemory": {"mcpServerId": "extra",
                                                             **({"auth": memory_auth} if memory_auth else {})}}
    wire["configVersion"] = compute_config_version(wire)
    return wire


def _config(**kwargs):
    return parse_config(_wire(**kwargs))


def _ach(hydrated_ids: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json={
                "environment": "engineering-prod",
                "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                            "mcpServers": [{"id": i, "endpoint": f"https://{i}.ach.example/mcp"} for i in hydrated_ids]},
                "context": {"skills": [], "prompts": [], "artifacts": []},
            })
        if request.url.path == "/v2/model/info":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"unexpected ACH request {request.url}")

    return httpx.MockTransport(handler)


async def test_extra_mcp_servers_join_the_hydrated_ones_with_their_own_headers():
    control = await assemble(_config(), environ=ENV, ach_transport=_ach(["tickets"]))
    try:
        assert [(s.id, s.endpoint) for s in control.catalog.mcp_servers] == [
            ("tickets", "https://tickets.ach.example/mcp"), ("extra", "https://extra.example/mcp")]
        assert control._mcp_headers == {"tickets": {}, "extra": {"Authorization": "Bearer extra-secret-1"}}
        assert "Bearer extra-secret-1" in control.upstream_secrets
    finally:
        await control.close()


async def test_memory_on_an_extra_server_keeps_its_headers():
    control = await assemble(_config(memory=True), environ=ENV, ach_transport=_ach(["tickets"]))
    try:
        assert control._memory_headers == {"Authorization": "Bearer extra-secret-1"}
    finally:
        await control.close()


def test_memory_cannot_send_the_ach_credential_to_an_extra_server():
    with pytest.raises(ConfigError, match="ACH credential"):
        parse_config(_wire({"type": "ach"}, memory=True))


async def test_an_extra_mcp_server_named_like_a_hydrated_one_fails_closed():
    with pytest.raises(WorkspaceAssemblyUnavailable, match="extra"):
        await assemble(_config(), environ=ENV, ach_transport=_ach(["extra"]))

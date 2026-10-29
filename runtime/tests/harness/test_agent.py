# SPDX-License-Identifier: MIT
import json
import logging
from pathlib import Path

import httpx
import pytest

from ach_runtime.__main__ import main
from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.harness import agent as agent_module
from ach_runtime.harness.agent import AGENT_FACADE_ENDPOINT, SINGLE_UNIT_WARNING, make_agent_assembly
from ach_runtime.harness.local import LocalRoutingTransport, LocalSource
from ach_runtime.harness.workspaces import WorkspaceRef

pytestmark = pytest.mark.asyncio

FIXTURE = Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json"


def _config():
    wire = json.loads(FIXTURE.read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["channels"][0].pop("handoff", None)
    wire["channels"][0]["prompt"] = "Hello {{ payload.title }}"
    wire["infrastructure"] = {}  # the agent role supplies the loopback default
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def _catalog(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/platform/hydrate":
        return httpx.Response(200, json={
            "environment": "engineering-prod",
            "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                        "mcpServers": [{"id": "ach-memory", "endpoint": "https://memory.example/mcp"}],
                        "a2aAgents": []},
            "context": {"skills": [], "prompts": [], "artifacts": []},
        })
    if request.url.path == "/v2/model/info":
        return httpx.Response(200, json={"data": []})
    raise AssertionError(request.url)


ENV = {"ACH_SANDBOX_KEY": "agent-test-key", "ACH_SECRET_IDENTITY": "x", "ACH_SECRET_GITLAB_WEBHOOK": "x"}


async def test_assembly_defaults_the_facade_and_activates_a_workspace_locally(tmp_path) -> None:
    transport, created = LocalRoutingTransport(), []
    assembly = make_agent_assembly(
        tmp_path, transport, created, environ=ENV, ach_transport=httpx.MockTransport(_catalog)
    )
    config = _config()
    assert config.infrastructure.facade_endpoint is None
    control = await assembly(config)
    try:
        assert control.config.infrastructure.facade_endpoint == AGENT_FACADE_ENDPOINT
        assert control.public.infrastructure.facade_endpoint == AGENT_FACADE_ENDPOINT
        assert created == [control.source] and isinstance(control.source, LocalSource)
        ref = WorkspaceRef(config.agent.namespace, config.agent.uid, "b" * 64)
        endpoint = await control.manager.ensure(ref)
        assert endpoint.base_url == f"http://{endpoint.name}.local" and endpoint.instance_id
    finally:
        await control.close()
        await created[0].aclose()


async def test_a_configured_facade_endpoint_is_kept(tmp_path) -> None:
    wire = json.loads(FIXTURE.read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["channels"][0].pop("handoff", None)
    wire["channels"][0]["prompt"] = "Hello {{ payload.title }}"
    wire["infrastructure"] = {"facadeEndpoint": "http://host.docker.internal:8081/facades"}
    wire["configVersion"] = compute_config_version(wire)
    assembly = make_agent_assembly(
        tmp_path, LocalRoutingTransport(), [], environ=ENV, ach_transport=httpx.MockTransport(_catalog)
    )
    control = await assembly(parse_config(wire))
    try:
        assert control.config.infrastructure.facade_endpoint == "http://host.docker.internal:8081/facades"
    finally:
        await control.close()


async def test_run_agent_warns_once_and_binds_the_facade_to_loopback(tmp_path, monkeypatch, caplog) -> None:
    seen = {}

    async def fake_run_control(config_path, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(agent_module, "run_control", fake_run_control)
    with caplog.at_level(logging.WARNING, logger=agent_module.__name__):
        await agent_module.run_agent(tmp_path / "config.json", data_dir=tmp_path, host="0.0.0.0", port=8080)
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [SINGLE_UNIT_WARNING]
    assert (seen["host"], seen["ingress_port"]) == ("0.0.0.0", 8080)
    assert (seen["facade_host"], seen["facade_port"], seen["host_api"]) == ("127.0.0.1", 8081, False)


def test_cli_agent_subcommand(monkeypatch, tmp_path) -> None:
    calls = {}

    async def fake_run_agent(config_path, **kwargs):
        calls.update(config_path=config_path, **kwargs)

    monkeypatch.setattr("ach_runtime.harness.agent.run_agent", fake_run_agent)
    assert main(["agent", "--config", str(tmp_path / "c.json"), "--data-dir", str(tmp_path), "--port", "9090"]) == 0
    assert calls == {"config_path": tmp_path / "c.json", "data_dir": tmp_path, "host": "0.0.0.0", "port": 9090}

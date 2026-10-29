# SPDX-License-Identifier: MIT
"""The compose example must stay valid: config parses, catalog satisfies contract 3.3.1, stub behaves."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.harness.hydrate import HydrationManifest, resolve_model

EXAMPLE = Path(__file__).parents[2] / "examples" / "hosts" / "compose"
pytestmark = pytest.mark.skipif(not EXAMPLE.is_dir(), reason="examples not mounted")


@pytest.fixture
def stub():
    spec = importlib.util.spec_from_file_location("ach_stub", EXAMPLE / "stub" / "ach_stub.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _app_client(module) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app), base_url="http://stub")


def test_config_parses_and_its_version_is_current() -> None:
    wire = json.loads((EXAMPLE / "config.json").read_text())
    expected = compute_config_version(wire)
    assert wire["configVersion"] == expected, f"stamp configVersion with {expected}"
    config = parse_config(wire)
    assert config.infrastructure.facade_endpoint is None  # the agent role defaults it
    assert config.ach.base_url == "http://ach-stub:9000"
    channel = config.channels[0]
    assert channel.type == "webhook" and channel.routing is not None and channel.routing.session_key
    assert config.workspace.persistence.enabled and config.workspace.session.persistence.enabled


def test_catalog_grants_the_configured_model_with_an_endpoint() -> None:
    wire = json.loads((EXAMPLE / "config.json").read_text())
    manifest = HydrationManifest.model_validate(json.loads((EXAMPLE / "stub" / "catalog.json").read_text()))
    assert resolve_model(manifest, wire["model"]["name"]).endpoint
    assert manifest.environment == wire["ach"]["environment"]


@pytest.mark.asyncio
async def test_stub_serves_hydrate_and_model_info_to_any_key(stub) -> None:
    async with _app_client(stub) as client:
        hydrate = await client.post("/platform/hydrate", json={"environment": "local"}, headers={"x-ach-key": "whatever"})
        info = await client.get("/v2/model/info", params={"model": "local-model"})
        health = await client.get("/healthz")
    assert hydrate.status_code == 200 and hydrate.json()["runtime"]["models"][0]["endpoint"]
    assert info.json() == {"data": []}
    assert health.status_code == 200


@pytest.mark.asyncio
async def test_stub_proxies_model_traffic_with_the_upstream_key(stub, monkeypatch) -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"data: pong\n\n", headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(stub, "UPSTREAM", "http://upstream:11434")
    monkeypatch.setattr(stub, "API_KEY", "sk-upstream")
    monkeypatch.setattr(stub, "client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    async with _app_client(stub) as client:
        response = await client.post(
            "/v1/chat/completions?x=1", json={"model": "m"},
            headers={"x-ach-key": "k", "authorization": "Bearer facade-token", "x-ach-run": "r"},
        )
    assert response.status_code == 200 and response.content == b"data: pong\n\n"
    request = seen[0]
    assert str(request.url) == "http://upstream:11434/v1/chat/completions?x=1"
    assert request.headers["authorization"] == "Bearer sk-upstream"
    assert "x-ach-key" not in request.headers and "x-ach-run" not in request.headers


@pytest.mark.asyncio
async def test_stub_rejects_unknown_paths_and_a_missing_upstream(stub, monkeypatch) -> None:
    monkeypatch.setattr(stub, "UPSTREAM", "")
    async with _app_client(stub) as client:
        assert (await client.post("/v1/chat/completions", json={})).status_code == 503
        assert (await client.get("/nothing")).status_code == 404

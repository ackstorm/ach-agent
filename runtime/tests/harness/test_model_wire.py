# SPDX-License-Identifier: MIT
"""Configured provider transports stay on their native ACH model endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.harness.control import Control, assemble
from ach_runtime.harness.tokens import facade_token
from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

FIXTURE = Path(__file__).parents[1] / "config" / "fixtures" / "pr-review-runtime.json"
MODEL_ID = "gemini.gemini-flash-latest"
ACH_BASE = "https://configured-ach.example/model/"


async def _response_body() -> AsyncIterator[bytes]:
    yield b'{"ok":true}'


def _config(model_type: str):
    wire = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire["ach"]["baseUrl"] = ACH_BASE
    wire["model"]["type"] = model_type
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["channels"][0].pop("handoff", None)
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


@pytest.mark.parametrize(
    ("model_type", "path", "prefix"),
    [
        ("openai", "/chat/completions", "/v1"),
        ("gemini", f"/models/{MODEL_ID}:streamGenerateContent?alt=sse", "/gemini/v1beta"),
        ("anthropic", "/messages", "/anthropic"),
    ],
)
async def test_model_facade_uses_configured_native_ach_endpoint(model_type, path, prefix, monkeypatch):
    upstream: list[tuple[str, dict[str, str]]] = []

    def ach_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/platform/hydrate"):
            return httpx.Response(200, json={
                "environment": "engineering-prod",
                "runtime": {"models": [{"id": MODEL_ID, "endpoint": "https://unrelated.example/v1"}],
                            "mcpServers": [], "a2aAgents": []},
                "context": {"skills": [], "prompts": [], "artifacts": []},
            })
        if request.url.path.endswith("/v2/model/info"):
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url)

    def upstream_handler(request: httpx.Request) -> httpx.Response:
        upstream.append((str(request.url), dict(request.headers)))
        return httpx.Response(200, content=_response_body())

    async def owned_live(self, claim: str) -> bool:
        return claim == "ach-workspace"

    monkeypatch.setattr(Control, "_owned_live", owned_live)
    control = await assemble(
        _config(model_type),
        environ={"ACH_SANDBOX_KEY": "harness-key", "ACH_SECRET_IDENTITY": "private-ek"},
        ach_transport=httpx.MockTransport(ach_handler),
        facade_transport=httpx.MockTransport(upstream_handler),
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(control.config.agent.namespace, control.config.agent.uid, "a" * 64),
        "ach-workspace", "instance-id", "http://mini",
    )
    token = facade_token(control.key, endpoint.name)
    app = control.model_facade.app()
    try:
        assert control.catalog.model.endpoint == "https://unrelated.example/v1"
        execution_catalog = control._catalog_for(endpoint)
        model_urls = [provider["baseURL"] for provider in execution_catalog["providers"].values()]
        assert model_urls == [f"{control.public.infrastructure.facade_endpoint.rstrip('/')}/model/t/{token}"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
            response = await client.post(f"/t/{token}{path}", json={"probe": True})
        assert response.status_code == 200
        assert len(upstream) == 1
        assert upstream[0][0] == ACH_BASE.rstrip("/") + prefix + path
        assert upstream[0][1]["x-ach-key"] == "private-ek"
    finally:
        await control.close()

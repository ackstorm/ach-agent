# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.facades.model import public_catalog
from ach_runtime.harness.control import assemble, create_facade_app
from ach_runtime.harness.hydrate import HydratedCatalog, HydratedModel
from ach_runtime.harness.tokens import facade_token
from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef
from ach_runtime.storage.s3 import S3Storage
from tests.storage.fakes import FakeS3Client


def _ach_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/platform/hydrate":
        return httpx.Response(200, json={
            "environment": "engineering-prod",
            "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.invalid/v1"}],
                        "mcpServers": [], "a2aAgents": []},
            "context": {"skills": [], "prompts": [], "artifacts": []},
        })
    if request.url.path == "/v2/model/info":
        return httpx.Response(200, json={"data": []})
    raise AssertionError(request.url)


def _config(enabled: bool):
    wire = json.loads(
        (Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text()
    )
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = enabled
    wire["channels"][0].pop("handoff", None)
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def test_public_catalog_projects_optional_artifact_facade_after_granted_entries() -> None:
    catalog = public_catalog(
        HydratedCatalog(
            environment="platform",
            model=HydratedModel(
                id="model-x", endpoint="https://model.invalid", metadata=None
            ),
            mcp_servers=(),
            skills=(),
            prompts=(),
            artifacts=(),
        ),
        provider_id="openai",
        model_url="https://facade.invalid/model",
        mcp_urls={"granted": "https://facade.invalid/mcp/granted"},
        memory_url="https://facade.invalid/memory",
        artifact_url="https://facade.invalid/artifacts/t/token",
    )
    assert catalog["mcp"] == {
        "granted": {"url": "https://facade.invalid/mcp/granted", "headers": {}},
        "memory": {"url": "https://facade.invalid/memory", "headers": {}},
        "artifacts": {"url": "https://facade.invalid/artifacts/t/token", "headers": {}},
    }


@pytest.mark.asyncio
async def test_artifact_catalog_and_mount_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ach_runtime.harness.control.storage_from_env", lambda *_args: None)
    env = {
        "ACH_SANDBOX_KEY": "artifact-assembly-key",
        "ACH_SECRET_IDENTITY": "ek-private",
        "ACH_STORAGE_S3_BUCKET": "private-bucket-sentinel",
    }
    disabled = await assemble(
        _config(False), environ=env,
        ach_transport=httpx.MockTransport(_ach_transport),
    )
    enabled = await assemble(
        _config(True), environ=env,
        ach_transport=httpx.MockTransport(_ach_transport),
    )
    try:
        endpoint = WorkspaceEndpoint(
            WorkspaceRef(enabled.config.agent.namespace, enabled.config.agent.uid, "a" * 64),
            "ws-enabled", "pod-live", "http://mini",
        )
        no_artifacts = disabled._catalog_for(endpoint)
        with_artifacts = enabled._catalog_for(endpoint)
        assert "artifacts" not in no_artifacts["mcp"]
        assert with_artifacts["mcp"]["artifacts"] == {
            "url": (
                f"{enabled.public.infrastructure.facade_endpoint.rstrip('/')}/artifacts/t/"
                f"{facade_token(enabled.key, endpoint.name)}"
            ),
            "headers": {},
        }
        assert "private-bucket-sentinel" not in json.dumps(with_artifacts)
        assert disabled.artifact_facade is None
        assert enabled.artifact_facade is not None

        workspace_ref = "a" * 64
        # The one live Workspace, as the manager's public accessors publish it.
        monkeypatch.setattr(enabled.manager, "active_endpoints", lambda: {workspace_ref: endpoint})
        monkeypatch.setattr(enabled.manager, "active_endpoint", lambda key: endpoint if key == workspace_ref else None)
        invocation_id = "inv-mounted"
        session_ref = "b" * 64
        enabled._prepared[invocation_id] = (
            endpoint,
            SimpleNamespace(
                invocation_id=invocation_id,
                workspace_ref=workspace_ref,
                session_ref=session_ref,
                deadline_at=datetime.now(timezone.utc) + timedelta(minutes=2),
            ),
        )
        app = create_facade_app(enabled)
        assert "/facades/artifacts" in {route.path for route in app.router.routes}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://localhost:8094"
            ) as client:
                async with streamable_http_client(
                    "http://localhost:8094/facades/artifacts/t/"
                    f"{facade_token(enabled.key, endpoint.name)}/runs/{invocation_id}/",
                    http_client=client,
                ) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        unavailable = await session.call_tool(
                            "create_artifact",
                            {
                                "idempotency_key": "unavailable-report",
                                "content": "no receipt",
                                "mime": "text/plain",
                                "name": "unavailable.txt",
                            },
                        )
                        assert unavailable.isError
                        assert unavailable.structuredContent is None

                        enabled.storage = S3Storage(
                            bucket="reports", prefix="ach", client=FakeS3Client()
                        )
                        created = await session.call_tool(
                            "create_artifact",
                            {
                                "idempotency_key": "mounted-report",
                                "content": "mounted text",
                                "mime": "text/plain",
                                "name": "mounted.txt",
                            },
                        )
                        assert not created.isError
                        receipt = json.loads(created.content[0].text)
                        assert receipt["metadata"]["owner"]["workspace_ref"] == workspace_ref
                        assert receipt["metadata"]["producer"] == {
                            "session_ref": session_ref,
                            "invocation_id": invocation_id,
                        }
    finally:
        await disabled.close()
        await enabled.close()

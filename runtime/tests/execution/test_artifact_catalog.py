# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import json
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ach_runtime.engine.config import to_fixture
from ach_runtime.engine.opencode import EngineReceipt
from ach_runtime.execution.service import ExecutionService
from ach_runtime.execution.sessions import SessionRegistry
from ach_runtime.execution.wire import StartRequest


class _InvocationEngine:
    def __init__(self) -> None:
        self.invocations = []

    async def start(self, invocation, *, home, scratch, password=None):
        self.invocations.append(invocation)
        return EngineReceipt(
            native_session_id=f"native-{invocation.invocation_id}",
            input_id=f"input-{invocation.invocation_id}",
        )

    async def status(self, invocation_id):
        return "completed"

    async def wait_settled(self, invocation_id):
        return "completed"

    async def cancel(self, invocation_id, reason, *, _halt_budget=None):
        return None

    async def suspend(self, session_ref):
        return True

    async def snapshot(self, session_ref, *, agent, workspace_ref, registry):
        raise AssertionError("snapshot is outside this test")

    async def restore(self, *args, **kwargs):
        return None

    async def close(self):
        return None


def _request(config_version: str, invocation_id: str, session_ref: str) -> StartRequest:
    return StartRequest(workspaceKey="wk", sessionKey="sk", 
        invocationId=invocation_id,
        channel="webhook",
        workspaceRef="a" * 64,
        sessionRef=session_ref,
        configVersion=config_version,
        prompt="publish report",
        deadlineAt=datetime.now(timezone.utc) + timedelta(seconds=30),
    )


@pytest.mark.asyncio
async def test_artifact_catalog_specialization_is_invocation_local(
    tmp_path: Path, public_config_fixture
) -> None:
    engine = _InvocationEngine()
    service = ExecutionService(engine=engine, sessions=SessionRegistry(tmp_path))
    base = f"{public_config_fixture.infrastructure.facade_endpoint.rstrip('/')}/artifacts/t/token"
    original = {
        "providers": {
            public_config_fixture.model.type: {
                "baseURL": "http://model.invalid",
                "apiKey": "local-proxy",
                "models": {public_config_fixture.model.name: {}},
            }
        },
        "mcp": {
            "artifacts": {"url": base, "headers": {}},
            "calendar": {"url": "http://calendar.invalid/mcp", "headers": {"x-read": "yes"}},
        },
    }
    service.configure(public_config_fixture, catalog=original)
    try:
        await asyncio.gather(
            service.start(_request(public_config_fixture.config_version, "run-a", "session-a")),
            service.start(_request(public_config_fixture.config_version, "run-b", "session-b")),
        )
        by_id = {invocation.invocation_id: invocation for invocation in engine.invocations}
        assert by_id["run-a"].catalog["mcp"]["artifacts"]["url"] == f"{base}/runs/run-a/"
        assert by_id["run-b"].catalog["mcp"]["artifacts"]["url"] == f"{base}/runs/run-b/"
        assert by_id["run-a"].catalog["mcp"]["calendar"] == original["mcp"]["calendar"]
        assert by_id["run-b"].catalog["mcp"]["calendar"] == original["mcp"]["calendar"]
        assert original["mcp"]["artifacts"]["url"] == base

        for _ in range(5):
            await asyncio.sleep(0)
        await service.start(
            _request(public_config_fixture.config_version, "run-a-later", "session-a")
        )
        later = next(inv for inv in engine.invocations if inv.invocation_id == "run-a-later")
        assert later.catalog["mcp"]["artifacts"]["url"] == f"{base}/runs/run-a-later/"
        assert original["mcp"]["calendar"] == {
            "url": "http://calendar.invalid/mcp", "headers": {"x-read": "yes"}
        }

        fixture = to_fixture(public_config_fixture, catalog=by_id["run-a"].catalog)
        assert fixture["mcp"]["artifacts"]["url"] == f"{base}/runs/run-a/"
        assert fixture["mcp"]["calendar"] == original["mcp"]["calendar"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_tools_preflight_probes_the_per_run_artifacts_url(
    tmp_path: Path, public_config_fixture, monkeypatch
) -> None:
    import ach_runtime.execution.service as service_module

    probed: list[str] = []

    async def count(url, headers):
        probed.append(url)
        return 1

    monkeypatch.setattr(service_module, "_count_mcp_tools", count)
    engine = _InvocationEngine()
    service = ExecutionService(engine=engine, sessions=SessionRegistry(tmp_path))
    base = f"{public_config_fixture.infrastructure.facade_endpoint.rstrip('/')}/artifacts/t/token"
    catalog = {
        "providers": {
            public_config_fixture.model.type: {
                "baseURL": "http://model.invalid",
                "apiKey": "local-proxy",
                "models": {public_config_fixture.model.name: {}},
            }
        },
        "mcp": {"artifacts": {"url": base, "headers": {}}},
    }
    service.configure(public_config_fixture, catalog=catalog)
    try:
        await service.start(_request(public_config_fixture.config_version, "run-a", "session-a"))
        assert probed == [f"{base}/runs/run-a/"]  # the facade 404s on the bare token URL
        assert [inv.invocation_id for inv in engine.invocations] == ["run-a"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_granted_mcp_artifacts_server_is_not_specialized(
    tmp_path: Path, public_config_fixture
) -> None:
    engine = _InvocationEngine()
    service = ExecutionService(engine=engine, sessions=SessionRegistry(tmp_path))
    granted_url = "https://granted.example.invalid/mcp/artifacts"
    original_entry = {"url": granted_url, "headers": {"x-grant": "read-only"}}
    catalog = {
        "providers": {
            public_config_fixture.model.type: {
                "baseURL": "http://model.invalid",
                "apiKey": "local-proxy",
                "models": {public_config_fixture.model.name: {}},
            }
        },
        "mcp": {"artifacts": dict(original_entry)},
    }
    service.configure(public_config_fixture, catalog=catalog)
    try:
        await service.start(
            _request(public_config_fixture.config_version, "run-granted", "session-granted")
        )
        invocation = engine.invocations[0]
        assert invocation.catalog["mcp"]["artifacts"] == original_entry
        assert catalog["mcp"]["artifacts"] == original_entry
    finally:
        await service.close()


def test_managed_plugin_registers_artifacts_remote_without_rewriting_values() -> None:
    script = textwrap.dedent(
        """
        import plugin from './plugins/managed-config/index.js'

        async function capture(mcp) {
          const registrations = []
          const ctx = {
            options: { fixture: {
              managedAgent: 'build', maxSteps: 8,
              providers: { managed: { baseURL: 'http://model.invalid', models: {} } },
              ...(mcp === undefined ? {} : { mcp }),
            } },
            agents: { build: {} },
            agent: { async transform(edit) { await edit({ update(_id, mutate) { mutate(ctx.agents.build) } }) } },
            provider: { async transform(edit) { await edit({ add() {} }) } },
            mcp: { async transform(edit) { await edit({ set(name, value) { registrations.push([name, value]) } }) } },
            session: { async hook() {} },
          }
          await plugin.setup(ctx)
          return registrations
        }

        const supplied = { url: 'http://facade.invalid/artifacts/t/token/runs/run-a/', headers: { 'x-test': 'kept' } }
        const result = [await capture({ artifacts: supplied }), await capture(undefined)]
        process.stdout.write(JSON.stringify(result))
        """
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
    )
    registrations = json.loads(completed.stdout)
    assert registrations[0] == [[
        "artifacts",
        {
            "type": "remote",
            "url": "http://facade.invalid/artifacts/t/token/runs/run-a/",
            "headers": {"x-test": "kept"},
            "oauth": False,
            "codemode": False,
            "protocol": "legacy",
        },
    ]]
    assert registrations[1] == []

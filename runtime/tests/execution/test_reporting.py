# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ach_runtime.engine.opencode import EngineReceipt, SnapshotManifest
from ach_runtime.config.schema import HookBlock, HooksBlock
from ach_runtime.execution.service import ExecutionService
from ach_runtime.execution.sessions import SessionRegistry
from ach_runtime.execution.wire import StartRequest
from ach_runtime.observability import RunUsage


class ReportingEngine:
    def __init__(self) -> None:
        self.release = asyncio.Event()

    async def start(self, invocation, *, home, scratch, password=None):
        return EngineReceipt("native", "input")

    async def status(self, invocation_id):
        return "running"

    async def wait_settled(self, invocation_id):
        await self.release.wait()
        return "completed"

    def result(self, invocation_id):
        return "raw result"

    def usage(self, invocation_id):
        return RunUsage("opencode", "engine_estimate", 1, 2, 0, 0, 0, 0, 1)

    async def suspend(self, session_ref):
        return True

    async def snapshot(self, session_ref, *, agent, workspace_ref, registry):
        return SnapshotManifest("session-v1", "@opencode/cli", "2.0.20", agent,
                                workspace_ref, session_ref, "native", registry["workspace_path"],
                                registry=registry)

    async def restore(self, *args, **kwargs):
        return None

    async def cancel(self, *args, **kwargs):
        return None

    async def close(self):
        return None


class BuiltInvocation:
    def __init__(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id


def _request(config_version: str) -> StartRequest:
    return StartRequest(workspaceKey="wk", sessionKey="sk", invocationId="reporting-run", channel="webhook", workspaceRef="ws" * 16,
                        sessionRef="se" * 16, configVersion=config_version, prompt="secret prompt",
                        deadlineAt=datetime.now(timezone.utc) + timedelta(minutes=1))


async def test_reporting_is_retained_in_status_snapshot_and_restore(tmp_path, public_config_fixture):
    engine = ReportingEngine()
    service = ExecutionService(engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
                               workspace_dir=tmp_path / "workspace",
                               build_invocation=lambda req, _public, _workspace: BuiltInvocation(req.invocation_id))
    service.configure(public_config_fixture)
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    invocation = service._invocations[request.invocation_id]
    engine.release.set()
    assert invocation.settle_task is not None
    await invocation.settle_task
    status = await service.status(request.invocation_id)
    assert status.result == {"text": "raw result", "reporting": {
        "duration_ms": status.result["reporting"]["duration_ms"],
        "usage": {"source": "opencode", "basis": "engine_estimate", "input": 1,
                  "output": 2, "reasoning": 0, "cache_read": 0, "cache_write": 0,
                  "cost_usd": 0, "steps": 1},
    }}
    assert status.result["reporting"]["duration_ms"] >= 0
    assert (await service.list_runs())[0].result == status.result
    manifest = await service.snapshot(request.session_ref)
    assert (await service.session_info(request.session_ref))["suspendHookFailed"] is False
    assert manifest.registry["runs"][0]["result"] == status.result

    restored = ExecutionService(engine=ReportingEngine(), sessions=SessionRegistry(tmp_path / "restored"),
                                workspace_dir=tmp_path / "workspace",
                                build_invocation=lambda req, _public, _workspace: BuiltInvocation(req.invocation_id))
    restored.configure(public_config_fixture)
    await restored.restore(request.session_ref, request.workspace_ref, manifest)
    assert await restored.status(request.invocation_id) == status
    assert (await restored.session_info(request.session_ref))["suspendHookFailed"] is False


async def test_failed_suspend_hook_is_reported_without_blocking_snapshot(
    tmp_path, public_config_fixture, monkeypatch
):
    import ach_runtime.execution.service as service_module
    from ach_runtime.execution.sessions import SessionHookFailed

    async def fail_hook(hook, *args, **kwargs):
        if hook is not None and hook.script == "exit 1":
            raise SessionHookFailed("suspend-secret")

    monkeypatch.setattr(service_module, "run_hook", fail_hook)
    public = public_config_fixture.model_copy(deep=True)
    public = public.model_copy(update={"hooks": HooksBlock(sessionSuspend=HookBlock(script="exit 1"))})
    engine = ReportingEngine()
    engine.release.set()
    service = ExecutionService(engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
                               workspace_dir=tmp_path / "workspace",
                               build_invocation=lambda req, _public, _workspace: BuiltInvocation(req.invocation_id))
    service.configure(public)
    request = _request(public.config_version)
    await service.start(request)
    invocation = service._invocations[request.invocation_id]
    assert invocation.settle_task is not None
    await invocation.settle_task
    manifest = await service.snapshot(request.session_ref)
    assert manifest.registry["suspend_hook_error"] == "suspend-secret"
    info = await service.session_info(request.session_ref)
    assert info["suspendHookFailed"] is True
    assert "suspend-secret" not in str(info)

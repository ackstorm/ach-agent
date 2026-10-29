# SPDX-License-Identifier: MIT
"""Raw native result retention through mini run records and snapshots."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ach_runtime.engine.opencode import EngineReceipt, SnapshotManifest
from ach_runtime.execution.service import ExecutionService
from ach_runtime.execution.sessions import SessionRegistry
from ach_runtime.execution.wire import StartRequest


class ResultEngine:
    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.settlement_release = asyncio.Event()
        self.result_calls: list[str] = []

    async def start(self, invocation, *, home, scratch, password=None):
        return EngineReceipt(native_session_id="native-result", input_id="input-result")

    async def status(self, invocation_id):
        return "running"

    async def wait_settled(self, invocation_id):
        await self.settlement_release.wait()
        return "completed"

    def result(self, invocation_id):
        self.result_calls.append(invocation_id)
        return self.raw_text

    async def suspend(self, session_ref):
        return True

    async def snapshot(self, session_ref, *, agent, workspace_ref, registry):
        return SnapshotManifest(
            snapshot_version="session-v1",
            engine_package="@opencode/cli",
            engine_version="2.0.20",
            agent=agent,
            workspace_ref=workspace_ref,
            session_ref=session_ref,
            native_session_id="native-result",
            workspace_path=registry["workspace_path"],
            registry=registry,
        )

    async def restore(self, *args, **kwargs):
        return None

    async def cancel(self, *args, **kwargs):
        return None

    async def close(self):
        return None


class Invocation:
    def __init__(self, invocation_id: str) -> None:
        self.invocation_id = invocation_id


def _request(config_version: str) -> StartRequest:
    return StartRequest(
        workspaceKey="wk", sessionKey="sk",
        invocationId="inv-result",
        channel="webhook",
        workspaceRef="ws" * 16,
        sessionRef="se" * 16,
        configVersion=config_version,
        prompt="return a raw result",
        deadlineAt=datetime.now(timezone.utc) + timedelta(minutes=1),
    )


def _service(tmp_path: Path, public, engine: ResultEngine) -> ExecutionService:
    service = ExecutionService(
        engine=engine,
        sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=tmp_path / "workspace",
        build_invocation=lambda request, _public, _workspace: Invocation(request.invocation_id),
    )
    service.configure(public)
    return service


async def test_result_is_settled_retained_listed_and_restored(tmp_path, public_config_fixture):
    raw_text = '{"action":"a2a_reply","text":"hello"}\nraw trailing bytes'
    engine = ResultEngine(raw_text)
    service = _service(tmp_path / "before", public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    started = await service.start(request)
    invocation = service._invocations[request.invocation_id]

    # A Started receipt is not result settlement; result lookup stays behind wait_settled.
    await asyncio.sleep(0)
    assert engine.result_calls == []
    assert (await service.status(request.invocation_id)).result is None

    engine.settlement_release.set()
    assert invocation.settle_task is not None
    await invocation.settle_task
    status = await service.status(request.invocation_id)
    assert status.result["text"] == raw_text
    assert status.result["reporting"]["duration_ms"] >= 0
    assert status.result["reporting"]["usage"] is None
    assert (await service.list_runs())[0].result == status.result
    assert engine.result_calls == [request.invocation_id]

    manifest = await service.snapshot(request.session_ref)
    saved = manifest.registry["runs"][0]
    assert saved["result"] == status.result
    assert saved["started"] == started.model_dump(by_alias=True, mode="json")

    restored = _service(tmp_path / "after", public_config_fixture, ResultEngine("unused"))
    await restored.restore(request.session_ref, request.workspace_ref, manifest)
    restored_status = await restored.status(request.invocation_id)
    assert restored_status == status
    assert await restored.start(request) == started
    assert await restored.list_runs() == [status]


async def test_old_engine_without_usage_getter_retains_unknown_usage(tmp_path, public_config_fixture):
    class OldEngine(ResultEngine):
        result = None

    engine = OldEngine("unused")
    service = _service(tmp_path, public_config_fixture, engine)
    request = _request(public_config_fixture.config_version)
    await service.start(request)
    engine.settlement_release.set()
    invocation = service._invocations[request.invocation_id]
    assert invocation.settle_task is not None
    await invocation.settle_task
    result = (await service.status(request.invocation_id)).result
    assert result["reporting"]["duration_ms"] >= 0
    assert result["reporting"]["usage"] is None
    assert "text" not in result

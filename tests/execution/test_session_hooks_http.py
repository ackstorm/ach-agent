# SPDX-License-Identifier: Apache-2.0
"""F5 + item 1 over the real HTTP seam: a failing sessionStart keeps H and retries as new."""

from __future__ import annotations

from pathlib import Path

import pytest

from ach_agent.boot.execution_client import ExecutionClient, WorkspaceOperationFailed
from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import HookSpec, PublicEngineConfig, WorkspacePrepareRequest
from tests.execution.test_http import _running_server


def _req(tmp_path: Path, invocation_id: str) -> WorkspacePrepareRequest:
    return WorkspacePrepareRequest(
        controller_id="h",
        invocation_id=invocation_id,
        session_key="999999:1",
        event_id=invocation_id,
        home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "work"),
        remaining_seconds=10,
    )


@pytest.mark.asyncio
async def test_failing_session_start_is_retried_as_new_without_controller_loss(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.configure(PublicEngineConfig(hook_session_start=HookSpec(script="exit 1")))
    async with _running_server(create_execution_app(service)) as url:
        client = ExecutionClient(url, controller_id="h")
        await client.connect()
        try:
            for invocation in ("inv-1", "inv-2"):
                result = await client.prepare_workspace(_req(tmp_path, invocation))
                assert result["new_session"] is True
                with pytest.raises(WorkspaceOperationFailed):
                    await client.start_session("h", invocation)
                assert not client.controller_lost
                await client.cancel("h", invocation)
        finally:
            await client.close()

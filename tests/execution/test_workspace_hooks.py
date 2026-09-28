"""Shared workspace reservation lifecycle coverage.

Hooks execute in the harness. E only reserves the shared workspace and owns native
lifecycle; there is no correlated stop notification — session-stop hooks
(hooks.sessionSuspend) run inside the mini-harness itself via EnginePool(on_stop=...).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import uvicorn

from ach_agent.boot.execution_client import (
    ExecutionClient,
    WorkspaceOperationFailed,
)
from ach_agent.engine.workspace import prepare_workspace, workspace_dir
from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import (
    AcquireRequest,
    PublicEngineConfig,
    WorkspacePrepareRequest,
)


@contextlib.asynccontextmanager
async def _running_server(app: object) -> AsyncIterator[str]:
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started and server.servers:
                break
            await asyncio.sleep(0.01)
        assert server.servers
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


def _prepare(tmp_path: Path, **changes: object) -> WorkspacePrepareRequest:
    values: dict[str, object] = {
        "controller_id": "controller",
        "invocation_id": "invocation",
        "session_key": "group/project:1",
        "event_id": "event-1",
        "home": str(tmp_path / "home"),
        "work_dir": str(tmp_path / "work"),
        "remaining_seconds": 5,
    }
    values.update(changes)
    return WorkspacePrepareRequest.model_validate(values)


@pytest.mark.asyncio
async def test_reserves_exact_shared_workspace_path(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)

    result = await service.prepare_workspace(request)

    expected = workspace_dir(str(tmp_path / "work"), request.session_key)
    assert result == {"status": "ok", "workspace": str(expected), "new_session": True}
    assert expected.is_dir()
    assert (expected / ".ach-state").is_symlink()
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_workspace_state_link_targets_installed_engine_context(tmp_path: Path) -> None:
    home = tmp_path / "engine-home"
    work = tmp_path / "work"
    transfer = tmp_path / "transfer"
    batch = transfer / ".ach-harness-shared-files-test"
    (batch / "skills").mkdir(parents=True)
    (batch / "prompts").mkdir()
    (batch / "artifacts").mkdir()
    service = ExecutionService(None, {})
    await service.configure(
        PublicEngineConfig(
            binary_path="true",
            home=str(home),
            work_dir=str(work),
            hydration_dir=str(batch),
        )
    )

    workspace = prepare_workspace(str(home), str(work), "group/project:public")
    assert (workspace / ".ach-state").resolve() == (home / ".ach-state").resolve()
    (home / ".ach-state" / "prepared.txt").write_text("installed")
    assert (workspace / ".ach-state" / "prepared.txt").read_text() == "installed"
    await service.close()


@pytest.mark.asyncio
async def test_duplicate_cancel_joins_one_cleanup_and_waits_for_callback(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)
    calls = 0
    original = service._finish_workspace_reservation

    async def counted(invocation_id: str, reservation: object) -> None:
        nonlocal calls
        calls += 1
        await original(invocation_id, reservation)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_finish_workspace_reservation", counted)
    await asyncio.gather(
        service.cancel("controller", request.invocation_id),
        service.cancel("controller", request.invocation_id),
    )
    assert request.invocation_id not in service._workspace_reservations
    assert calls == 1
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_controller_loss_joins_reservation_cleanup_before_reconnect(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)
    started = asyncio.Event()
    allowed = asyncio.Event()
    original = service._finish_workspace_reservation

    async def blocked(invocation_id: str, reservation: object) -> None:
        started.set()
        await allowed.wait()
        await original(invocation_id, reservation)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "_finish_workspace_reservation", blocked)
    release = asyncio.create_task(service.release_controller("controller"))
    await asyncio.wait_for(started.wait(), timeout=1)
    assert not release.done()
    allowed.set()
    await release
    assert service.can_accept_controller


@pytest.mark.asyncio
async def test_cancel_blocked_reserved_acquire_has_one_cleanup_owner(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)
    fake_driver.launch_barrier = asyncio.Event()
    acquire_task = asyncio.create_task(
        service.acquire(
            AcquireRequest(
                controller_id="controller",
                invocation_id=request.invocation_id,
                lane_key=request.session_key,
                conversation_key="conversation",
                reuse=True,
                remaining_seconds=5,
                config=PublicEngineConfig(),
            )
        )
    )
    await asyncio.wait_for(fake_driver.launch_started.wait(), timeout=1)
    await asyncio.wait_for(service.cancel("controller", request.invocation_id), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await acquire_task
    assert not service._unhealthy
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_controller_loss_cancels_blocked_reserved_acquire_once(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)
    fake_driver.launch_barrier = asyncio.Event()
    acquire_task = asyncio.create_task(
        service.acquire(
            AcquireRequest(
                controller_id="controller",
                invocation_id=request.invocation_id,
                lane_key=request.session_key,
                conversation_key="conversation",
                reuse=True,
                remaining_seconds=5,
                config=PublicEngineConfig(),
            )
        )
    )
    await asyncio.wait_for(fake_driver.launch_started.wait(), timeout=1)
    await asyncio.wait_for(service.release_controller("controller"), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await acquire_task
    assert service.can_accept_controller


@pytest.mark.asyncio
async def test_real_http_malformed_success_confirms_reservation_cancel(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    original_prepare = service.prepare_workspace

    async def malformed(request: WorkspacePrepareRequest) -> dict[str, str]:
        await original_prepare(request)
        return {"status": "ok"}

    monkeypatch.setattr(service, "prepare_workspace", malformed)
    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=0.5)
        try:
            await client.connect()
            request = _prepare(tmp_path)
            with pytest.raises(WorkspaceOperationFailed, match="invalid workspace") as failure:
                await client.prepare_workspace(request)
            assert failure.value.confirmed
            assert request.invocation_id not in service._workspace_reservations
            assert not client._failed
            monkeypatch.undo()
            peer = await client.prepare_workspace(
                _prepare(tmp_path, invocation_id="peer", session_key="peer")
            )
            assert peer["status"] == "ok"
            await client.cancel("controller", "peer")
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_pending_same_lane_reservation_is_rejected_without_mutation(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    first = _prepare(tmp_path)
    await service.prepare_workspace(first)

    with pytest.raises(ValueError, match="reservation|lane"):
        await service.prepare_workspace(_prepare(tmp_path, invocation_id="other"))

    assert set(service._workspace_reservations) == {first.invocation_id}
    await service.cancel("controller", first.invocation_id)
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_cancel_reclaims_reservation_and_peer_can_acquire(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)
    await service.cancel("controller", request.invocation_id)
    assert request.invocation_id not in service._workspace_reservations

    peer = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="peer",
            lane_key="peer-lane",
            conversation_key="peer",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(),
        )
    )
    assert peer.invocation_id == "peer"
    await service.cancel("controller", peer.invocation_id)
    await service.release_controller("controller")

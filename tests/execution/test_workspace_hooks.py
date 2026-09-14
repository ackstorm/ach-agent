from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from pydantic import ValidationError

from ach_agent.boot.execution_client import (
    ExecutionClient,
    ExecutionClientError,
    WorkspaceOperationFailed,
)
from ach_agent.boot.private_prepare import PrivateCleanupRegistry
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock
from ach_agent.engine.lifecycle import OwnedProcessCleanupError
from ach_agent.engine.workspace import (
    WorkspaceHookExitFailed,
    WorkspaceHookTimedOut,
    run_public_hook,
)
from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import (
    AcquireRequest,
    PublicEngineConfig,
    ReleaseRequest,
    TurnRequest,
    WorkspaceCleanupAckRequest,
    WorkspaceHandoffRequest,
    WorkspaceHook,
    WorkspacePrepareRequest,
    WorkspaceStoppedEvent,
)


def _prepare(tmp_path: Path, **changes: object) -> WorkspacePrepareRequest:
    values: dict[str, object] = {
        "controller_id": "controller",
        "invocation_id": "invocation",
        "session_key": "group/project:1",
        "event_id": "event-1",
        "channel_name": "review",
        "delivery_context": {"project_path": "group/project"},
        "home": str(tmp_path / "home"),
        "work_dir": str(tmp_path / "work"),
        "prepare": {
            "script": "printf '%s' \"$ACH_EVENT_PROJECT_PATH\" > prepared.txt",
            "env": {},
            "timeout_seconds": 2,
        },
        "remaining_seconds": 5,
    }
    values.update(changes)
    return WorkspacePrepareRequest.model_validate(values)


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


@pytest.mark.asyncio
async def test_public_prepare_completes_before_native_acquire(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    result = await service.prepare_workspace(_prepare(tmp_path))

    workspace = Path(result["workspace"])
    assert (workspace / "prepared.txt").read_text() == "group/project"
    assert fake_driver.servers == []
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_public_prepare_failure_does_not_launch_native(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare={"script": "exit 17", "timeout_seconds": 2})

    with pytest.raises(RuntimeError, match="public hook exited 17"):
        await service.prepare_workspace(request)
    assert fake_driver.servers == []
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_private_only_prepare_still_notifies_harness_on_stop(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare=None, cleanup=None)
    await service.prepare_workspace(request)
    await service.pool.discard(request.session_key)
    events = service.controller_events()
    assert events is not None
    event = await asyncio.wait_for(events.get(), timeout=1)
    assert event.kind == "workspace_stopped"
    assert event.event_id == "event-1"
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_private_cleanup_ack_is_completion_barrier(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup=None,
        cleanup_ack_required=True,
        cleanup_timeout_seconds=2,
    )
    await service.prepare_workspace(request)
    stopping = asyncio.create_task(service.pool.discard(request.session_key))
    events = service.controller_events()
    assert events is not None
    event = await asyncio.wait_for(events.get(), timeout=1)
    await asyncio.sleep(0)
    assert not stopping.done()
    await service.ack_workspace_cleanup(
        WorkspaceCleanupAckRequest(
            controller_id=event.controller_id,
            instance_id=event.instance_id,
            session_key=event.session_key,
            event_id=event.event_id,
            invocation_id=event.invocation_id,
        )
    )
    result = await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), timeout=1)
    assert result[0] is None
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_graceful_controller_stop_preserves_warm_cleanup_ack(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup=None,
        cleanup_ack_required=True,
        cleanup_timeout_seconds=2,
    )
    await service.prepare_workspace(request)
    stopping = asyncio.create_task(service.pool.discard(request.session_key))
    events = service.controller_events()
    assert events is not None
    event = await asyncio.wait_for(events.get(), timeout=1)
    await service.ack_workspace_cleanup(
        WorkspaceCleanupAckRequest(
            controller_id=event.controller_id,
            instance_id=event.instance_id,
            session_key=event.session_key,
            event_id=event.event_id,
            invocation_id=event.invocation_id,
        )
    )
    await asyncio.wait_for(stopping, timeout=1)
    await service.graceful_stop_controller("controller")
    assert service.can_accept_controller
    assert not service._unhealthy


@pytest.mark.asyncio
async def test_warm_release_retains_hook_budget_for_pool_stop(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        cleanup=WorkspaceHook(script="true", timeout_seconds=300),
        cleanup_ack_required=False,
    )
    await service.prepare_workspace(request)
    handle = await service.acquire(
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
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id=handle.invocation_id,
            idle_ttl_seconds=60,
        )
    )
    assert service._warm_cleanup_budget_seconds == 300
    await service.graceful_stop_controller("controller")


@pytest.mark.asyncio
async def test_private_cleanup_ack_controller_loss_wakes_native_cleanup(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup=None,
        cleanup_ack_required=True,
        cleanup_timeout_seconds=120,
    )
    await service.prepare_workspace(request)
    stopping = asyncio.create_task(service.pool.discard(request.session_key))
    events = service.controller_events()
    assert events is not None
    await asyncio.wait_for(events.get(), timeout=1)

    with pytest.raises(Exception):
        await asyncio.wait_for(service.release_controller("controller"), timeout=2)
    result = await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), timeout=1)
    assert isinstance(result[0], Exception)
    assert service.shutdown_requested


@pytest.mark.asyncio
async def test_private_cleanup_ack_delivery_margin_covers_near_budget_hook(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ach_agent.execution.service.CLEANUP_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr("ach_agent.execution.service.CLEANUP_PROCESS_MARGIN_SECONDS", 0.1)
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup=None,
        cleanup_ack_required=True,
        cleanup_timeout_seconds=0.05,
    )
    await service.prepare_workspace(request)
    stopping = asyncio.create_task(service.pool.discard(request.session_key))
    events = service.controller_events()
    assert events is not None
    event = await asyncio.wait_for(events.get(), timeout=1)
    await asyncio.sleep(0.08)
    await service.ack_workspace_cleanup(
        WorkspaceCleanupAckRequest(
            controller_id=event.controller_id,
            instance_id=event.instance_id,
            session_key=event.session_key,
            event_id=event.event_id,
            invocation_id=event.invocation_id,
        )
    )
    await asyncio.wait_for(stopping, timeout=1)
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_cancel_reclaims_idle_reservation_and_peer_survives(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare=None, remaining_seconds=5)
    await service.prepare_workspace(request)
    assert request.invocation_id in service._workspace_reservations
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
    await service.cancel("controller", "peer")
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_duplicate_cancel_joins_one_cleanup_and_waits_for_callback(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def controlled_hook(hook: object, **kwargs: object) -> None:
        if getattr(hook, "script", "") == "cleanup":
            cleanup_started.set()
            await asyncio.shield(cleanup_release.wait())

    monkeypatch.setattr("ach_agent.execution.service.run_public_hook", controlled_hook)
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "cleanup", "timeout_seconds": 2},
    )
    await service.prepare_workspace(request)
    first = asyncio.create_task(service.cancel("controller", request.invocation_id))
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    second = asyncio.create_task(service.cancel("controller", request.invocation_id))
    await asyncio.sleep(0.02)
    assert not first.done() and not second.done()
    cleanup_release.set()
    await asyncio.gather(first, second)
    assert request.invocation_id not in service._workspace_reservations
    assert not service._unhealthy
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_controller_loss_joins_reservation_cleanup_before_reconnect(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    async def controlled_hook(hook: object, **kwargs: object) -> None:
        if getattr(hook, "script", "") == "cleanup":
            cleanup_started.set()
            await asyncio.shield(cleanup_release.wait())

    monkeypatch.setattr("ach_agent.execution.service.run_public_hook", controlled_hook)
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "cleanup", "timeout_seconds": 2},
    )
    await service.prepare_workspace(request)
    release = asyncio.create_task(service.release_controller("controller"))
    await asyncio.wait_for(cleanup_started.wait(), timeout=1)
    assert not release.done()
    cleanup_release.set()
    await release
    assert service.can_accept_controller
    assert not service._unhealthy


@pytest.mark.asyncio
async def test_acquire_and_handoff_cannot_cross_pending_reservation(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    first = _prepare(tmp_path, prepare=None)
    await service.prepare_workspace(first)
    with pytest.raises(ValueError, match="pending reservation"):
        await service.acquire(
            AcquireRequest(
                controller_id="controller",
                invocation_id="other",
                lane_key=first.session_key,
                conversation_key="other",
                reuse=True,
                remaining_seconds=5,
                config=PublicEngineConfig(),
            )
        )
    assert first.invocation_id in service._workspace_reservations
    await service.cancel("controller", first.invocation_id)

    second = _prepare(tmp_path, invocation_id="second", session_key="second", prepare=None)
    await service.prepare_workspace(second)
    service._workspace_reservations[second.invocation_id].acquiring = True
    with pytest.raises(ValueError, match="acquiring"):
        await service.handoff_workspace(
            WorkspaceHandoffRequest(
                controller_id="controller",
                invocation_id=second.invocation_id,
                session_key=second.session_key,
                home=second.home,
                work_dir=second.work_dir,
                bundle_path="bundle",
                head="a" * 40,
                remaining_seconds=5,
            )
        )
    service._workspace_reservations[second.invocation_id].acquiring = False
    await service.cancel("controller", second.invocation_id)
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_cancel_blocked_reserved_acquire_has_one_cleanup_owner(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare=None)
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
    assert request.invocation_id in service._acquire_tasks

    await asyncio.wait_for(service.cancel("controller", request.invocation_id), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await acquire_task
    assert request.invocation_id not in service._workspace_reservations
    assert not service._unhealthy
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_controller_loss_cancels_blocked_reserved_acquire_once(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare=None)
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
    assert request.invocation_id in service._acquire_tasks

    await asyncio.wait_for(service.release_controller("controller"), timeout=2)
    with pytest.raises(asyncio.CancelledError):
        await acquire_task
    assert service.can_accept_controller
    assert not service._unhealthy


@pytest.mark.asyncio
async def test_reservation_rejects_duplicate_invocation_and_lane_without_mutation(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    first = _prepare(tmp_path, prepare=None)
    await service.prepare_workspace(first)
    with pytest.raises(ValueError, match="reservation"):
        await service.prepare_workspace(_prepare(tmp_path, prepare=None))
    with pytest.raises(ValueError, match="lane"):
        await service.prepare_workspace(_prepare(tmp_path, invocation_id="other", prepare=None))
    assert set(service._workspace_reservations) == {first.invocation_id}
    await service.cancel("controller", first.invocation_id)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="live",
            lane_key="live-lane",
            conversation_key="live",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(),
        )
    )
    with pytest.raises(ValueError, match="lane"):
        await service.prepare_workspace(
            _prepare(tmp_path, invocation_id="new", session_key="live-lane", prepare=None)
        )
    await service.cancel("controller", handle.invocation_id)
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_public_cleanup_failure_is_recorded_but_confirmed_stop_stays_healthy(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "exit 17", "timeout_seconds": 2},
    )
    await service.prepare_workspace(request)
    await service.pool.discard(request.session_key)
    assert service.workspace_cleanup_errors
    assert "WorkspaceHookExitFailed" in service.workspace_cleanup_errors[-1]
    assert not service._unhealthy
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_public_hook_keeps_bounded_diagnostics_in_engine_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records: list[dict[str, object]] = []

    def record(_message: str, **fields: object) -> None:
        records.append(fields)

    monkeypatch.setattr("ach_agent.engine.workspace.log.debug", record)
    await run_public_hook(
        WorkspaceHook(script="printf PREPARE-END", timeout_seconds=2),
        cwd=tmp_path,
        env={"PATH": os.environ["PATH"]},
        remaining_seconds=2,
    )
    assert any("PREPARE-END" in str(record.get("stdout")) for record in records)


@pytest.mark.asyncio
async def test_public_cleanup_timeout_is_best_effort_after_confirmed_stop(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "sleep 30", "timeout_seconds": 0.05},
    )
    await service.prepare_workspace(request)
    await service.pool.discard(request.session_key)
    assert service.workspace_cleanup_errors
    assert "WorkspaceHookTimedOut" in service.workspace_cleanup_errors[-1]
    assert not service._unhealthy
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_public_cleanup_budget_is_separate_from_native_stop_bound(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ach_agent.execution.service.CLEANUP_DEADLINE_SECONDS", 0.05)
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "sleep 0.15", "timeout_seconds": 0.4},
    )
    await service.prepare_workspace(request)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id=request.invocation_id,
            lane_key=request.session_key,
            conversation_key=request.session_key,
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(),
        )
    )
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id=request.invocation_id,
            idle_ttl_seconds=0,
        )
    )
    assert not service._unhealthy
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_native_stop_deadline_is_not_extended_by_public_hook_budget(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ach_agent.execution.service.CLEANUP_DEADLINE_SECONDS", 0.05)
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "sleep 30", "timeout_seconds": 30},
    )
    await service.prepare_workspace(request)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id=request.invocation_id,
            lane_key=request.session_key,
            conversation_key=request.session_key,
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(),
        )
    )
    fake_driver.stop_barrier = asyncio.Event()
    fake_driver.suppress_stop_cancellation = True
    started = asyncio.get_running_loop().time()
    release = asyncio.create_task(
        service.release(
            ReleaseRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id=request.invocation_id,
                idle_ttl_seconds=0,
            )
        )
    )
    await asyncio.wait_for(fake_driver.stop_started.wait(), timeout=1)
    with pytest.raises(RuntimeError, match="cleanup deadline"):
        await asyncio.wait_for(release, timeout=1)
    assert asyncio.get_running_loop().time() - started < 1
    assert service._unhealthy
    fake_driver.stop_barrier.set()
    cleanup = service._invocations.get(request.invocation_id)
    if cleanup is not None and cleanup.cleanup_task is not None:
        await asyncio.wait_for(
            asyncio.gather(cleanup.cleanup_task, return_exceptions=True), timeout=1
        )
    service._invocations.pop(request.invocation_id, None)


@pytest.mark.asyncio
async def test_cleanup_stop_failure_does_not_acknowledge_cancel(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")

    async def uncertain_hook(*args: object, **kwargs: object) -> None:
        raise OwnedProcessCleanupError("cleanup process ownership lost")

    monkeypatch.setattr("ach_agent.execution.service.run_public_hook", uncertain_hook)
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "cleanup", "timeout_seconds": 2},
    )
    await service.prepare_workspace(request)
    with pytest.raises(RuntimeError, match="cleanup process ownership lost"):
        await service.cancel("controller", request.invocation_id)
    assert service._unhealthy


@pytest.mark.asyncio
async def test_acquired_cleanup_stop_failure_does_not_acknowledge_cancel(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")

    async def uncertain_hook(*args: object, **kwargs: object) -> None:
        raise OwnedProcessCleanupError("cleanup process ownership lost")

    monkeypatch.setattr("ach_agent.execution.service.run_public_hook", uncertain_hook)
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={"script": "cleanup", "timeout_seconds": 2},
    )
    await service.prepare_workspace(request)
    handle = await service.acquire(
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
    with pytest.raises(RuntimeError, match="cleanup process ownership lost"):
        await service.cancel("controller", handle.invocation_id)
    assert service._unhealthy
    assert handle.invocation_id in service._invocations


@pytest.mark.asyncio
async def test_http_prepare_failure_reports_unconfirmed_cleanup_and_client_cancels(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})

    async def failing_hooks(hook: object, **kwargs: object) -> None:
        if getattr(hook, "script", "") == "prepare-fails":
            raise WorkspaceHookExitFailed("prepare hook failed")
        if getattr(hook, "script", "") == "cleanup-fails":
            raise OwnedProcessCleanupError("cleanup process ownership lost")

    monkeypatch.setattr("ach_agent.execution.service.run_public_hook", failing_hooks)
    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=0.5)
        try:
            await client.connect()
            request = _prepare(
                tmp_path,
                prepare={"script": "prepare-fails", "timeout_seconds": 2},
                cleanup={"script": "cleanup-fails", "timeout_seconds": 2},
            )
            with pytest.raises(ExecutionClientError):
                await client.prepare_workspace(request)
            assert service._unhealthy
            assert client._failed
            with pytest.raises(ExecutionClientError):
                await client.prepare_workspace(
                    _prepare(tmp_path, invocation_id="peer", session_key="peer", prepare=None)
                )
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_duplicate_prepare_preserves_live_cleanup_budget(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=0.5)
        try:
            await client.connect()
            request = _prepare(
                tmp_path,
                prepare=None,
                cleanup={"script": "true", "timeout_seconds": 0.4},
            )
            await client.prepare_workspace(request)
            assert client._cleanup_budgets[request.invocation_id] == 0.4
            with pytest.raises(ExecutionClientError, match="already active"):
                await client.prepare_workspace(request)
            assert client._cleanup_budgets[request.invocation_id] == 0.4
            await client.cancel("controller", request.invocation_id)
            assert request.invocation_id not in client._cleanup_budgets
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_uncertain_prepare_and_handoff_cleanup_make_service_unhealthy(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")

    async def uncertain_hook(*args: object, **kwargs: object) -> None:
        raise OwnedProcessCleanupError("detached writer survived")

    monkeypatch.setattr("ach_agent.execution.service.run_public_hook", uncertain_hook)
    with pytest.raises(OwnedProcessCleanupError):
        await service.prepare_workspace(_prepare(tmp_path))
    assert service._unhealthy

    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller-2")
    request = _prepare(tmp_path, controller_id="controller-2", prepare=None)

    async def uncertain_handoff(*args: object, **kwargs: object) -> Path:
        raise OwnedProcessCleanupError("handoff writer survived")

    monkeypatch.setattr("ach_agent.execution.service.handoff_bundle", uncertain_handoff)
    await service.prepare_workspace(request)
    with pytest.raises(OwnedProcessCleanupError):
        await service.handoff_workspace(
            WorkspaceHandoffRequest(
                controller_id="controller-2",
                invocation_id=request.invocation_id,
                session_key=request.session_key,
                home=request.home,
                work_dir=request.work_dir,
                bundle_path="bundle",
                head="a" * 40,
                remaining_seconds=5,
            )
        )
    assert service._unhealthy


def test_workspace_hook_wire_has_no_secret_or_path_fields(tmp_path: Path) -> None:
    request = _prepare(tmp_path)
    with pytest.raises(ValidationError):
        WorkspacePrepareRequest.model_validate(
            {**request.model_dump(), "secret_env": {"TOKEN": "value"}}
        )
    with pytest.raises(ValidationError):
        WorkspaceHandoffRequest.model_validate(
            {
                "controller_id": "controller",
                "invocation_id": "invocation",
                "session_key": "group/project:1",
                "home": str(tmp_path / "home"),
                "work_dir": str(tmp_path / "work"),
                "bundle_path": ".ach-handoff/bundle",
                "head": "HEAD",
                "bundle": "private-file-content",
                "remaining_seconds": 5,
            }
        )


@pytest.mark.asyncio
async def test_controller_loss_cancels_public_prepare(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare={"script": "sleep 30", "timeout_seconds": 30})
    task = asyncio.create_task(service.prepare_workspace(request))
    await asyncio.sleep(0.05)
    await service.release_controller("controller")
    with pytest.raises(BaseException):
        await task
    assert service.can_accept_controller


@pytest.mark.asyncio
async def test_caller_cancel_waits_for_prepare_reservation_cleanup(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare={"script": "sleep 30", "timeout_seconds": 30},
        remaining_seconds=10,
    )
    task = asyncio.create_task(service.prepare_workspace(request))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert request.invocation_id not in service._workspace_reservations
    assert not service._unhealthy
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_real_http_long_prepare_uses_deadline_and_failed_prepare_is_invocation_local(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=0.05)
        try:
            await client.connect()
            slow = _prepare(
                tmp_path,
                invocation_id="slow",
                prepare={"script": "sleep 0.2", "timeout_seconds": 2},
                remaining_seconds=2,
            )
            result = await client.prepare_workspace(slow)
            assert result["workspace"] == str(Path(slow.work_dir) / "group-project-1-ac555c4f")
            with pytest.raises(WorkspaceOperationFailed, match="public hook exited 17") as failure:
                await client.prepare_workspace(
                    _prepare(
                        tmp_path,
                        invocation_id="failed",
                        session_key="failed",
                        prepare={"script": "exit 17", "timeout_seconds": 2},
                    )
                )
            assert failure.value.confirmed
            peer = await client.prepare_workspace(
                _prepare(tmp_path, invocation_id="peer", session_key="peer", prepare=None)
            )
            assert peer["status"] == "ok"
            await client.cancel("controller", "slow")
            await client.cancel("controller", "peer")
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_real_http_cleanup_ack_releases_existing_pool_callback(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=0.5)
        try:
            await client.connect()
            request = _prepare(
                tmp_path,
                cleanup_ack_required=True,
                cleanup_timeout_seconds=2,
                prepare=None,
            )
            await client.prepare_workspace(request)
            stopping = asyncio.create_task(service.pool.discard(request.session_key))
            event = await asyncio.wait_for(client.next_controller_event(), timeout=1)
            await asyncio.sleep(0)
            assert not stopping.done()
            await client.ack_workspace_cleanup(event)
            await asyncio.wait_for(stopping, timeout=1)
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_real_http_registry_orders_warm_expiry_before_same_lane_prepare(
    fake_driver, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)

    async def private_cleanup(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr("ach_agent.boot.prepare.run_cleanup", private_cleanup)
    registry = PrivateCleanupRegistry()
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=1)
        try:
            await client.connect()
            old = _prepare(
                tmp_path,
                prepare=None,
                cleanup=None,
                cleanup_ack_required=True,
                cleanup_timeout_seconds=2,
            )
            old_event = MessageEvent(
                idempotency_key=old.event_id,
                session_key=old.session_key,
                channel_name=old.channel_name,
                delivery_context=old.delivery_context,
            )
            await registry.register(
                old.invocation_id,
                old_event,
                Path(old.work_dir),
                tmp_path / "scratch",
                PrepareBlock.model_validate({"script": "true"}),
            )
            await client.prepare_workspace(old)
            old_handle = await client.acquire(
                AcquireRequest(
                    controller_id="controller",
                    invocation_id=old.invocation_id,
                    lane_key=old.session_key,
                    conversation_key=old.session_key,
                    reuse=True,
                    remaining_seconds=5,
                    config=PublicEngineConfig(),
                )
            )
            await client.release(
                ReleaseRequest(
                    controller_id="controller",
                    execution_id=old_handle.execution_id,
                    invocation_id=old.invocation_id,
                    idle_ttl_seconds=0.01,
                )
            )
            for _ in range(100):
                if old.invocation_id in service._workspace_barriers:
                    break
                await asyncio.sleep(0.01)
            assert old.invocation_id in service._workspace_barriers

            new = _prepare(
                tmp_path,
                invocation_id="replacement",
                event_id="event-2",
                prepare=None,
                cleanup=None,
                cleanup_ack_required=False,
            )
            new_event = MessageEvent(
                idempotency_key=new.event_id,
                session_key=new.session_key,
                channel_name=new.channel_name,
                delivery_context=new.delivery_context,
            )
            await registry.register(
                new.invocation_id,
                new_event,
                Path(new.work_dir),
                tmp_path / "scratch",
                PrepareBlock.model_validate({"script": "true"}),
            )
            preparing = asyncio.create_task(client.prepare_workspace(new))
            await asyncio.sleep(0.05)
            assert not preparing.done()
            event = await asyncio.wait_for(client.next_controller_event(), timeout=1)
            assert await registry.handle_event(event, client.ack_workspace_cleanup)
            await asyncio.wait_for(preparing, timeout=1)
            registry.commit(new.invocation_id)
            registry.retire(new.invocation_id)
            await client.cancel("controller", new.invocation_id)
        finally:
            await registry.close()
            await client.close()


@pytest.mark.asyncio
async def test_real_http_long_cleanup_pool_keeps_priority_ack_responsive(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=1)
        try:
            await client.connect()
            private_requests = [
                _prepare(
                    tmp_path,
                    invocation_id=invocation_id,
                    session_key=invocation_id,
                    prepare=None,
                    cleanup_ack_required=True,
                    cleanup_timeout_seconds=2,
                )
                for invocation_id in ("private-one", "private-two")
            ]
            for request in private_requests:
                await client.prepare_workspace(request)
            stopping = [
                asyncio.create_task(client.cancel("controller", request.invocation_id))
                for request in private_requests
            ]
            events = [
                await asyncio.wait_for(client.next_controller_event(), timeout=1)
                for _ in private_requests
            ]
            assert all(not task.done() for task in stopping)

            peer = await client.acquire(
                AcquireRequest(
                    controller_id="controller",
                    invocation_id="peer",
                    lane_key="peer",
                    conversation_key="peer",
                    reuse=True,
                    remaining_seconds=5,
                    config=PublicEngineConfig(),
                )
            )
            streamed = [
                event
                async for event in client.turn(
                    TurnRequest(
                        controller_id="controller",
                        execution_id=peer.execution_id,
                        invocation_id=peer.invocation_id,
                        turn_id="peer-turn",
                        prompt="peer",
                        max_tool_calls=0,
                    )
                )
            ]
            assert any(event.kind == "turn_done" for event in streamed)
            await client.cancel("controller", peer.invocation_id)

            for event in events:
                await client.ack_workspace_cleanup(event)
            await asyncio.wait_for(asyncio.gather(*stopping), timeout=1)
        finally:
            await client.close()


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
            request = _prepare(tmp_path, prepare=None)
            with pytest.raises(WorkspaceOperationFailed, match="invalid workspace") as failure:
                await client.prepare_workspace(request)
            assert failure.value.confirmed
            assert request.invocation_id not in service._workspace_reservations
            assert not client._failed
            monkeypatch.undo()
            peer = await client.prepare_workspace(
                _prepare(tmp_path, invocation_id="peer", session_key="peer", prepare=None)
            )
            assert peer["status"] == "ok"
            await client.cancel("controller", "peer")
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_real_http_cancel_stops_detached_prepare_and_wakes_event_waiter(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    pid_file = tmp_path / "child.pid"
    script = (
        "python3 -c 'import os,time; p=\"" + str(pid_file) + '"; c=os.fork(); '
        '(os.setsid(), open(p,"w").write(str(os.getpid())), time.sleep(30)) '
        "if c == 0 else time.sleep(30)'"
    )
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=0.5)
        try:
            await client.connect()
            waiter = asyncio.create_task(client.next_controller_event())
            request = _prepare(
                tmp_path,
                prepare={"script": script, "timeout_seconds": 30},
                remaining_seconds=10,
            )
            operation = asyncio.create_task(client.prepare_workspace(request))
            for _ in range(100):
                if pid_file.exists():
                    break
                await asyncio.sleep(0.02)
            assert pid_file.exists()
            await client.cancel("controller", request.invocation_id)
            with pytest.raises(WorkspaceOperationFailed):
                await operation
            event = await asyncio.wait_for(waiter, timeout=2)
            assert event.controller_id == "controller"
            assert event.instance_id == service.instance_id
            assert event.invocation_id == request.invocation_id
            pid = int(pid_file.read_text())
            for _ in range(100):
                if not Path(f"/proc/{pid}").exists():
                    break
                await asyncio.sleep(0.02)
            assert not Path(f"/proc/{pid}").exists()
        finally:
            await client.close()


@pytest.mark.asyncio
async def test_controller_event_waiter_wakes_when_client_closes(fake_driver) -> None:
    client = ExecutionClient("http://execution", controller_id="controller")
    waiter = asyncio.create_task(client.next_controller_event())
    await asyncio.sleep(0)
    await client.close()
    with pytest.raises(ExecutionClientError, match="closed"):
        await waiter


@pytest.mark.asyncio
async def test_cancelled_event_waiter_does_not_drop_next_event() -> None:
    client = ExecutionClient("http://execution", controller_id="controller")
    client.instance_id = "instance"
    stale = asyncio.get_running_loop().create_future()
    stale.cancel()
    client._controller_event_waiters.add(stale)
    stop = asyncio.Event()
    event = WorkspaceStoppedEvent(
        controller_id="controller",
        instance_id="instance",
        session_key="lane",
        event_id="event",
        invocation_id="invocation",
        workspace="/workspace",
    )

    async def lines() -> AsyncIterator[bytes]:
        yield event.model_dump_json().encode()
        await stop.wait()

    monitor = asyncio.create_task(client._monitor_controller(lines()))
    await asyncio.sleep(0.02)
    stop.set()
    monitor.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await monitor
    try:
        assert (await client.next_controller_event()).invocation_id == "invocation"
    finally:
        await client.close()


@pytest.mark.skipif(os.name != "posix", reason="requires process ownership procfs")
@pytest.mark.asyncio
async def test_hook_timeout_reaps_fast_detached_setsid_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    hook = {
        "script": (
            "python3 -c 'import os,time; p=\"" + str(pid_file) + '"; c=os.fork(); '
            '(os.setsid(), open(p,"w").write(str(os.getpid())), time.sleep(30)) '
            "if c == 0 else time.sleep(30)'"
        ),
        "timeout_seconds": 0.2,
    }
    with pytest.raises(WorkspaceHookTimedOut):
        await run_public_hook(
            WorkspaceHook(**hook),
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
            remaining_seconds=2,
        )
    for _ in range(100):
        if pid_file.exists():
            pid = int(pid_file.read_text())
            if not Path(f"/proc/{pid}").exists():
                break
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    assert not Path(f"/proc/{int(pid_file.read_text())}").exists()


def _bundle(source: Path, target: Path) -> str:
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "README").write_text("approved\n")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(source), "bundle", "create", str(target), "--all"], check=True)
    return head


@pytest.mark.asyncio
async def test_handoff_preserves_workspace_root_and_rejects_destination_symlink(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    prepared = await service.prepare_workspace(_prepare(tmp_path, prepare=None))
    workspace = Path(prepared["workspace"])
    artifact_dir = workspace / ".ach-handoff"
    artifact_dir.mkdir()
    bundle = artifact_dir / "repo.bundle"
    head = _bundle(tmp_path / "source", bundle)
    root_inode = workspace.stat().st_ino
    result = await service.handoff_workspace(
        WorkspaceHandoffRequest(
            controller_id="controller",
            invocation_id="invocation",
            session_key="group/project:1",
            home=str(tmp_path / "home"),
            work_dir=str(tmp_path / "work"),
            bundle_path=".ach-handoff/repo.bundle",
            head=head,
            remaining_seconds=5,
        )
    )
    assert Path(result["workspace"]).stat().st_ino == root_inode
    assert (workspace / "repo" / "README").read_text() == "approved\n"

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "repo.bundle").write_bytes(b"not a bundle")
    artifact_dir.rmdir()
    artifact_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        await service.handoff_workspace(
            WorkspaceHandoffRequest(
                controller_id="controller",
                invocation_id="invocation",
                session_key="group/project:1",
                home=str(tmp_path / "home"),
                work_dir=str(tmp_path / "work"),
                bundle_path=".ach-handoff/repo.bundle",
                head=head,
                remaining_seconds=5,
            )
        )
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_warm_expiry_stops_native_before_public_cleanup(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={
            "script": 'printf cleanup > "$ACH_WORKSPACE/cleanup-marker"',
            "timeout_seconds": 2,
        },
    )
    prepared = await service.prepare_workspace(request)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="invocation",
            lane_key="group/project:1",
            conversation_key="repo",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(work_dir=str(tmp_path / "work"), home=str(tmp_path / "home")),
        )
    )
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="invocation",
            idle_ttl_seconds=0.02,
        )
    )
    marker = Path(prepared["workspace"]) / "cleanup-marker"
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.read_text() == "cleanup"
    assert fake_driver.stopped_servers and fake_driver.stopped_servers[0].stopped
    events = service.controller_events()
    assert events is not None
    stopped = await asyncio.wait_for(events.get(), timeout=1)
    assert stopped.kind == "workspace_stopped"
    assert stopped.controller_id == "controller"
    assert stopped.instance_id == service.instance_id
    assert stopped.session_key == "group/project:1"
    assert stopped.event_id == "event-1"
    assert stopped.invocation_id == "invocation"
    assert stopped.workspace == str(marker.parent)
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_new_prepare_cancels_stale_warm_cleanup(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    first = _prepare(
        tmp_path,
        prepare=None,
        cleanup={
            "script": 'printf first > "$ACH_WORKSPACE/cleanup-marker"',
            "timeout_seconds": 2,
        },
    )
    await service.prepare_workspace(first)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="invocation",
            lane_key="group/project:1",
            conversation_key="repo",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(work_dir=str(tmp_path / "work"), home=str(tmp_path / "home")),
        )
    )
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="invocation",
            idle_ttl_seconds=0.05,
        )
    )
    second = _prepare(
        tmp_path,
        invocation_id="invocation-2",
        event_id="event-2",
        prepare=None,
        cleanup={
            "script": 'printf second > "$ACH_WORKSPACE/cleanup-marker"',
            "timeout_seconds": 2,
        },
    )
    await service.prepare_workspace(second)
    await asyncio.sleep(0.08)
    workspace = Path(second.work_dir) / "group-project-1-ac555c4f"
    assert not (workspace / "cleanup-marker").exists()
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_execution_client_sends_only_public_hook_fields(tmp_path: Path) -> None:
    from ach_agent.boot.execution_client import ExecutionClient

    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "workspace": str(Path(_prepare(tmp_path).work_dir) / "group-project-1-ac555c4f"),
            },
            request=request,
        )

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    try:
        await client.prepare_workspace(_prepare(tmp_path))
    finally:
        await client.close()
    prepare = seen["prepare"]
    assert isinstance(prepare, dict) and str(prepare["script"]).startswith("printf")
    assert "secret_env" not in seen and "secretEnv" not in seen
    assert "private_scratch" not in seen


@pytest.mark.asyncio
async def test_execution_client_rejects_malformed_workspace_success_without_admission_loss(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"}, request=request)

    client = ExecutionClient(
        "http://execution",
        controller_id="controller",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(WorkspaceOperationFailed, match="invalid workspace"):
            await client.prepare_workspace(_prepare(tmp_path))
        assert not client._failed
    finally:
        await client.close()

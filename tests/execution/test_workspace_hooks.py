"""Shared workspace reservation and stop-ack lifecycle coverage.

Hooks execute in H. E only reserves the shared workspace, owns native lifecycle, and
emits the correlated stop notification that lets H run cleanup and acknowledge release.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import uvicorn

from ach_agent.boot.cleanup_registry import CleanupRegistry
from ach_agent.boot.execution_client import (
    ExecutionClient,
    ExecutionClientError,
    WorkspaceOperationFailed,
)
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock
from ach_agent.engine.workspace import prepare_workspace, workspace_dir
from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import (
    AcquireRequest,
    PublicEngineConfig,
    ReleaseRequest,
    TurnRequest,
    WorkspaceCleanupAckRequest,
    WorkspacePrepareRequest,
    WorkspaceStoppedEvent,
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
        "notify_on_stop": True,
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
    assert result == {"status": "ok", "workspace": str(expected)}
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
async def test_stop_notification_ack_is_release_barrier(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, cleanup_ack_required=True, cleanup_timeout_seconds=2)
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
    assert await asyncio.wait_for(stopping, timeout=1) is None
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_graceful_controller_stop_preserves_warm_cleanup_ack(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, cleanup_ack_required=True, cleanup_timeout_seconds=2)
    await service.prepare_workspace(request)
    stopping = asyncio.create_task(service.pool.discard(request.session_key))
    event = await asyncio.wait_for(service.controller_events().get(), timeout=1)  # type: ignore[union-attr]
    graceful = asyncio.create_task(service.graceful_stop_controller("controller"))
    await asyncio.sleep(0)
    assert not graceful.done()
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
    await asyncio.wait_for(graceful, timeout=1)
    assert service.can_accept_controller and not service._unhealthy


@pytest.mark.asyncio
async def test_warm_release_retains_hook_budget_for_pool_stop(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path, notify_on_stop=True, cleanup_ack_required=True, cleanup_timeout_seconds=300
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
    stopping = asyncio.create_task(service.graceful_stop_controller("controller"))
    event = await asyncio.wait_for(service.controller_events().get(), timeout=1)  # type: ignore[union-attr]
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


@pytest.mark.asyncio
async def test_private_cleanup_ack_controller_loss_wakes_native_cleanup(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, cleanup_ack_required=True, cleanup_timeout_seconds=120)
    await service.prepare_workspace(request)
    stopping = asyncio.create_task(service.pool.discard(request.session_key))
    await asyncio.wait_for(service.controller_events().get(), timeout=1)  # type: ignore[union-attr]
    with pytest.raises(Exception):
        await asyncio.wait_for(service.release_controller("controller"), timeout=2)
    result = await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), timeout=1)
    assert isinstance(result[0], Exception) and service.shutdown_requested


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
async def test_new_prepare_cancels_stale_warm_cleanup(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    first = _prepare(tmp_path)
    await service.prepare_workspace(first)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id=first.invocation_id,
            lane_key=first.session_key,
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
            idle_ttl_seconds=0.05,
        )
    )
    second = _prepare(tmp_path, invocation_id="second")
    result = await service.prepare_workspace(second)
    assert result["workspace"] == str(workspace_dir(str(tmp_path / "work"), second.session_key))
    await asyncio.sleep(0.08)
    events = service.controller_events()
    assert events is not None
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(events.get(), timeout=0.02)
    await service.cancel("controller", second.invocation_id)
    await service.release_controller("controller")


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
            request = _prepare(tmp_path, cleanup_ack_required=True, cleanup_timeout_seconds=2)
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
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    app = create_execution_app(service)
    registry = CleanupRegistry()
    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=1)
        try:
            await client.connect()
            old = _prepare(
                tmp_path,
                cleanup_ack_required=True,
                cleanup_timeout_seconds=2,
            )
            old_event = MessageEvent(
                idempotency_key=old.event_id,
                session_key=old.session_key,
                channel_name="review",
                delivery_context={},
            )
            await registry.register(
                old.invocation_id,
                old_event,
                Path(old.work_dir),
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
                cleanup_ack_required=False,
            )
            new_event = MessageEvent(
                idempotency_key=new.event_id,
                session_key=new.session_key,
                channel_name="review",
                delivery_context={},
            )
            await registry.register(
                new.invocation_id,
                new_event,
                Path(new.work_dir),
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
            requests = [
                _prepare(
                    tmp_path,
                    invocation_id=invocation_id,
                    session_key=invocation_id,
                    cleanup_ack_required=True,
                    cleanup_timeout_seconds=2,
                )
                for invocation_id in ("private-one", "private-two")
            ]
            for request in requests:
                await client.prepare_workspace(request)
            stopping = [
                asyncio.create_task(client.cancel("controller", request.invocation_id))
                for request in requests
            ]
            events = [
                await asyncio.wait_for(client.next_controller_event(), timeout=1) for _ in requests
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

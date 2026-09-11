from __future__ import annotations

import asyncio

import pytest

from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import (
    AcquireRequest,
    PublicEngineConfig,
    ReleaseRequest,
    SessionOperation,
    TurnRequest,
)


def _acquire() -> AcquireRequest:
    return AcquireRequest(
        controller_id="controller",
        invocation_id="inv",
        lane_key="lane",
        conversation_key="repo",
        reuse=True,
        remaining_seconds=5,
        config=PublicEngineConfig(),
    )


@pytest.mark.asyncio
async def test_turns_keep_current_native_ref_and_resolve_once(fake_driver):
    driver = fake_driver
    service = ExecutionService(driver, {})
    handle = await service.acquire(_acquire())
    for turn_id in ("main", "wrap", "repair"):
        events = [
            event
            async for event in service.turn(
                TurnRequest(
                    controller_id="controller",
                    execution_id=handle.execution_id,
                    invocation_id="inv",
                    turn_id=turn_id,
                    prompt="p",
                    max_tool_calls=0,
                )
            )
        ]
        assert events[-1].kind == "turn_done"
    assert driver.resolved_conversations == [("repo", True)]
    assert driver.turn_session_refs == ["native-ref"] * 3
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            idle_ttl_seconds=0,
        )
    )


@pytest.mark.asyncio
async def test_cancel_stops_running_turn(fake_driver):
    driver = fake_driver
    driver.turn_barrier = asyncio.Event()
    service = ExecutionService(driver, {})
    handle = await service.acquire(_acquire())
    stream = service.turn(
        TurnRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            turn_id="main",
            prompt="p",
            max_tool_calls=0,
        )
    )
    assert (await stream.__anext__()).kind == "session_resolved"
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await service.cancel("controller", "inv")
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_invocation_deadline_cleans_idle_native_server(fake_driver):
    service = ExecutionService(fake_driver, {})
    request = _acquire().model_copy(update={"remaining_seconds": 0.03})
    await service.acquire(request)
    await asyncio.sleep(0.08)
    assert fake_driver.stopped
    with pytest.raises(ValueError, match="unknown invocation"):
        await service.cancel("controller", "inv")


@pytest.mark.asyncio
async def test_resolution_is_bounded_by_invocation_deadline(fake_driver):
    fake_driver.resolve_barrier = asyncio.Event()
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire().model_copy(update={"remaining_seconds": 0.03}))
    stream = service.turn(
        TurnRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            turn_id="main",
            prompt="p",
            max_tool_calls=0,
        )
    )
    with pytest.raises(TimeoutError):
        await stream.__anext__()
    await asyncio.sleep(0.02)
    assert fake_driver.stopped


@pytest.mark.asyncio
async def test_failed_turn_without_output_has_no_unhandled_wake_error(fake_driver):
    errors = []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: errors.append(context))
    fake_driver.run_error = RuntimeError("native turn failed")
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    events = [
        event
        async for event in service.turn(
            TurnRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                turn_id="main",
                prompt="p",
                max_tool_calls=0,
            )
        )
    ]
    assert events[-1].kind == "error"
    await asyncio.sleep(0)
    assert errors == []


@pytest.mark.asyncio
async def test_cancel_during_resolution_prevents_native_turn(fake_driver):
    fake_driver.resolve_barrier = asyncio.Event()
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    stream = service.turn(
        TurnRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            turn_id="main",
            prompt="p",
            max_tool_calls=0,
        )
    )
    task = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    await service.cancel("controller", "inv")
    with pytest.raises(asyncio.CancelledError):
        await task
    assert fake_driver.turn_session_refs == []


@pytest.mark.asyncio
async def test_cleanup_failure_marks_service_unhealthy(fake_driver):
    service = ExecutionService(fake_driver, {})
    await service.acquire(_acquire())
    fake_driver.stop_error = RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="stop failed"):
        await service.cancel("controller", "inv")
    with pytest.raises(RuntimeError, match="unhealthy"):
        await service.acquire(_acquire().model_copy(update={"invocation_id": "other"}))


@pytest.mark.asyncio
async def test_release_cleanup_failure_marks_service_unhealthy(fake_driver):
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    fake_driver.stop_error = RuntimeError("stop failed")
    with pytest.raises(RuntimeError, match="stop failed"):
        await service.release(
            ReleaseRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                idle_ttl_seconds=0,
            )
        )
    with pytest.raises(RuntimeError, match="unhealthy"):
        await service.acquire(_acquire().model_copy(update={"invocation_id": "other"}))


@pytest.mark.asyncio
async def test_missing_native_binary_keeps_service_healthy(tmp_path):
    from ach_agent.engine.lifecycle import NativeLaunchFailed
    from ach_agent.engine.opencode.driver import OpencodeDriver

    service = ExecutionService(OpencodeDriver(), {})
    request = _acquire().model_copy(
        update={
            "config": PublicEngineConfig(
                binary_path=str(tmp_path / "does-not-exist"),
                home=str(tmp_path),
                work_dir=str(tmp_path),
            )
        }
    )
    with pytest.raises(NativeLaunchFailed):
        await service.acquire(request)
    assert not service._unhealthy
    assert request.invocation_id not in service._acquiring


@pytest.mark.asyncio
async def test_config_conversion_failure_releases_acquiring_reservation(fake_driver, monkeypatch):
    service = ExecutionService(fake_driver, {})

    def fail_conversion(_public):
        raise ValueError("invalid native config")

    monkeypatch.setattr("ach_agent.execution.service._engine_config", fail_conversion)
    with pytest.raises(ValueError, match="invalid native config"):
        await service.acquire(_acquire())
    assert "inv" not in service._acquiring


@pytest.mark.asyncio
async def test_rejected_concurrent_turn_does_not_cancel_first(fake_driver):
    fake_driver.turn_barrier = asyncio.Event()
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    first = service.turn(
        TurnRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            turn_id="one",
            prompt="p",
            max_tool_calls=0,
        )
    )
    assert (await first.__anext__()).kind == "session_resolved"
    with pytest.raises(ValueError, match="turn already running"):
        await service.turn(
            TurnRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                turn_id="two",
                prompt="p",
                max_tool_calls=0,
            )
        ).__anext__()
    await service.cancel("controller", "inv")
    await first.aclose()


@pytest.mark.asyncio
async def test_cancel_after_resolution_event_rejects_resumption(fake_driver):
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    stream = service.turn(
        TurnRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            turn_id="one",
            prompt="p",
            max_tool_calls=0,
        )
    )
    assert (await stream.__anext__()).kind == "session_resolved"
    await service.cancel("controller", "inv")
    with pytest.raises(ValueError, match="terminal"):
        await stream.__anext__()


@pytest.mark.asyncio
async def test_compact_reserves_invocation_against_new_turn(fake_driver):
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    events = [
        event
        async for event in service.turn(
            TurnRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                turn_id="one",
                prompt="p",
                max_tool_calls=0,
            )
        )
    ]
    assert events[-1].kind == "turn_done"
    fake_driver.compact_barrier = asyncio.Event()
    compact = asyncio.create_task(
        service.session_op(
            SessionOperation(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                operation="compact",
            )
        )
    )
    await asyncio.sleep(0)
    with pytest.raises(ValueError, match="maintenance"):
        await service.turn(
            TurnRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                turn_id="two",
                prompt="p",
                max_tool_calls=0,
            )
        ).__anext__()
    await service.cancel("controller", "inv")
    with pytest.raises(asyncio.CancelledError):
        await compact


@pytest.mark.asyncio
async def test_cancelled_session_op_joins_shielded_maintenance(fake_driver):
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    events = [
        event
        async for event in service.turn(
            TurnRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                turn_id="one",
                prompt="p",
                max_tool_calls=0,
            )
        )
    ]
    assert events[-1].kind == "turn_done"

    fake_driver.compact_barrier = asyncio.Event()
    operation = asyncio.create_task(
        service.session_op(
            SessionOperation(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                operation="compact",
            )
        )
    )
    await asyncio.wait_for(fake_driver.compact_started.wait(), timeout=1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    inv = service._invocations["inv"]
    assert not inv.maintenance_active
    assert inv.maintenance_task is None
    assert fake_driver.compact_cancelled.is_set()

    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            idle_ttl_seconds=0,
        )
    )


@pytest.mark.asyncio
async def test_release_reserves_invocation_before_awaiting_pool_cleanup(fake_driver):
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    fake_driver.stop_barrier = asyncio.Event()
    release = asyncio.create_task(
        service.release(
            ReleaseRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                idle_ttl_seconds=0,
            )
        )
    )
    await asyncio.wait_for(fake_driver.stop_started.wait(), timeout=1)

    with pytest.raises(ValueError, match="terminal"):
        await service.turn(
            TurnRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                turn_id="late",
                prompt="p",
                max_tool_calls=0,
            )
        ).__anext__()

    fake_driver.stop_barrier.set()
    await release


@pytest.mark.asyncio
async def test_cancelled_release_waits_for_owned_cleanup(fake_driver):
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    fake_driver.stop_barrier = asyncio.Event()
    release = asyncio.create_task(
        service.release(
            ReleaseRequest(
                controller_id="controller",
                execution_id=handle.execution_id,
                invocation_id="inv",
                idle_ttl_seconds=0,
            )
        )
    )
    await asyncio.wait_for(fake_driver.stop_started.wait(), timeout=1)
    release.cancel()
    await asyncio.sleep(0)
    assert not release.done(), "release must join cleanup before reporting cancellation"

    fake_driver.stop_barrier.set()
    with pytest.raises(asyncio.CancelledError):
        await release
    assert fake_driver.stopped
    assert "inv" not in service._invocations

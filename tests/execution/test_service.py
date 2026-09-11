from __future__ import annotations

import asyncio
import contextlib
import sys
import uuid
from pathlib import Path

import pytest

from ach_agent.engine.base.events import OpenCodeUsage
from ach_agent.execution.service import ExecutionService, OutputLimitExceeded
from ach_agent.execution.wire import (
    AcquireRequest,
    PublicEngineConfig,
    ReleaseRequest,
    SessionOperation,
    TurnRequest,
)


class _RealProcessDriver:
    """Execution driver whose server is a real owned process for cleanup acceptance."""

    engine_type = "opencode"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.process = None
        self.processes = []
        self.servers = []
        self.ready_path = Path("/tmp") / f"ach-test-native-ready-{uuid.uuid4().hex}"

    def skills_dir(self, home: Path) -> Path:
        return home

    async def launch(self, cfg, session_key):
        from ach_agent.engine.lifecycle import ManagedServer

        self.ready_path.unlink(missing_ok=True)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "ach_agent.engine.process_supervisor",
            "--",
            sys.executable,
            "-c",
            "import signal,sys,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "Path(sys.argv[1]).write_text('ready'); time.sleep(30)",
            str(self.ready_path),
            start_new_session=True,
        )
        server = ManagedServer(port=0)
        server.register_process(process, protect_root=True)
        self.process = process
        self.processes.append(process)
        self.servers.append(server)
        deadline = asyncio.get_running_loop().time() + 2.0
        while not self.ready_path.exists() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        if not self.ready_path.exists():
            await server.stop()
            raise RuntimeError("native test process did not become ready")
        return server

    async def health(self, server) -> bool:
        return server.is_alive()

    async def resolve_session(self, server, **kwargs) -> str:
        return "real-session"

    async def run_turn(self, server, **kwargs):
        self.started.set()
        await asyncio.sleep(3600)
        raise AssertionError("turn unexpectedly returned")

    async def discard_session(self, server, session_ref) -> None:
        return None

    async def compact_session(self, server, session_ref) -> None:
        return None

    async def stop(self, server) -> None:
        await server.stop()

    async def cleanup(self) -> None:
        for server in self.servers:
            with contextlib.suppress(BaseException):
                await server.stop()
        self.ready_path.unlink(missing_ok=True)


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
async def test_controller_release_joins_real_process_before_new_controller() -> None:
    """Controller loss cancels a real turn and reaps its native process before reclaim."""
    driver = _RealProcessDriver()
    service = ExecutionService(driver, {})
    await service.claim_controller("controller")
    request = _acquire().model_copy(update={"controller_id": "controller", "remaining_seconds": 30})
    handle = await service.acquire(request)
    stream = service.turn(
        TurnRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            turn_id="turn",
            prompt="p",
            max_tool_calls=0,
        )
    )
    await stream.__anext__()
    turn_task = asyncio.create_task(stream.__anext__())
    try:
        await asyncio.wait_for(driver.started.wait(), timeout=1.0)
        await service.release_controller("controller")
        assert driver.process is not None and driver.process.returncode is not None
        await service.claim_controller("new-controller")
    finally:
        turn_task.cancel()
        await asyncio.gather(turn_task, return_exceptions=True)
        await driver.cleanup()


@pytest.mark.asyncio
async def test_cancel_supervised_term_resistant_process_keeps_other_execution_healthy() -> None:
    """Cancellation escalates within the service deadline without cross-killing a peer."""
    driver = _RealProcessDriver()
    service = ExecutionService(driver, {})
    await service.claim_controller("controller")
    first = await service.acquire(_acquire().model_copy(update={"remaining_seconds": 30}))
    _second = await service.acquire(
        _acquire().model_copy(
            update={
                "invocation_id": "inv-two",
                "lane_key": "lane-two",
                "controller_id": "controller",
                "remaining_seconds": 30,
            }
        )
    )
    stream = service.turn(
        TurnRequest(
            controller_id="controller",
            execution_id=first.execution_id,
            invocation_id="inv",
            turn_id="turn",
            prompt="p",
            max_tool_calls=0,
        )
    )
    await stream.__anext__()
    turn_task = asyncio.create_task(stream.__anext__())
    try:
        await asyncio.wait_for(driver.started.wait(), timeout=1.0)
        started = asyncio.get_running_loop().time()
        await service.cancel("controller", "inv")
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed >= 4.5
        assert elapsed < 9.0
        assert driver.processes[0].returncode is not None
        assert driver.processes[1].returncode is None
        assert not service._unhealthy
        await service.cancel("controller", "inv-two")
        await service.release_controller("controller")
        await service.claim_controller("new-controller")
    finally:
        turn_task.cancel()
        await asyncio.gather(turn_task, return_exceptions=True)
        await driver.cleanup()


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
        invocation = service._invocations["inv"]
        assert invocation.buffered_events == 0
        assert invocation.buffered_bytes == 0
        assert invocation.leased_bytes == 0
        assert service._queued_stream_bytes == 0
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
async def test_turn_done_stats_normalize_native_usage_dataclass(fake_driver):
    fake_driver.usage = OpenCodeUsage(
        session_id="native-session",
        message_id="message",
        input_tokens=11,
        output_tokens=7,
        cache_read=3,
        cache_write=2,
        cost=0.125,
        duration_ms=42,
    )
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

    done = events[-1]
    assert done.kind == "turn_done"
    assert done.payload["stats"]["usage"]["input_tokens"] == 11
    assert done.payload["stats"]["usage"]["duration_ms"] == 42
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            idle_ttl_seconds=0,
        )
    )


@pytest.mark.asyncio
async def test_output_overflow_cancels_only_the_affected_invocation(fake_driver):
    fake_driver.text_chunks = ["x" * 700_000] * 8
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

    with pytest.raises(OutputLimitExceeded, match="4 MiB"):
        await anext(stream)
        while True:
            await anext(stream)

    assert fake_driver.stopped
    assert "inv" not in service._invocations


@pytest.mark.asyncio
async def test_output_overflow_does_not_block_another_execution(fake_driver):
    fake_driver.text_chunks_by_conversation["slow"] = ["x" * 700_000] * 8
    service = ExecutionService(fake_driver, {})
    slow = await service.acquire(
        _acquire().model_copy(update={"invocation_id": "slow-inv", "conversation_key": "slow"})
    )

    fast = await service.acquire(
        _acquire().model_copy(update={"invocation_id": "fast-inv", "conversation_key": "fast"})
    )

    async def collect(handle, invocation_id, conversation_key):
        return [
            event
            async for event in service.turn(
                TurnRequest(
                    controller_id="controller",
                    execution_id=handle.execution_id,
                    invocation_id=invocation_id,
                    turn_id="main",
                    prompt=conversation_key,
                    max_tool_calls=0,
                )
            )
        ]

    slow_task = asyncio.create_task(collect(slow, "slow-inv", "slow"))
    fast_task = asyncio.create_task(collect(fast, "fast-inv", "fast"))
    slow_result, fast_result = await asyncio.gather(slow_task, fast_task, return_exceptions=True)

    assert isinstance(slow_result, OutputLimitExceeded)
    assert fast_result[-1].kind == "turn_done"
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=fast.execution_id,
            invocation_id="fast-inv",
            idle_ttl_seconds=0,
        )
    )


@pytest.mark.asyncio
async def test_fast_reader_can_drain_more_than_256_small_records(fake_driver):
    fake_driver.text_chunks = ["x"] * 300
    fake_driver.yield_between_text_chunks = True
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

    assert len([event for event in events if event.kind == "text"]) == 300
    assert events[-1].kind == "turn_done"
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
async def test_invocation_deadline_marks_stalled_cleanup_unhealthy(fake_driver, monkeypatch):
    monkeypatch.setattr("ach_agent.execution.service.CLEANUP_DEADLINE_SECONDS", 0.02)
    service = ExecutionService(fake_driver, {})
    await service.acquire(_acquire().model_copy(update={"remaining_seconds": 0.01}))
    fake_driver.stop_barrier = asyncio.Event()
    fake_driver.suppress_stop_cancellation = True

    try:
        await asyncio.wait_for(fake_driver.stop_started.wait(), timeout=1)
        await asyncio.sleep(0.05)
        assert service.shutdown_requested
        assert service._unhealthy
        assert not service.can_accept_controller
        with pytest.raises(RuntimeError, match="unhealthy"):
            await service.claim_controller("replacement")
    finally:
        fake_driver.stop_barrier.set()
        cleanup = service._invocations.get("inv")
        if cleanup is not None and cleanup.cleanup_task is not None:
            await asyncio.wait_for(asyncio.shield(cleanup.cleanup_task), timeout=1)


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
    invocation = service._invocations["inv"]
    assert invocation.buffered_events == 0
    assert invocation.buffered_bytes == 0
    assert invocation.leased_bytes == 0
    assert service._queued_stream_bytes == 0
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


@pytest.mark.asyncio
async def test_concurrent_cancel_joins_existing_cleanup(fake_driver):
    service = ExecutionService(fake_driver, {})
    await service.acquire(_acquire())
    fake_driver.stop_barrier = asyncio.Event()
    first = asyncio.create_task(service.cancel("controller", "inv"))
    await asyncio.wait_for(fake_driver.stop_started.wait(), timeout=1)

    second = asyncio.create_task(service.cancel("controller", "inv"))
    await asyncio.sleep(0)
    assert not second.done()
    fake_driver.stop_barrier.set()
    await first
    await second


@pytest.mark.asyncio
async def test_warm_expiry_failure_marks_service_unhealthy(fake_driver):
    service = ExecutionService(fake_driver, {})
    handle = await service.acquire(_acquire())
    fake_driver.stop_error = RuntimeError("warm stop failed")
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="inv",
            idle_ttl_seconds=0.01,
        )
    )
    await asyncio.sleep(0.05)
    assert service._unhealthy
    assert service.shutdown_requested


@pytest.mark.asyncio
async def test_cleanup_timeout_closes_admission_when_stop_suppresses_cancel(
    fake_driver, monkeypatch
):
    monkeypatch.setattr("ach_agent.execution.service.CLEANUP_DEADLINE_SECONDS", 0.03)
    service = ExecutionService(fake_driver, {})
    await service.acquire(_acquire())
    fake_driver.stop_barrier = asyncio.Event()
    fake_driver.suppress_stop_cancellation = True
    deadline = asyncio.create_task(service.cancel("controller", "inv"))
    await asyncio.wait_for(fake_driver.stop_started.wait(), timeout=1)
    await asyncio.sleep(0.08)

    assert service.shutdown_requested
    assert service._unhealthy
    assert not service.can_accept_controller
    with pytest.raises(RuntimeError, match="unhealthy"):
        await service.claim_controller("new-controller")
    with pytest.raises(RuntimeError, match="cleanup deadline"):
        await deadline
    fake_driver.stop_barrier.set()

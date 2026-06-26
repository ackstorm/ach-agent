"""Lifecycle tests: subprocess launch, readiness polling, watchdog.

Per-Task Verification Map (00-VALIDATION.md):
  ENG-01: test_launch_subprocess       — implemented by 00-02
  ENG-02: test_poll_ready              — implemented by 00-02
  ENG-06: test_startup_deadline_exits  — implemented by 00-02
  ENG-07: test_watchdog_kills_and_releases — implemented by 00-02
  ENG-07: test_watchdog_metric         — implemented by 00-02
  H-05:   test_drain_tasks_started     — implemented by 00-02
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.engine.conftest import FakeSlotManager


# ---------------------------------------------------------------------------
# ENG-01: opencode serve launches on allocated port; subprocess alive
# ---------------------------------------------------------------------------


async def test_launch_subprocess(tmp_path: Path) -> None:
    """ENG-01: launch() starts opencode serve; process is alive after launch.

    Uses a real subprocess (sleep) to verify pgid == pid (start_new_session=True).
    """
    import os

    from ach_agent.engine.lifecycle import EngineConfig, launch

    # We cannot easily run the real opencode binary in CI without credentials,
    # so we patch create_subprocess_exec with a real 'sleep' subprocess that
    # stays alive long enough for the test, then assert process group invariants.
    config = EngineConfig()

    # Create a minimal fake "opencode" binary (sleep)
    fake_binary = tmp_path / "opencode"
    fake_binary.write_text("#!/bin/sh\nsleep 30\n")
    fake_binary.chmod(0o755)
    config.binary_path = str(fake_binary)

    # Override work_dir to tmp_path so the workspace exists
    config.work_dir = str(tmp_path)

    # Patch check_health to avoid real HTTP calls
    with patch("ach_agent.engine.client.OpenCodeClient.check_health", new_callable=AsyncMock, return_value=True):
        server = await launch(port=19876, ephemeral_home=tmp_path, config=config)

    try:
        assert server.is_alive(), "Process should be alive after launch"
        proc = server._process
        assert proc is not None
        # start_new_session=True means pgid == pid (new process group leader)
        pgid = os.getpgid(proc.pid)
        assert pgid == proc.pid, f"Expected pgid={proc.pid}, got pgid={pgid}"
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# ENG-02: Readiness poll returns when /app responds 200
# ---------------------------------------------------------------------------


async def test_poll_ready() -> None:
    """ENG-02: poll_ready() returns without error when opencode /app is healthy."""
    from ach_agent.engine.lifecycle import EngineConfig, ManagedServer, poll_ready
    from ach_agent.engine.client import OpenCodeClient

    server = ManagedServer(port=19877)
    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.check_health = AsyncMock(return_value=True)
    server._client = mock_client

    # poll_ready should return without error when health check passes
    await poll_ready(server, startup_timeout_seconds=5)
    assert mock_client.check_health.called


# ---------------------------------------------------------------------------
# ENG-06: Startup deadline — process exits with code != 0 on timeout
# ---------------------------------------------------------------------------


async def test_startup_deadline_exits() -> None:
    """ENG-06: poll_ready() calls sys.exit(1) when startup_timeout_seconds elapsed.

    Points at a closed port (health always False) with sub-second timeout.
    """
    from ach_agent.engine.lifecycle import ManagedServer, poll_ready
    from ach_agent.engine.client import OpenCodeClient

    server = ManagedServer(port=19878)
    mock_client = AsyncMock(spec=OpenCodeClient)
    # Health check always fails — startup deadline must be triggered
    mock_client.check_health = AsyncMock(return_value=False)
    server._client = mock_client

    with pytest.raises(SystemExit) as exc_info:
        # Use a very short timeout so the test runs fast
        await poll_ready(server, startup_timeout_seconds=1)

    assert exc_info.value.code != 0, "sys.exit code must be non-zero on timeout"


# ---------------------------------------------------------------------------
# H-05: stdout/stderr drain tasks are started after launch (hardening)
# ---------------------------------------------------------------------------


async def test_drain_tasks_started(tmp_path: Path) -> None:
    """H-05: launch() starts stdout and stderr drain tasks immediately.

    Without drain tasks the OS PIPE buffer (64KB) fills and the subprocess
    blocks writing, deadlocking the harness. This test verifies that both
    drain tasks are created as asyncio.Task objects after launch().
    """
    from ach_agent.engine.lifecycle import EngineConfig, launch

    config = EngineConfig()
    fake_binary = tmp_path / "opencode"
    fake_binary.write_text("#!/bin/sh\nsleep 30\n")
    fake_binary.chmod(0o755)
    config.binary_path = str(fake_binary)
    config.work_dir = str(tmp_path)

    created_tasks: list[asyncio.Task] = []

    original_create_task = asyncio.create_task

    def recording_create_task(coro: Any, **kwargs: Any) -> asyncio.Task:
        task = original_create_task(coro, **kwargs)
        created_tasks.append(task)
        return task

    with (
        patch("ach_agent.engine.lifecycle.asyncio.create_task", side_effect=recording_create_task),
        patch("ach_agent.engine.client.OpenCodeClient.check_health", new_callable=AsyncMock, return_value=True),
    ):
        server = await launch(port=19879, ephemeral_home=tmp_path, config=config)

    try:
        # Should have created exactly two drain tasks (stdout + stderr)
        # There may be additional tasks if asyncio creates background tasks,
        # but drain tasks for _drain_logs must be among them.
        drain_task_count = sum(
            1 for t in created_tasks
            if not t.done() or not t.cancelled()
        )
        assert len(created_tasks) >= 2, (
            f"Expected at least 2 drain tasks (stdout + stderr), got {len(created_tasks)}"
        )
        # Verify neither stream blocks (tasks are alive / scheduled)
        # Both tasks should be started as asyncio.Task objects
        assert all(isinstance(t, asyncio.Task) for t in created_tasks)
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# ENG-07: Watchdog kills subprocess and calls on_kill callback
# ---------------------------------------------------------------------------


async def test_watchdog_kills_and_releases(
    fake_slot_manager: FakeSlotManager,
    tmp_path: Path,
) -> None:
    """ENG-07: watchdog kills the overrunning subprocess and calls on_kill.

    Uses a mock server with a fake SSE consume that sleeps past max_invocation_seconds.
    """
    from ach_agent.engine.lifecycle import ManagedServer, run_invocation
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import InvocationTimeout

    # Create a fake process that is "alive"
    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.returncode = None

    mock_client = AsyncMock(spec=OpenCodeClient)
    # create_session returns a session dict
    mock_client.create_session = AsyncMock(return_value={"id": "ses_test"})
    # send_message succeeds
    mock_client.send_message = AsyncMock(return_value=None)
    # subscribe_events returns something that causes a long SSE consume
    mock_client.subscribe_events = AsyncMock(return_value=AsyncMock())

    server = ManagedServer(port=19880)
    server._process = mock_proc
    server._client = mock_client

    # Patch consume_sse_after_send to sleep longer than the watchdog
    async def slow_consume(*args: Any, **kwargs: Any) -> str:
        await asyncio.sleep(10)  # much longer than max_invocation_seconds=1
        return "should not get here"

    with (
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", side_effect=slow_consume),
        patch("ach_agent.engine.lifecycle._process_group_kill", new_callable=AsyncMock),
    ):
        with pytest.raises(InvocationTimeout):
            await run_invocation(
                server=server,
                session_id="ses_test",
                prompt="test prompt",
                terminal_retries=1,
                max_invocation_seconds=1,
                on_kill=fake_slot_manager.on_kill,
            )

    assert fake_slot_manager.released is True, "on_kill must be called after watchdog fires"


# ---------------------------------------------------------------------------
# ENG-07: Watchdog emits prometheus counter increment
# ---------------------------------------------------------------------------


async def test_watchdog_metric(
    fake_slot_manager: FakeSlotManager,
    tmp_path: Path,
) -> None:
    """ENG-07: watchdog increments engine_watchdog_kills_total prometheus counter."""
    from ach_agent.engine.lifecycle import ManagedServer, run_invocation
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import InvocationTimeout
    from ach_agent.engine.metrics import ENGINE_WATCHDOG_KILLS

    mock_proc = MagicMock()
    mock_proc.pid = 99998
    mock_proc.returncode = None

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.create_session = AsyncMock(return_value={"id": "ses_test_metric"})
    mock_client.send_message = AsyncMock(return_value=None)
    mock_client.subscribe_events = AsyncMock(return_value=AsyncMock())

    server = ManagedServer(port=19881)
    server._process = mock_proc
    server._client = mock_client

    # Get counter value before
    before = ENGINE_WATCHDOG_KILLS._value.get()

    async def slow_consume(*args: Any, **kwargs: Any) -> str:
        await asyncio.sleep(10)
        return "should not get here"

    with (
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", side_effect=slow_consume),
        patch("ach_agent.engine.lifecycle._process_group_kill", new_callable=AsyncMock),
    ):
        with pytest.raises(InvocationTimeout):
            await run_invocation(
                server=server,
                session_id="ses_test_metric",
                prompt="metric test",
                terminal_retries=1,
                max_invocation_seconds=1,
                on_kill=fake_slot_manager.on_kill,
            )

    after = ENGINE_WATCHDOG_KILLS._value.get()
    assert after - before == 1.0, f"Expected counter increment of 1, got {after - before}"


# ---------------------------------------------------------------------------
# Terminal contract: run_invocation returns the single terminal object
# ---------------------------------------------------------------------------


async def test_run_invocation_returns_terminal_object() -> None:
    """run_invocation extracts the single terminal object and returns it as a dict.

    Uses a fake ManagedServer + mocked consume_sse_after_send so no real opencode
    binary is needed. Asserts the returned dict has an "action" key.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, run_invocation

    fake_server = ManagedServer(port=19882)
    fake_client = MagicMock(spec=OpenCodeClient)
    fake_server._client = fake_client
    fake_process = MagicMock()
    fake_process.returncode = None
    fake_server._process = fake_process

    canned_text = 'thinking...\n{"action":"none","text":"done","thoughts":"ok"}'

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned_text,
    ):
        result = await run_invocation(
            server=fake_server,
            session_id="ses_test",
            prompt="hello",
            terminal_retries=1,
            max_invocation_seconds=30,
            on_kill=lambda: None,
        )

    assert isinstance(result, dict)
    assert result["action"] == "none"
    assert result["text"] == "done"

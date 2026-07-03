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
        server = await launch(port=19876, ephemeral_home=tmp_path, config=config, session_key="k1")

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
        server = await launch(port=19879, ephemeral_home=tmp_path, config=config, session_key="k1")

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
        )

    assert isinstance(result, dict)
    assert result["action"] == "none"
    assert result["text"] == "done"


# ---------------------------------------------------------------------------
# Text-part reduction: suffix streaming + tool sink + separator between parts
# ---------------------------------------------------------------------------


async def test_consume_streams_suffix_separates_parts_and_emits_tools() -> None:
    """consume_sse_after_send: stream new text suffix per snapshot, insert a blank line
    between distinct text parts, and fire on_tool once per (part_id, status)."""
    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import (
        OpenCodeSessionIdle,
        OpenCodeStreamReady,
        OpenCodeTextUpdate,
        OpenCodeToolUpdate,
        ToolStateRunning,
    )
    from ach_agent.engine.lifecycle import consume_sse_after_send

    # Scripted typed events: subscription confirmed first (server.connected), then part A
    # grows, a tool runs (twice → deduped), part B begins.
    scripted = [
        OpenCodeStreamReady(),
        OpenCodeTextUpdate("s", "prtA", "msgA", "Hola"),
        OpenCodeTextUpdate("s", "prtA", "msgA", "Hola mundo"),
        OpenCodeToolUpdate("s", "prtT", "msgA", "mcp-x_auth_wait", "cid", ToolStateRunning()),
        OpenCodeToolUpdate("s", "prtT", "msgA", "mcp-x_auth_wait", "cid", ToolStateRunning()),
        OpenCodeTextUpdate("s", "prtB", "msgA", "Adios"),
        OpenCodeSessionIdle("s"),
    ]

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        for e in scripted:
            await queue.put(e)

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]

    streamed: list[str] = []
    tools: list[OpenCodeToolUpdate] = []

    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        text = await consume_sse_after_send(
            client, "ses", "hi", on_text=streamed.append, on_tool=tools.append
        )

    assert text == "Hola mundo\n\nAdios", "suffix streamed + blank line between parts"
    assert "".join(streamed) == "Hola mundo\n\nAdios"
    assert len(tools) == 1, "tool running fired once (deduped per part_id+status)"
    assert tools[0].tool_name == "mcp-x_auth_wait"


async def test_consume_releases_resp_on_early_sse_error() -> None:
    """CR-03: if the first SSE item is an exception (early stream error), the finally still
    cancels the reader and releases resp — the readiness gate must not leak past the try."""
    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import consume_sse_after_send

    boom = RuntimeError("sse stream died on connect")

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        await queue.put(boom)  # first (and only) item is an error

    resp = MagicMock(release=AsyncMock())
    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=resp)  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]

    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        with pytest.raises(RuntimeError, match="sse stream died"):
            await consume_sse_after_send(client, "ses", "hi")

    resp.release.assert_awaited()  # leak guard: resp released despite the early raise
    client.send_message.assert_not_awaited()  # gate raised before the prompt was sent


async def test_consume_filters_user_message_echo() -> None:
    """Text belonging to user-echo message IDs is filtered from the accumulated reply.

    (Live-path coverage preserved from the retired consume_sse_to_completion tests.)
    """
    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import (
        OpenCodeSessionIdle,
        OpenCodeStreamReady,
        OpenCodeTextUpdate,
        OpenCodeUserMessage,
    )
    from ach_agent.engine.lifecycle import consume_sse_after_send

    scripted = [
        OpenCodeStreamReady(),
        OpenCodeUserMessage("s", "msg_user"),
        OpenCodeTextUpdate("s", "prtU", "msg_user", "echoed user prompt"),  # filtered
        OpenCodeTextUpdate("s", "prtA", "msg_asst", '{"actions":[]}'),  # kept
        OpenCodeSessionIdle("s"),
    ]

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        for e in scripted:
            await queue.put(e)

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]

    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        text = await consume_sse_after_send(client, "ses", "hi")

    assert "echoed user prompt" not in text, "user-echo text must be filtered"
    assert text == '{"actions":[]}', "only assistant text is accumulated"


# ---------------------------------------------------------------------------
# B4: live-path SSE reconnect (send-once, health-gated, send/stream error split)
# ---------------------------------------------------------------------------


async def test_live_sse_reconnects_after_transient_drop() -> None:
    """A transient SSE-reader drop reconnects (health-gated), sends once, no text dup."""
    import aiohttp

    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import (
        OpenCodeSessionIdle,
        OpenCodeStreamReady,
        OpenCodeTextUpdate,
    )
    from ach_agent.engine.lifecycle import consume_sse_after_send

    # Attempt 0: connect, grow part A, then the reader drops (ClientError).
    # Attempt 1: connect, opencode RESENDS the cumulative snapshot, then idle.
    scripts = [
        [
            OpenCodeStreamReady(),
            OpenCodeTextUpdate("s", "prtA", "msgA", "Hola"),
            OpenCodeTextUpdate("s", "prtA", "msgA", "Hola mundo"),
            aiohttp.ClientError("connection reset"),
        ],
        [
            OpenCodeStreamReady(),
            OpenCodeTextUpdate("s", "prtA", "msgA", "Hola mundo"),  # resent snapshot
            OpenCodeSessionIdle("s"),
        ],
    ]
    calls = {"n": 0}

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        script = scripts[calls["n"]]
        calls["n"] += 1
        for e in script:
            await queue.put(e)

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]
    client.check_health = AsyncMock(return_value=True)  # type: ignore[method-assign]

    streamed: list[str] = []
    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        text = await consume_sse_after_send(client, "ses", "hi", on_text=streamed.append)

    assert text == "Hola mundo", "resent snapshot deduped by the accumulator"
    assert "".join(streamed) == "Hola mundo", "on_text did not re-emit the resent prefix"
    assert client.send_message.await_count == 1, "prompt sent exactly once across reconnects"
    assert client.subscribe_events.await_count == 2, "reconnected once (two subscribe_events)"


async def test_live_send_failure_is_terminal_no_resend() -> None:
    """A send_message failure is terminal — raised, never re-sent / reconnected."""
    import aiohttp

    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import OpenCodeStreamReady
    from ach_agent.engine.lifecycle import consume_sse_after_send

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        # Only the readiness gate event; the send fails, no terminal ever arrives.
        await queue.put(OpenCodeStreamReady())

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock(side_effect=aiohttp.ClientError("post failed"))  # type: ignore[method-assign]
    client.check_health = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        with pytest.raises(aiohttp.ClientError, match="post failed"):
            await consume_sse_after_send(client, "ses", "hi")

    assert client.send_message.await_count == 1, "send attempted exactly once"
    assert client.subscribe_events.await_count == 1, "no reconnect on a send failure"


async def test_live_sse_exhausts_reconnects() -> None:
    """Every attempt's reader drops → EngineError('sse_exhausted') after max_reconnects."""
    import aiohttp

    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import EngineError, OpenCodeStreamReady
    from ach_agent.engine.lifecycle import consume_sse_after_send

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        await queue.put(OpenCodeStreamReady())
        await queue.put(aiohttp.ClientError("drop"))

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]
    client.check_health = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        with pytest.raises(EngineError, match="sse_exhausted"):
            await consume_sse_after_send(client, "ses", "hi", max_reconnects=2)

    assert client.send_message.await_count == 1, "sent once despite exhausted reconnects"
    assert client.subscribe_events.await_count == 3, "attempt 0 + 2 reconnects"


async def test_live_reconnect_gated_on_health() -> None:
    """SSE drops but check_health() is False → no reconnect, original error raised."""
    import aiohttp

    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import OpenCodeStreamReady
    from ach_agent.engine.lifecycle import consume_sse_after_send

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        await queue.put(OpenCodeStreamReady())
        await queue.put(aiohttp.ClientError("drop"))

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]
    client.check_health = AsyncMock(return_value=False)  # type: ignore[method-assign]

    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        with pytest.raises(aiohttp.ClientError, match="drop"):
            await consume_sse_after_send(client, "ses", "hi")

    assert client.subscribe_events.await_count == 1, "unhealthy engine → no reconnect"


# ---------------------------------------------------------------------------
# B5: mid-invocation liveness — fail fast when opencode dies during a turn
# ---------------------------------------------------------------------------


async def test_engine_death_mid_invocation_fails_fast() -> None:
    """A silent stream + dead engine raises engine_died within a poll, not the 300s stall."""
    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import EngineError, OpenCodeStreamReady
    from ach_agent.engine.lifecycle import consume_sse_after_send

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        # Gate event only, then silence — no terminal ever arrives (engine died).
        await queue.put(OpenCodeStreamReady())

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]

    # Small poll so the test resolves fast; is_alive False → engine_died on first poll timeout.
    with (
        patch.object(ev, "_consume_events_from_response", new=fake_consume),
        patch("ach_agent.engine.lifecycle._LIVENESS_POLL_S", 0.05),
    ):
        with pytest.raises(EngineError, match="engine_died"):
            await asyncio.wait_for(
                consume_sse_after_send(client, "ses", "hi", is_alive=lambda: False),
                timeout=2.0,
            )


async def test_alive_engine_slow_but_not_dead_waits() -> None:
    """A slow-but-alive engine (is_alive True) is NOT failed as dead; it completes on idle."""
    from ach_agent.engine import events as ev
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import (
        OpenCodeSessionIdle,
        OpenCodeStreamReady,
        OpenCodeTextUpdate,
    )
    from ach_agent.engine.lifecycle import consume_sse_after_send

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        await queue.put(OpenCodeStreamReady())
        await asyncio.sleep(0.15)  # spans a couple of poll intervals (0.05)
        await queue.put(OpenCodeTextUpdate("s", "prtA", "msgA", "later"))
        await queue.put(OpenCodeSessionIdle("s"))

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]

    with (
        patch.object(ev, "_consume_events_from_response", new=fake_consume),
        patch("ach_agent.engine.lifecycle._LIVENESS_POLL_S", 0.05),
    ):
        text = await asyncio.wait_for(
            consume_sse_after_send(client, "ses", "hi", is_alive=lambda: True),
            timeout=2.0,
        )
    assert text == "later", "slow-but-alive engine completes normally (no engine_died)"


async def test_run_invocation_threads_is_alive() -> None:
    """run_invocation passes is_alive=server.is_alive into every consume_sse_after_send call."""
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, run_invocation

    server = ManagedServer(port=19890)
    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.create_session = AsyncMock(return_value={"id": "ses-x"})
    server._client = mock_client
    fake_proc = MagicMock()
    fake_proc.returncode = None
    server._process = fake_proc

    seen_is_alive: list[Any] = []

    async def fake_consume(_client: Any, _sid: str, _prompt: str, **kwargs: Any) -> str:
        seen_is_alive.append(kwargs.get("is_alive"))
        return '{"action":"none","text":"done"}'

    with patch("ach_agent.engine.lifecycle.consume_sse_after_send", new=fake_consume):
        await run_invocation(
            server=server,
            session_id="k",
            prompt="hi",
            terminal_retries=1,
        )

    assert seen_is_alive, "consume_sse_after_send was not called"
    # Bound methods aren't identity-stable (each `server.is_alive` access makes a new object),
    # but == compares (__self__, __func__) — so this asserts the SAME bound is_alive was passed.
    assert all(cb == server.is_alive for cb in seen_is_alive), (
        "run_invocation must thread is_alive=server.is_alive into every consume call"
    )


# ---------------------------------------------------------------------------
# SEC-01 / ek-hygiene: opencode subprocess env is clean-slate (allowlist only)
# ---------------------------------------------------------------------------


def test_build_opencode_env_strips_secrets_in_proxy_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proxy mode: the ek_ and other secrets are NEVER forwarded into opencode's env."""
    from ach_agent.engine.lifecycle import EngineConfig, build_opencode_env

    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("ACH_TOKEN", "ek-secret")
    monkeypatch.setenv("ACH_API_KEY", "ek-secret")
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-x")

    dummy_cfg_path = tmp_path / ".config" / "opencode" / "opencode_k.json"
    env = build_opencode_env(
        tmp_path, EngineConfig(model_base_url="http://127.0.0.1:9/v1"), dummy_cfg_path
    )

    assert "ACH_TOKEN" not in env
    assert "ACH_API_KEY" not in env  # proxy injects the ek_; opencode must not see it
    assert "GITLAB_TOKEN" not in env
    assert env["PATH"] == "/usr/bin"  # base allowlist preserved
    assert env["HOME"] == str(tmp_path)  # pinned to ephemeral home
    assert env["TMPDIR"] == "/tmp"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert "OPENCODE_CONFIG" in env  # per-session config path always set


def test_build_opencode_env_forwards_configured_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """engine.forwardEnv names are forwarded by value; unnamed secrets stay stripped."""
    from ach_agent.engine.lifecycle import EngineConfig, build_opencode_env

    monkeypatch.setenv("MY_CA_BUNDLE", "/etc/ca.pem")
    monkeypatch.setenv("ACH_TOKEN", "ek-secret")

    dummy_cfg_path = tmp_path / ".config" / "opencode" / "opencode_k.json"
    env = build_opencode_env(
        tmp_path,
        EngineConfig(model_base_url="http://127.0.0.1:9/v1", forward_env=["MY_CA_BUNDLE"]),
        dummy_cfg_path,
    )

    assert env["MY_CA_BUNDLE"] == "/etc/ca.pem"
    assert "ACH_TOKEN" not in env  # not named → not forwarded


def test_build_opencode_env_pins_exa_enable_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OPENCODE_ENABLE_EXA is always passed to opencode; a forwarded value still wins."""
    from ach_agent.engine.lifecycle import EngineConfig, build_opencode_env

    cfg_path = tmp_path / ".config" / "opencode" / "opencode_k.json"

    # Default: pinned true even when the harness env has nothing.
    monkeypatch.delenv("OPENCODE_ENABLE_EXA", raising=False)
    env = build_opencode_env(tmp_path, EngineConfig(), cfg_path)
    assert env["OPENCODE_ENABLE_EXA"] == "true"

    # An explicit forwardEnv value wins over the pinned default.
    monkeypatch.setenv("OPENCODE_ENABLE_EXA", "false")
    env = build_opencode_env(tmp_path, EngineConfig(forward_env=["OPENCODE_ENABLE_EXA"]), cfg_path)
    assert env["OPENCODE_ENABLE_EXA"] == "false"


def test_write_opencode_config_per_session_filename(tmp_path: Path) -> None:
    from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config

    cfg = EngineConfig(model_base_url="http://127.0.0.1:9/v1")
    path = write_opencode_config(tmp_path, cfg, "gitlab.com/g/repo")
    cfg_dir = tmp_path / ".config" / "opencode"
    # per-session filename; there is NO opencode.json fallback
    assert path.parent == cfg_dir
    assert path.name.startswith("opencode_") and path.name.endswith(".json")
    assert path.is_file()
    assert not (cfg_dir / "opencode.json").exists()
    # deterministic in the key
    again = write_opencode_config(tmp_path, cfg, "gitlab.com/g/repo")
    assert again.name == path.name
    # a per-session prompt file is written too, under .config/opencode/personality/
    # (CONTRACT §3.2), NOT a shared system_prompt.txt and NOT top-level personality/
    assert not (cfg_dir / "personality" / "system_prompt.txt").exists()
    assert not (tmp_path / "personality").exists()
    assert (cfg_dir / "personality" / f"system_prompt{path.name[len('opencode'):-len('.json')]}.txt").is_file()


def test_write_opencode_config_provider_by_model_type(tmp_path: Path) -> None:
    """model.type selects the opencode provider + model ref (not a hardcoded openai wire).

    gemini → built-in "google" (no npm, native /v1beta wire); openai → custom "ach"
    backed by @ai-sdk/openai-compatible. Regression lock for the type:gemini fix.
    """
    import json

    from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config

    # gemini: built-in google provider, model ref google/<name>, no npm field.
    g = EngineConfig(
        model="gemini-flash-latest",
        model_type="gemini",
        model_base_url="http://127.0.0.1:9/gemini/v1beta",
    )
    oc = json.loads(write_opencode_config(tmp_path, g, "k").read_text(encoding="utf-8"))
    assert oc["enabled_providers"] == ["google"]
    assert oc["model"] == "google/gemini-flash-latest"
    assert oc["small_model"] == "google/gemini-flash-latest"
    assert "gemini-flash-latest" in oc["provider"]["google"]["models"]
    assert "npm" not in oc["provider"]["google"]  # built-in provider needs no npm
    assert oc["provider"]["google"]["options"]["baseURL"].endswith("/gemini/v1beta")

    # openai (default type): custom ach provider on the lenient openai-compatible parser.
    o = EngineConfig(model="ackstorm.smart", model_base_url="http://127.0.0.1:9/v1")
    oc = json.loads(write_opencode_config(tmp_path, o, "k2").read_text(encoding="utf-8"))
    assert oc["enabled_providers"] == ["ach"]
    assert oc["model"] == "ach/ackstorm.smart"
    assert oc["provider"]["ach"]["npm"] == "@ai-sdk/openai-compatible"


def test_build_opencode_env_sets_opencode_config(tmp_path: Path) -> None:
    from ach_agent.engine.lifecycle import EngineConfig, build_opencode_env

    cfg_path = tmp_path / ".config" / "opencode" / "opencode_k.json"
    env = build_opencode_env(tmp_path, EngineConfig(), cfg_path)
    assert env["OPENCODE_CONFIG"] == str(cfg_path)
    assert env["HOME"] == str(tmp_path)  # HOME stays the shared home


def test_engine_config_has_home_and_no_session_dir() -> None:
    from ach_agent.engine.lifecycle import EngineConfig

    cfg = EngineConfig(home="/var/lib/ach-agent/home")
    assert cfg.home == "/var/lib/ach-agent/home"
    assert not hasattr(cfg, "session_dir")


# ---------------------------------------------------------------------------
# session reuse policy: run_invocation(reuse=True|False)
# ---------------------------------------------------------------------------


async def test_run_invocation_reuse_false_always_creates_fresh_session() -> None:
    """reuse=False: two invocations each create a distinct opencode session.

    server._sessions must remain untouched (empty for the key) across both calls.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, run_invocation

    # Incrementing counter to produce distinct session ids per create_session call
    call_count = 0

    async def fake_create_session() -> dict[str, str]:
        nonlocal call_count
        call_count += 1
        return {"id": f"ses-fresh-{call_count}"}

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.create_session = fake_create_session

    server = ManagedServer(port=19883)
    mock_proc = MagicMock()
    mock_proc.returncode = None
    server._process = mock_proc
    server._client = mock_client

    canned = '{"action":"none","text":"ok","thoughts":""}'

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ):
        result1 = await run_invocation(
            server=server,
            session_id="key-a",
            prompt="first",
            terminal_retries=1,
            reuse=False,
        )
        result2 = await run_invocation(
            server=server,
            session_id="key-a",
            prompt="second",
            terminal_retries=1,
            reuse=False,
        )

    # Two distinct opencode sessions were created
    assert call_count == 2
    # server._sessions must be untouched — reuse=False never writes to it
    assert "key-a" not in server._sessions
    # Both invocations returned valid terminal objects
    assert result1["action"] == "none"
    assert result2["action"] == "none"


async def test_run_invocation_reuse_true_reuses_session() -> None:
    """reuse=True (default): second invocation reuses the opencode session from the first.

    create_session is called exactly once; server._sessions is populated.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, run_invocation

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.create_session = AsyncMock(return_value={"id": "ses-reused"})

    server = ManagedServer(port=19884)
    mock_proc = MagicMock()
    mock_proc.returncode = None
    server._process = mock_proc
    server._client = mock_client

    canned = '{"action":"none","text":"ok","thoughts":""}'

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ):
        await run_invocation(
            server=server,
            session_id="key-b",
            prompt="first",
            terminal_retries=1,
            reuse=True,
        )
        await run_invocation(
            server=server,
            session_id="key-b",
            prompt="second",
            terminal_retries=1,
            reuse=True,
        )

    # create_session called exactly once — second call reuses the stored id
    mock_client.create_session.assert_awaited_once()
    assert server._sessions.get("key-b") == "ses-reused"


# ---------------------------------------------------------------------------
# pool-owned session map: run_invocation(oc_sessions=...)
# ---------------------------------------------------------------------------


def _server_with_client(port: int, create_session: object) -> "ManagedServer":  # noqa: F821, UP037
    """Fresh ManagedServer wired to a mock client (simulates one opencode process)."""
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.create_session = create_session
    server = ManagedServer(port=port)
    mock_proc = MagicMock()
    mock_proc.returncode = None
    server._process = mock_proc
    server._client = mock_client
    return server


async def test_run_invocation_pool_map_survives_server_replacement() -> None:
    """The core feature: a NEW ManagedServer (idle-TTL restart) reuses the opencode
    session id cached in the pool-owned map by the previous server — create_session
    is never called on the second server."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {}

    server1 = _server_with_client(
        19885, AsyncMock(return_value={"id": "ses-persist"})
    )
    server2 = _server_with_client(
        19886, AsyncMock(return_value={"id": "ses-WRONG-never-created"})
    )

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ) as mock_consume:
        await run_invocation(
            server=server1,
            session_id="key-a",
            prompt="first",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )
        # server1 died (TTL); server2 is the replacement — same map, no create.
        await run_invocation(
            server=server2,
            session_id="key-a",
            prompt="second",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )

    assert shared_map == {"key-a": "ses-persist"}
    server1._client.create_session.assert_awaited_once()
    server2._client.create_session.assert_not_awaited()
    # consume_sse_after_send(client, oc_session_id, prompt, ...) — positional arg 1
    assert mock_consume.call_args_list[0].args[1] == "ses-persist"
    assert mock_consume.call_args_list[1].args[1] == "ses-persist"


async def test_run_invocation_reuse_false_ignores_pool_map() -> None:
    """reuse=False (channel.session='none') never reads nor writes the pool map."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {"key-a": "ses-cached"}
    server = _server_with_client(19887, AsyncMock(return_value={"id": "ses-fresh"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ) as mock_consume:
        await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=False,
            oc_sessions=shared_map,
        )

    server._client.create_session.assert_awaited_once()  # fresh, not cached
    assert mock_consume.call_args_list[0].args[1] == "ses-fresh"
    assert shared_map == {"key-a": "ses-cached"}  # untouched


async def test_run_invocation_logs_oc_session(capfd) -> None:
    """Every turn logs which opencode session is used and whether it was reused."""
    import structlog

    from ach_agent.engine.lifecycle import run_invocation

    # Other test modules call structlog.configure() (global, mutable) without resetting
    # it; reset here so this assertion doesn't depend on cross-test execution order.
    structlog.reset_defaults()

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {}
    server = _server_with_client(19888, AsyncMock(return_value={"id": "ses-log-1"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ):
        await run_invocation(
            server=server,
            session_id="key-log",
            prompt="p",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )

    out, err = capfd.readouterr()
    combined = out + err
    assert "engine: opencode session" in combined
    assert "ses-log-1" in combined
    assert "reused" in combined


async def test_stop_releases_reserved_port() -> None:
    """B7: ManagedServer.stop() frees the reserved port so it can be reused."""
    from ach_agent.engine import client as client_mod
    from ach_agent.engine.client import find_free_port, release_port
    from ach_agent.engine.lifecycle import ManagedServer

    port = find_free_port()
    assert port in client_mod._reserved_ports

    exited_proc = MagicMock()
    exited_proc.returncode = 0  # already exited — _process_group_kill is a no-op

    server = ManagedServer(port=port)
    server._process = exited_proc

    try:
        await server.stop()
        assert port not in client_mod._reserved_ports, "stop() must release the reserved port"

        # Double-stop is safe (release_port is a no-op on an absent port).
        await server.stop()
        assert port not in client_mod._reserved_ports
    finally:
        release_port(port)


# ---------------------------------------------------------------------------
# Shared-home parity: launch() populates ManagedServer.config_path
# ---------------------------------------------------------------------------


async def test_launch_populates_config_path(tmp_path: Path) -> None:
    """launch() stores the path returned by write_opencode_config in server.config_path.

    Under the shared-home model a per-session config file is written and the path is
    needed by the --tui attach client to set OPENCODE_CONFIG.  Drives launch() with a
    real fake binary so the full code path executes (same pattern as test_launch_subprocess).
    """
    from ach_agent.engine.lifecycle import EngineConfig, ManagedServer, launch

    config = EngineConfig()
    fake_binary = tmp_path / "opencode"
    fake_binary.write_text("#!/bin/sh\nsleep 30\n")
    fake_binary.chmod(0o755)
    config.binary_path = str(fake_binary)
    config.work_dir = str(tmp_path)

    known_path = tmp_path / ".config" / "opencode" / "opencode_k1.json"

    with (
        patch(
            "ach_agent.engine.lifecycle.write_opencode_config",
            return_value=known_path,
        ),
        patch(
            "ach_agent.engine.client.OpenCodeClient.check_health",
            new_callable=AsyncMock,
            return_value=True,
        ),
    ):
        server = await launch(port=19883, ephemeral_home=tmp_path, config=config, session_key="k1")

    try:
        assert server.config_path == known_path, (
            f"Expected config_path={known_path!r}, got {server.config_path!r}"
        )
    finally:
        await server.stop()


def test_managed_server_config_path_defaults_none() -> None:
    """ManagedServer.config_path defaults to None when not supplied."""
    from ach_agent.engine.lifecycle import ManagedServer

    server = ManagedServer(port=0)
    assert server.config_path is None


# ---------------------------------------------------------------------------
# Plan 4: step-budget abort inside consume_sse_after_send
# ---------------------------------------------------------------------------


def _tool_client() -> Any:
    """OpenCodeClient with subscribe/send mocked + a recording abort_session AsyncMock."""
    from ach_agent.engine.client import OpenCodeClient

    client = OpenCodeClient("http://127.0.0.1:0")
    client.subscribe_events = AsyncMock(return_value=MagicMock(release=AsyncMock()))  # type: ignore[method-assign]
    client.send_message = AsyncMock()  # type: ignore[method-assign]
    client.abort_session = AsyncMock()  # type: ignore[method-assign]
    return client


async def test_step_budget_aborts_after_threshold() -> None:
    """max_tool_calls=3: after 3 distinct tool call_ids → abort once, keep consuming to idle."""
    from ach_agent.engine import events as ev
    from ach_agent.engine.events import (
        OpenCodeSessionIdle,
        OpenCodeStreamReady,
        OpenCodeTextUpdate,
        OpenCodeToolUpdate,
        ToolStateCompleted,
        ToolStateRunning,
    )
    from ach_agent.engine.lifecycle import consume_sse_after_send

    scripted = [
        OpenCodeStreamReady(),
        OpenCodeToolUpdate("s", "p1", "m", "tool", "c1", ToolStateRunning()),
        OpenCodeToolUpdate("s", "p1", "m", "tool", "c1", ToolStateCompleted()),
        OpenCodeToolUpdate("s", "p2", "m", "tool", "c2", ToolStateRunning()),
        OpenCodeToolUpdate("s", "p3", "m", "tool", "c3", ToolStateRunning()),
        OpenCodeTextUpdate("s", "pA", "m", "partial reply"),
        OpenCodeSessionIdle("s"),
    ]

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        for e in scripted:
            await queue.put(e)

    client = _tool_client()
    stats: dict[str, Any] = {}
    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        text = await consume_sse_after_send(
            client, "ses", "hi", max_tool_calls=3, stats=stats
        )

    client.abort_session.assert_awaited_once_with("ses")
    assert stats["aborted"] is True
    assert stats["tool_calls"] >= 3
    assert text == "partial reply", "idle arrived after abort → accumulated text returned"


async def test_step_budget_counts_distinct_calls_not_updates() -> None:
    """One call_id with running+completed (2 updates) + max_tool_calls=2 → no abort."""
    from ach_agent.engine import events as ev
    from ach_agent.engine.events import (
        OpenCodeSessionIdle,
        OpenCodeStreamReady,
        OpenCodeToolUpdate,
        ToolStateCompleted,
        ToolStateRunning,
    )
    from ach_agent.engine.lifecycle import consume_sse_after_send

    scripted = [
        OpenCodeStreamReady(),
        OpenCodeToolUpdate("s", "p1", "m", "tool", "c1", ToolStateRunning()),
        OpenCodeToolUpdate("s", "p1", "m", "tool", "c1", ToolStateCompleted()),
        OpenCodeSessionIdle("s"),
    ]

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        for e in scripted:
            await queue.put(e)

    client = _tool_client()
    stats: dict[str, Any] = {}
    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        await consume_sse_after_send(client, "ses", "hi", max_tool_calls=2, stats=stats)

    client.abort_session.assert_not_awaited()
    assert stats["aborted"] is False
    assert stats["tool_calls"] == 1, "distinct call_ids counted, not per-update"


async def test_step_budget_disabled_never_aborts() -> None:
    """max_tool_calls=0 (default): many tool calls → never abort, stats.aborted False."""
    from ach_agent.engine import events as ev
    from ach_agent.engine.events import (
        OpenCodeSessionIdle,
        OpenCodeStreamReady,
        OpenCodeToolUpdate,
        ToolStateRunning,
    )
    from ach_agent.engine.lifecycle import consume_sse_after_send

    scripted = [
        OpenCodeStreamReady(),
        *[
            OpenCodeToolUpdate("s", f"p{i}", "m", "tool", f"c{i}", ToolStateRunning())
            for i in range(10)
        ],
        OpenCodeSessionIdle("s"),
    ]

    async def fake_consume(_client: Any, _resp: Any, queue: asyncio.Queue) -> None:
        for e in scripted:
            await queue.put(e)

    client = _tool_client()
    stats: dict[str, Any] = {}
    with patch.object(ev, "_consume_events_from_response", new=fake_consume):
        await consume_sse_after_send(client, "ses", "hi", max_tool_calls=0, stats=stats)

    client.abort_session.assert_not_awaited()
    assert stats["aborted"] is False
    assert stats["tool_calls"] == 10


# ---------------------------------------------------------------------------
# Plan 4: abort-triggered wrap-up turn in run_invocation
# ---------------------------------------------------------------------------


def _wrapup_server(port: int) -> Any:
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer

    server = ManagedServer(port=port)
    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.create_session = AsyncMock(return_value={"id": "ses-x"})
    server._client = mock_client
    fake_proc = MagicMock()
    fake_proc.returncode = None
    server._process = fake_proc
    return server


async def test_run_invocation_wrapup_after_abort() -> None:
    """An aborted turn runs ONE wrap-up consume (budget off) → terminal object from wrap-up."""
    from ach_agent.engine.lifecycle import run_invocation

    server = _wrapup_server(19891)
    calls: list[dict[str, Any]] = []

    async def fake_consume(_client: Any, _sid: str, prompt: str, **kwargs: Any) -> str:
        calls.append({"prompt": prompt, "mtc": kwargs.get("max_tool_calls", 0)})
        if len(calls) == 1:
            stats = kwargs.get("stats")
            if stats is not None:
                stats["aborted"] = True
                stats["tool_calls"] = 9
            return "ran many tools, no terminal json here"
        return '{"action":"none","text":"wrapped up"}'

    with patch("ach_agent.engine.lifecycle.consume_sse_after_send", new=fake_consume):
        result = await run_invocation(
            server=server, session_id="k", prompt="hi", terminal_retries=1, max_tool_calls=5
        )

    assert len(calls) == 2, "aborted turn triggers exactly one wrap-up consume"
    assert calls[0]["mtc"] == 5, "main turn carries the budget"
    assert calls[1]["mtc"] == 0, "wrap-up turn disables the budget"
    assert calls[1]["prompt"].startswith("You have reached your tool-call budget")
    assert result == {"action": "none", "text": "wrapped up"}


async def test_run_invocation_no_wrapup_when_not_aborted() -> None:
    """Not aborted → exactly ONE consume (no wrap-up), terminal object returned as-is."""
    from ach_agent.engine.lifecycle import run_invocation

    server = _wrapup_server(19892)
    calls: list[str] = []

    async def fake_consume(_client: Any, _sid: str, prompt: str, **kwargs: Any) -> str:
        calls.append(prompt)
        stats = kwargs.get("stats")
        if stats is not None:
            stats["aborted"] = False
            stats["tool_calls"] = 2
        return '{"action":"none","text":"clean"}'

    with patch("ach_agent.engine.lifecycle.consume_sse_after_send", new=fake_consume):
        result = await run_invocation(
            server=server, session_id="k", prompt="hi", terminal_retries=1, max_tool_calls=5
        )

    assert len(calls) == 1, "no abort → no wrap-up turn"
    assert result["text"] == "clean"


async def test_run_invocation_freeform_abort_returns_wrapup_text() -> None:
    """free_form + aborted → wrap-up ran, returns {'action':'none','text': <wrap-up text>}."""
    from ach_agent.engine.lifecycle import run_invocation

    server = _wrapup_server(19893)
    calls: list[dict[str, Any]] = []

    async def fake_consume(_client: Any, _sid: str, prompt: str, **kwargs: Any) -> str:
        calls.append({"prompt": prompt, "mtc": kwargs.get("max_tool_calls", 0)})
        if len(calls) == 1:
            stats = kwargs.get("stats")
            if stats is not None:
                stats["aborted"] = True
            return "partial free-form reply"
        return "final free-form summary"

    with patch("ach_agent.engine.lifecycle.consume_sse_after_send", new=fake_consume):
        result = await run_invocation(
            server=server,
            session_id="k",
            prompt="hi",
            terminal_retries=1,
            free_form=True,
            max_tool_calls=5,
        )

    assert len(calls) == 2, "aborted free-form turn still runs the wrap-up"
    assert calls[1]["mtc"] == 0
    assert result == {"action": "none", "text": "final free-form summary"}


# ---------------------------------------------------------------------------
# stale cached session: 404 → recreate + retry once
# ---------------------------------------------------------------------------


def _client_response_error(status: int) -> "aiohttp.ClientResponseError":  # noqa: F821, UP037
    import aiohttp

    return aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=status
    )


async def test_stale_cached_session_recreated_and_retried() -> None:
    """Cached id 404s on the (new) server → mint fresh session, overwrite map, retry once."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {"key-a": "ses-stale"}
    server = _server_with_client(19889, AsyncMock(return_value={"id": "ses-new"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=[_client_response_error(404), canned],
    ) as mock_consume:
        result = await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )

    assert shared_map == {"key-a": "ses-new"}
    server._client.create_session.assert_awaited_once()
    assert mock_consume.call_args_list[0].args[1] == "ses-stale"
    assert mock_consume.call_args_list[1].args[1] == "ses-new"
    assert result["action"] == "none"


async def test_404_on_fresh_session_propagates() -> None:
    """A fresh (just-created) id cannot be 'stale' — 404 propagates, no retry loop."""
    import aiohttp

    from ach_agent.engine.lifecycle import run_invocation

    shared_map: dict[str, str] = {}
    server = _server_with_client(19890, AsyncMock(return_value={"id": "ses-x"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=_client_response_error(404),
    ) as mock_consume:
        with pytest.raises(aiohttp.ClientResponseError):
            await run_invocation(
                server=server,
                session_id="key-a",
                prompt="p",
                terminal_retries=1,
                reuse=True,
                oc_sessions=shared_map,
            )

    assert len(mock_consume.call_args_list) == 1  # no retry


async def test_non_404_error_on_reused_session_propagates() -> None:
    """Only 404 triggers the stale-recreate path; a 500 on a reused id propagates."""
    import aiohttp

    from ach_agent.engine.lifecycle import run_invocation

    shared_map: dict[str, str] = {"key-a": "ses-cached"}
    server = _server_with_client(19891, AsyncMock(return_value={"id": "ses-x"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=_client_response_error(500),
    ):
        with pytest.raises(aiohttp.ClientResponseError):
            await run_invocation(
                server=server,
                session_id="key-a",
                prompt="p",
                terminal_retries=1,
                reuse=True,
                oc_sessions=shared_map,
            )

    assert shared_map == {"key-a": "ses-cached"}  # map NOT overwritten


# ---------------------------------------------------------------------------
# stats oc_session_id + discard/compact helpers
# ---------------------------------------------------------------------------


async def test_run_invocation_reports_oc_session_id_in_stats() -> None:
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    server = _server_with_client(19892, AsyncMock(return_value={"id": "ses-stats"}))
    turn_stats: dict = {}

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ):
        await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=False,
            stats=turn_stats,
        )

    assert turn_stats["oc_session_id"] == "ses-stats"


async def test_stats_oc_session_id_reflects_stale_retry() -> None:
    """After the 404 stale-guard recreates the session, stats carries the NEW id."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {"key-a": "ses-stale"}
    server = _server_with_client(19893, AsyncMock(return_value={"id": "ses-new"}))
    turn_stats: dict = {}

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=[_client_response_error(404), canned],
    ):
        await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
            stats=turn_stats,
        )

    assert turn_stats["oc_session_id"] == "ses-new"


async def test_discard_oc_session_swallows_errors() -> None:
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, discard_oc_session

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.delete_session = AsyncMock(side_effect=RuntimeError("boom"))
    server = ManagedServer(port=19894)
    server._client = mock_client

    await discard_oc_session(server, "ses-x")  # must not raise
    mock_client.delete_session.assert_awaited_once_with("ses-x")

    # no client at all → silent no-op
    await discard_oc_session(ManagedServer(port=19895), "ses-y")


async def test_compact_oc_session_calls_client() -> None:
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, compact_oc_session

    mock_client = AsyncMock(spec=OpenCodeClient)
    server = ManagedServer(port=19896)
    server._client = mock_client

    await compact_oc_session(server, "ses-z")
    mock_client.compact_session.assert_awaited_once_with("ses-z")

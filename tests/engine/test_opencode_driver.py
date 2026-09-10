# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from ach_agent.engine import trace
from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.opencode.driver import OpencodeDriver


class _FakeClient:
    """Stands in for OpenCodeClient — isinstance() check in run_turn is bypassed via patch."""


class _FakeServer:
    proxy_token = "tok"

    def __init__(self) -> None:
        self._client = _FakeClient()

    def is_alive(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _accept_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_turn does `isinstance(client, OpenCodeClient)`; make the fake pass.
    import ach_agent.engine.opencode.driver as drv

    monkeypatch.setattr(drv, "OpenCodeClient", _FakeClient, raising=False)


async def test_run_turn_reuse_creates_and_records_session() -> None:
    sessions: dict[str, str] = {}
    stats: dict[str, Any] = {}
    with (
        patch("ach_agent.engine.lifecycle._create_oc_session", return_value="ses_new") as mk,
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", return_value="hello") as cs,
    ):
        result = await OpencodeDriver().run_turn(
            _FakeServer(),
            conv_key="k1",
            prompt="p",
            reuse=True,
            sessions=sessions,
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats=stats,
        )
    assert result == TurnResult(text="hello", session_ref="ses_new", aborted=False)
    assert sessions["k1"] == "ses_new"
    assert stats["session_ref"] == "ses_new" and stats["oc_session_id"] == "ses_new"
    mk.assert_awaited_once()
    cs.assert_awaited_once()


async def test_run_turn_with_session_ref_bypasses_map() -> None:
    sessions: dict[str, str] = {}
    with (
        patch("ach_agent.engine.lifecycle._create_oc_session") as mk,
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", return_value="wrapped"),
    ):
        result = await OpencodeDriver().run_turn(
            _FakeServer(),
            conv_key="k1",
            prompt="wrap",
            reuse=True,
            sessions=sessions,
            session_ref="ses_fixed",
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats={},
        )
    assert result.session_ref == "ses_fixed"
    assert result.text == "wrapped"
    assert sessions == {}          # map never touched on the session_ref path
    mk.assert_not_awaited()        # no create on the continue path


async def test_session_is_correlated_before_the_prompt_is_sent() -> None:
    # Ordering is the whole point: consume_sse_after_send is what sends the prompt,
    # so a set_session AFTER it would leave every session's first turn without a
    # sessionId in Langfuse. Capture the header state from inside the send.
    trace.reset_for_testing()
    server = _FakeServer()
    server.proxy_token = trace.mint_token()
    seen: dict[str, str] = {}

    async def fake_consume(*_args: Any, **_kwargs: Any) -> str:
        seen.update(trace.headers(server.proxy_token))
        return "hello"

    with (
        patch("ach_agent.engine.lifecycle._create_oc_session", return_value="ses_8a1b2c3d"),
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", new=fake_consume),
    ):
        await OpencodeDriver().run_turn(
            server,
            conv_key="k1",
            prompt="p",
            reuse=True,
            sessions={},
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats={},
        )
    assert seen == {
        "langfuse_session_id": "ses_8a1b2c3d",
        "x-litellm-session-id": "ses_8a1b2c3d",
    }
    trace.reset_for_testing()


async def test_launch_cancellation_releases_subprocess_client_and_port(
    tmp_path: Path,
) -> None:
    """finding 8: cancelling driver.launch() while poll_ready is still blocked (readiness
    never confirms) must stop the already-spawned subprocess, close its HTTP client, and
    release the allocated port — launch() must not leak all three by only ever cleaning
    up on the happy path."""
    import ach_agent.engine.lifecycle as oc_lifecycle
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.opencode.client import _reserved_ports

    fake_binary = tmp_path / "opencode"
    fake_binary.write_text("#!/bin/sh\nsleep 30\n")
    fake_binary.chmod(0o755)

    config = EngineConfig()
    config.binary_path = str(fake_binary)
    config.work_dir = str(tmp_path)
    config.home = str(tmp_path)

    # Spy on the real oc.launch so the test can see the ManagedServer + port it
    # allocated, without changing driver.launch()'s own resolution of `oc`.
    captured: dict[str, Any] = {}
    real_launch = oc_lifecycle.launch

    async def spy_launch(port: int, home: Path, cfg: EngineConfig, session_key: str) -> Any:
        server = await real_launch(port, home, cfg, session_key)
        captured["server"] = server
        captured["port"] = port
        return server

    driver = OpencodeDriver()
    try:
        with (
            patch.object(oc_lifecycle, "launch", side_effect=spy_launch),
            patch(
                "ach_agent.engine.client.OpenCodeClient.check_health",
                new_callable=AsyncMock,
                return_value=False,  # readiness never confirms — poll_ready stays blocked
            ),
        ):
            task = asyncio.create_task(driver.launch(config, "k-cancel"))
            for _ in range(100):
                if "server" in captured:
                    break
                await asyncio.sleep(0.05)
            else:
                pytest.fail("driver.launch() never reached poll_ready")

            assert not task.done(), "poll_ready must still be blocked (health never True)"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5.0)

        server = captured["server"]
        port = captured["port"]
        assert not server.is_alive(), "subprocess must have exited"
        assert server._client._session is None, "HTTP client must be closed"
        assert port not in _reserved_ports, "allocated port must be released"
    finally:
        # Belt-and-braces: if the assertions above ever fail, don't leak the real
        # subprocess/port into later tests.
        server = captured.get("server")
        if server is not None and server.is_alive():
            await server.stop()


def test_signature_canonical_matches_protocol() -> None:
    sig = inspect.signature(OpencodeDriver.run_turn)
    kw_only_names = [p.name for p in sig.parameters.values() if p.kind == inspect.Parameter.KEYWORD_ONLY]
    expected_kw_only = [
        "conv_key",
        "prompt",
        "reuse",
        "sessions",
        "session_ref",
        "on_text",
        "on_tool",
        "max_tool_calls",
        "stats",
    ]
    assert kw_only_names == expected_kw_only
    for name in ["on_text", "on_tool", "max_tool_calls", "stats"]:
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty, f"Parameter {name} must have no default"

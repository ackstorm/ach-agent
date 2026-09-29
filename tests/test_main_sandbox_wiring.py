# SPDX-License-Identifier: Apache-2.0
"""Sandboxed boot helpers in main: fail-closed key, egress listen/advertise, gateway allowlist."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ach_agent import main as main_mod
from ach_agent.config.schema import AgentConfig
from tests.config.test_handoff_hooks import _base


def _cfg(**sandbox: Any) -> AgentConfig:
    raw = _base()
    raw["persistence"] = {"enabled": True}
    raw["sandbox"] = {
        "enabled": True,
        "warmPool": "wp",
        "gatewayHost": "bot.ach.svc",
        "sessions": {"bucket": "b"},
        **sandbox,
    }
    return AgentConfig.model_validate(raw)


def test_missing_key_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACH_SANDBOX_KEY", raising=False)
    with pytest.raises(SystemExit) as info:
        main_mod._sandbox_key(_cfg())
    assert info.value.code == 1


def test_key_is_read_from_the_configured_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_K", "abc")
    assert main_mod._sandbox_key(_cfg(keyEnv="MY_K")) == b"abc"


def test_egress_listens_on_all_interfaces_and_advertises_gateway_host() -> None:
    kwargs, url = main_mod._egress_listen(_cfg(), True)
    assert kwargs == {"listen_host": "0.0.0.0", "listen_port": 8096}
    assert url == "http://bot.ach.svc:8096"
    assert main_mod._egress_listen(_cfg(), False) == ({}, "")


def test_gateway_allowlist_is_loopback_facade_ports_only() -> None:
    ports = main_mod._facade_ports(
        "http://127.0.0.1:9000/openai/v1",
        "http://127.0.0.1:9001/mcp",
        "https://mcp.example.com/x",
        None,
        "",
    )
    assert ports == {9000, 9001}


async def test_start_and_stop_sandbox_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starts the gateway on the configured port, sweeps hourly, and stops links only."""
    monkeypatch.setenv("POD_NAMESPACE", "ach")
    cfg = _cfg(gatewayPort=0)
    cfg.persistence.mount_path = str(tmp_path / "mnt")
    batch = tmp_path / "batch"
    batch.mkdir()
    (batch / "f").write_text("x")
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    events: list[str] = []
    sessions, gateway, sweep = await main_mod._start_sandbox(
        cfg,
        key=b"k" * 32,
        public_cfg=SimpleNamespace(),
        hydration_batch=batch,
        state_dir=state_dir,
        facade_urls=["http://127.0.0.1:9000/v1"],
        egress_url="",
    )
    try:
        assert gateway._ports == {9000}
        assert (state_dir / "hydration.tar.gz").exists()
        assert not sweep.done()
    finally:
        real_close, real_stop = sessions.close, gateway.stop

        async def close() -> None:
            events.append("sessions.close")
            await real_close()

        async def stop() -> None:
            events.append("gateway.stop")
            await real_stop()

        sessions.close, gateway.stop = close, stop
        await main_mod._stop_sandbox(sessions, gateway, sweep)
        await asyncio.sleep(0)
    assert events == ["sessions.close", "gateway.stop"]
    assert sweep.cancelled()

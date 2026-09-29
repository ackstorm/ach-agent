# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from ach_agent.execution.service import ExecutionService, _egress_env
from ach_agent.execution.wire import PublicEngineConfig

_CA1 = "-----BEGIN CERTIFICATE-----\nONE\n-----END CERTIFICATE-----"
_CA2 = "-----BEGIN CERTIFICATE-----\nTWO\n-----END CERTIFICATE-----"


def _cfg(tmp_path: Path, url: str, ca: str = _CA1) -> PublicEngineConfig:
    return PublicEngineConfig(
        home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "home" / "workspace"),
        egress_proxy_url=url,
        egress_proxy_capability="cap",
        egress_ca_cert=ca,
    )


def test_no_proxy_loopback_only_in_standalone(tmp_path: Path) -> None:
    env = _egress_env(_cfg(tmp_path, "http://127.0.0.1:5555"))
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert env["HTTPS_PROXY"] == "http://ach-egress:cap@127.0.0.1:5555"


def test_no_proxy_adds_the_gateway_host_in_sandboxed(tmp_path: Path) -> None:
    env = _egress_env(_cfg(tmp_path, "http://bot.ach.svc:8096"))
    assert env["NO_PROXY"] == "127.0.0.1,localhost,::1,bot.ach.svc"
    assert env["no_proxy"] == env["NO_PROXY"]
    assert env["HTTPS_PROXY"] == "http://ach-egress:cap@bot.ach.svc:8096"


async def test_reconfigure_without_hydration_rewrites_the_ca(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    (tmp_path / "home").mkdir()
    await service.configure(_cfg(tmp_path, "http://bot.ach.svc:8096", _CA1))
    bundle = tmp_path / "home" / ".ach-egress-ca-bundle.pem"
    bundle.write_text("stale")  # e.g. restored from an old HOME archive
    await service.configure(_cfg(tmp_path, "http://bot.ach.svc:8096", _CA2))
    assert "TWO" in bundle.read_text() and "stale" not in bundle.read_text()

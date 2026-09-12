# SPDX-License-Identifier: Apache-2.0
"""Harness-owned bootstrap publication and role startup contract tests."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import traceback
from pathlib import Path

import pytest


def _public() -> dict[str, object]:
    return {"agentName": "bootstrap-agent", "model": "demo"}


def _channels() -> dict[str, object]:
    return {"schemaVersion": "1", "channels": []}


def test_publish_bootstraps_writes_narrow_atomic_bundles_and_reuses_key(tmp_path: Path) -> None:
    from ach_agent.boot.bootstrap import (
        BootstrapPaths,
        publish_bootstraps,
        read_channels_bootstrap,
    )

    paths = BootstrapPaths(
        tmp_path / "channels" / "bootstrap.json", tmp_path / "engine" / "bootstrap.json"
    )
    first = publish_bootstraps(paths, _channels(), _public(), agent_name="bootstrap-agent")
    assert first.hmac_key
    assert first.channels_path == paths.channels
    assert first.engine_path == paths.engine
    assert paths.channels.stat().st_mode & 0o777 == 0o600
    assert paths.engine.stat().st_mode & 0o777 == 0o600
    assert read_channels_bootstrap(paths.channels).hmac_key == first.hmac_key

    second = publish_bootstraps(paths, _channels(), _public(), agent_name="bootstrap-agent")
    assert second.hmac_key == first.hmac_key
    assert json.loads(paths.engine.read_text(encoding="utf-8")) == _public()


def test_explicit_key_is_allowed_but_invalid_existing_bundle_fails_closed(tmp_path: Path) -> None:
    from ach_agent.boot.bootstrap import BootstrapError, BootstrapPaths, publish_bootstraps

    paths = BootstrapPaths(tmp_path / "channels.json", tmp_path / "engine.json")
    publish_bootstraps(
        paths, _channels(), _public(), agent_name="bootstrap-agent", hmac_key="operator-key"
    )
    assert (
        publish_bootstraps(
            paths, _channels(), _public(), agent_name="bootstrap-agent", hmac_key="operator-key"
        ).hmac_key
        == "operator-key"
    )

    with pytest.raises(BootstrapError, match="key"):
        publish_bootstraps(
            paths, _channels(), _public(), agent_name="bootstrap-agent", hmac_key="different-key"
        )

    paths.channels.write_text('{"schemaVersion":"1","hmacKey":"leaked-secret"}', encoding="utf-8")
    with pytest.raises(BootstrapError) as exc:
        publish_bootstraps(
            paths, _channels(), _public(), agent_name="bootstrap-agent", hmac_key="new-key"
        )
    assert "leaked-secret" not in str(exc.value)
    assert "invalid" in str(exc.value).lower()


def test_malformed_bundle_error_does_not_include_secret_or_raw_json(tmp_path: Path) -> None:
    from ach_agent.boot.bootstrap import BootstrapError, read_channels_bootstrap

    secret = "super-secret-bootstrap-key"
    path = tmp_path / "channels.json"
    path.write_text(
        json.dumps({"schemaVersion": "1", "agentName": "a", "hmacKey": secret}),
        encoding="utf-8",
    )
    with pytest.raises(BootstrapError) as exc:
        read_channels_bootstrap(path)
    assert secret not in str(exc.value)
    assert "hmacKey" not in str(exc.value)
    assert secret not in "".join(traceback.format_exception(exc.value))


def test_reads_only_bounded_regular_files_and_rejects_symlink(tmp_path: Path) -> None:
    from ach_agent.boot.bootstrap import BootstrapError, read_engine_bootstrap

    path = tmp_path / "engine.json"
    path.write_text("{" + "x" * 100 + "}", encoding="utf-8")
    with pytest.raises(BootstrapError):
        read_engine_bootstrap(path, max_bytes=32)

    target = tmp_path / "target.json"
    target.write_text(json.dumps(_public()), encoding="utf-8")
    path.unlink()
    path.symlink_to(target)
    with pytest.raises(BootstrapError):
        read_engine_bootstrap(path)

    fifo = tmp_path / "engine.fifo"
    os.mkfifo(fifo)
    with pytest.raises(BootstrapError, match="invalid"):
        read_engine_bootstrap(fifo)


def test_wait_for_bootstrap_is_bounded_and_uses_fixed_role_defaults(tmp_path: Path) -> None:
    from ach_agent.boot.bootstrap import (
        DEFAULT_BOOTSTRAP_WAIT_SECONDS,
        DEFAULT_CHANNELS_BOOTSTRAP_PATH,
        DEFAULT_CHANNELS_HOST,
        DEFAULT_CHANNELS_PORT,
        DEFAULT_ENGINE_BOOTSTRAP_PATH,
        DEFAULT_ENGINE_HOST,
        DEFAULT_ENGINE_PORT,
        DEFAULT_ENGINE_URL,
        DEFAULT_HARNESS_HOST,
        DEFAULT_HARNESS_PORT,
        DEFAULT_HARNESS_URL,
        BootstrapError,
        wait_for_engine_bootstrap,
    )

    assert DEFAULT_BOOTSTRAP_WAIT_SECONDS == 300.0
    assert DEFAULT_CHANNELS_BOOTSTRAP_PATH == Path("/run/ach-agent/channels/bootstrap.json")
    assert DEFAULT_ENGINE_BOOTSTRAP_PATH == Path("/run/ach-agent/engine/bootstrap.json")
    assert (DEFAULT_CHANNELS_HOST, DEFAULT_CHANNELS_PORT) == ("0.0.0.0", 8080)
    assert (DEFAULT_HARNESS_HOST, DEFAULT_HARNESS_PORT, DEFAULT_HARNESS_URL) == (
        "127.0.0.1",
        8090,
        "http://127.0.0.1:8090",
    )
    assert (DEFAULT_ENGINE_HOST, DEFAULT_ENGINE_PORT, DEFAULT_ENGINE_URL) == (
        "127.0.0.1",
        8081,
        "http://127.0.0.1:8081",
    )

    async def run() -> None:
        with pytest.raises(BootstrapError, match="did not become available"):
            await wait_for_engine_bootstrap(
                tmp_path / "missing.json", timeout=0.01, poll_interval=0.001
            )

    asyncio.run(run())


def test_bootstrap_paths_can_be_selected_without_reading_full_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ach_agent.boot.bootstrap import role_bootstrap_path

    monkeypatch.delenv("ACH_ENGINE_BOOTSTRAP_PATH", raising=False)
    assert role_bootstrap_path("engine") == Path("/run/ach-agent/engine/bootstrap.json")
    monkeypatch.setenv("ACH_ENGINE_BOOTSTRAP_PATH", "/tmp/operator-engine.json")
    assert role_bootstrap_path("engine") == Path("/tmp/operator-engine.json")


def test_role_wait_rejects_nonfinite_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from ach_agent.boot.roles import SplitRoleConfigError, _bootstrap_wait_seconds

    monkeypatch.setenv("ACH_BOOTSTRAP_WAIT_SECONDS", "nan")
    with pytest.raises(SplitRoleConfigError, match="positive"):
        _bootstrap_wait_seconds()


def test_main_engine_role_consumes_bootstrap_without_manual_config_path(tmp_path: Path) -> None:
    """Exercise the real ``python -m ach_agent.main --role engine`` boundary."""
    import httpx

    from ach_agent.boot.bootstrap import BootstrapPaths, publish_bootstraps

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    paths = BootstrapPaths(tmp_path / "channels.json", tmp_path / "engine.json")
    public = {
        "agentName": "bootstrap-agent",
        "engine_type": "opencode",
        "binary_path": "missing-native-binary",
        "home": str(tmp_path / "home"),
        "work_dir": str(tmp_path / "workspace"),
        "publicContext": str(tmp_path / "public"),
        "model": "demo",
    }
    publish_bootstraps(paths, _channels(), public, agent_name="bootstrap-agent")

    async def run() -> None:
        env = os.environ.copy()
        env.pop("ACH_ENGINE_CONFIG_PATH", None)
        env["ACH_CONFIG_PATH"] = str(tmp_path / "full-config-must-not-be-read.json")
        env["ACH_ENGINE_BOOTSTRAP_PATH"] = str(paths.engine)
        env["ACH_ENGINE_HOST"] = "127.0.0.1"
        env["ACH_ENGINE_PORT"] = str(port)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "ach_agent.main",
            "--role",
            "engine",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                deadline = asyncio.get_running_loop().time() + 10
                while True:
                    try:
                        response = await client.get(f"http://127.0.0.1:{port}/readyz")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if asyncio.get_running_loop().time() >= deadline:
                        stderr = (await proc.stderr.read()).decode("utf-8", "replace")
                        raise AssertionError(f"engine role did not start: {stderr}")
                    await asyncio.sleep(0.05)
        finally:
            if proc.returncode is None:
                proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)

    asyncio.run(run())

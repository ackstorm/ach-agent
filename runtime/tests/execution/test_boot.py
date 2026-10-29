# SPDX-License-Identifier: MIT
"""Executable mini boot contract: CLI help and required boot identity."""

from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _run_cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    child_env = dict(os.environ if env is None else env)
    child_env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + os.environ.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "ach_runtime", *args],
        cwd=ROOT,
        env=child_env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_cli_help_and_identity() -> None:
    for args in (('--help',), ('execution', '--help')):
        result = _run_cli(*args, env={})
        assert result.returncode == 0, result.stderr

    missing = _run_cli('execution', '--config', str(ROOT / "tests/config/fixtures/bootstrap.json"), env={})
    assert missing.returncode != 0
    assert "ACH_SANDBOX_VERIFY_KEY" in (missing.stdout + missing.stderr)


@pytest.mark.asyncio
async def test_tcp_cold_configure_start_status(public_config_fixture: object, tmp_path: Path) -> None:
    from ach_runtime.harness.tokens import engine_bearer, engine_verify_key

    key = b"mini-boot-test-key-material".ljust(32, b"!")
    public = public_config_fixture
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    bootstrap = ROOT / "tests/config/fixtures/bootstrap.json"
    fake = ROOT / "tests/engine/fixtures/ready_native_server.py"
    wrapper = tmp_path / "opencode2"
    command = shlex.join([sys.executable, str(fake), public.model.type, public.model.name,
                          str(public.limits.max_steps)])
    wrapper.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
    wrapper.chmod(0o755)
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", ""),
        "ACH_SANDBOX_VERIFY_KEY": engine_verify_key(key),
        "ACH_WORKSPACE_NAME": "gitlab-reviewer",
        "ACH_WORKSPACE_REF": "ws" * 16,
        "OPENCODE_BIN": str(wrapper),
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "ach_runtime", "execution", "--config", str(bootstrap),
         "--host", "127.0.0.1", "--port", str(port), "--workspace-dir", str(tmp_path / "workspace"),
         "--sessions-root", str(tmp_path / "sessions")],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    headers = {"Authorization": f"Bearer {engine_bearer(key, 'gitlab-reviewer')}", "X-ACH-Pod-UID": "pod-test"}
    base = f"http://127.0.0.1:{port}"
    client = httpx.Client(timeout=3)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                health = client.get(base + "/healthz")
                if health.status_code == 200:
                    headers["X-ACH-Instance-Id"] = health.headers["x-ach-instance-id"]
                    break
            except httpx.HTTPError:
                if proc.poll() is not None:
                    pytest.fail(f"mini exited early: {proc.stderr.read() if proc.stderr else ''}")
                time.sleep(.05)
        else:
            pytest.fail("mini did not open its TCP listener")
        assert client.get(base + "/readyz").status_code == 503
        assert client.get(base + "/runs").status_code == 401
        assert client.post(base + "/configure", json={}, headers={}).status_code == 401
        assert client.post(base + "/configure", json={}, headers=headers).status_code == 422
        wire_public = public.model_dump(mode="json", by_alias=True)
        credential_catalog = {"providers": {public.model.type: {"baseURL": public.infrastructure.facade_endpoint,
            "apiKey": "local-proxy", "models": {public.model.name: {"apiKey": "ambient-ek-sentinel"}}}}, "mcp": {}}
        assert client.post(base + "/configure", json={"public": wire_public, "catalog": credential_catalog},
                           headers=headers).status_code == 422
        assert client.get(base + "/readyz").status_code == 503
        assert client.post(base + "/configure", json={"public": wire_public, "catalog": {
            "providers": {public.model.type: {"baseURL": public.infrastructure.facade_endpoint,
                "apiKey": "local-proxy", "models": {public.model.name: {}}}}, "mcp": {}
        }}, headers=headers).status_code == 200
        assert client.get(base + "/readyz").status_code == 200
        wrong_binding = dict(wire_public)
        wrong_binding["agent"] = {**wire_public["agent"], "name": "other-agent"}
        invalid_update = client.post(base + "/configure", json={"public": wrong_binding, "catalog": {
            "providers": {public.model.type: {"baseURL": public.infrastructure.facade_endpoint,
                "apiKey": "local-proxy", "models": {public.model.name: {}}}}, "mcp": {}
        }}, headers=headers)
        assert invalid_update.status_code == 422
        assert client.get(base + "/readyz").status_code == 200
        request = {
            "invocationId": "inv-mini", "channel": "webhook", "workspaceRef": "ws" * 16,
            "workspaceKey": "wk", "sessionKey": "sk", "sessionRef": "se" * 16, "configVersion": public.config_version, "prompt": "hello",
            "deadlineAt": "2099-01-01T00:00:00+00:00",
        }
        foreign = {**request, "invocationId": "inv-foreign", "workspaceRef": "xx" * 16}
        assert client.post(base + "/start", json=foreign, headers=headers).status_code == 422
        started = client.post(base + "/start", json=request, headers=headers)
        assert started.status_code == 202, started.text
        started_wire = started.json()
        assert started_wire["invocationId"] == request["invocationId"]
        assert started_wire["sessionRef"] == request["sessionRef"]
        assert started_wire["configVersion"] == request["configVersion"]
        retry = client.post(base + "/start", json=request, headers=headers)
        assert retry.status_code == 202 and retry.json() == started_wire
        status = client.get(base + "/status/inv-mini", headers=headers)
        assert status.status_code == 200
        assert status.json()["workspaceRef"] == request["workspaceRef"]
        assert status.json()["sessionRef"] == request["sessionRef"]
        assert status.json()["configVersion"] == request["configVersion"]
        assert datetime.fromisoformat(status.json()["deadlineAt"].replace("Z", "+00:00")) == datetime.fromisoformat(
            request["deadlineAt"]
        )
        runs = client.get(base + "/runs", headers=headers)
        assert runs.status_code == 200
        assert [run["invocationId"] for run in runs.json()] == [request["invocationId"]]
        session_home = tmp_path / "sessions" / __import__("hashlib").sha256(request["sessionRef"].encode()).hexdigest() / "home"
        native_config = __import__("json").loads((session_home / "opencode.config.json").read_text())
        fixture = native_config["plugins"][0]["options"]["fixture"]
        assert fixture["maxSteps"] == public.limits.max_steps
        assert fixture["providers"][public.model.type]["apiKey"] == "local-proxy"
    finally:
        client.close()
        if proc.poll() is None:
            proc.terminate()
        try:
            stdout, stderr = proc.communicate(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            pytest.fail(f"mini shutdown exceeded 25s; stdout={stdout!r}, stderr={stderr!r}")
        assert "Application shutdown complete." in stderr
        assert proc.returncode in (0, -15)


def test_each_build_mints_its_own_instance_id_and_ignores_pod_uid(public_config_fixture: object, tmp_path: Path) -> None:
    import uuid

    from ach_runtime.config.public import ExecutionBootstrap
    from ach_runtime.execution.boot import build_execution_app, load_bootstrap
    from ach_runtime.harness.tokens import engine_verify_key

    environ = {
        "ACH_SANDBOX_VERIFY_KEY": engine_verify_key(b"k" * 32),
        "ACH_WORKSPACE_NAME": "ws",
        "ACH_WORKSPACE_REF": "ref",
        "ACH_POD_UID": "ignored",
    }
    bootstrap: ExecutionBootstrap = load_bootstrap(ROOT / "tests/config/fixtures/bootstrap.json", {})
    ids = [
        build_execution_app(bootstrap, environ=environ, workspace_dir=tmp_path / "w", sessions_root=tmp_path / "s")
        .state.instance_id
        for _ in range(2)
    ]
    assert ids[0] != ids[1] and ids[0] != "ignored"
    assert all(str(uuid.UUID(i, version=4)) == i for i in ids)
    environ.pop("ACH_POD_UID")
    build_execution_app(bootstrap, environ=environ, workspace_dir=tmp_path / "w", sessions_root=tmp_path / "s")


BOOTSTRAP_FIXTURE = ROOT / "tests/config/fixtures/bootstrap.json"


def test_load_bootstrap_from_env() -> None:
    from ach_runtime.execution.boot import load_bootstrap

    inline = BOOTSTRAP_FIXTURE.read_text(encoding="utf-8")
    bootstrap = load_bootstrap(None, {"ACH_BOOTSTRAP_JSON": inline})
    assert bootstrap.bootstrap_version == "workspace-v3"
    assert bootstrap == load_bootstrap(BOOTSTRAP_FIXTURE, {})


def test_load_bootstrap_env_and_config_are_mutually_exclusive() -> None:
    from ach_runtime.execution.boot import load_bootstrap

    with pytest.raises(ValueError, match="mutually exclusive"):
        load_bootstrap(BOOTSTRAP_FIXTURE, {"ACH_BOOTSTRAP_JSON": "{}"})


def test_load_bootstrap_without_a_source_is_an_error() -> None:
    from ach_runtime.execution.boot import load_bootstrap

    with pytest.raises(ValueError, match="ACH_BOOTSTRAP_JSON"):
        load_bootstrap(None, {})


def test_load_bootstrap_invalid_json_names_the_variable_not_its_value() -> None:
    from ach_runtime.execution.boot import load_bootstrap

    with pytest.raises(ValueError) as exc:
        load_bootstrap(None, {"ACH_BOOTSTRAP_JSON": "not-json-secret-sentinel"})
    assert "ACH_BOOTSTRAP_JSON" in str(exc.value)
    assert "secret-sentinel" not in str(exc.value)


def test_cli_without_bootstrap_source_fails_before_binding_the_port() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {"ACH_SANDBOX_VERIFY_KEY": "x", "ACH_WORKSPACE_NAME": "ws", "ACH_WORKSPACE_REF": "ref"}
    result = _run_cli("execution", "--host", "127.0.0.1", "--port", str(port), env=env)
    assert result.returncode != 0
    assert "no execution bootstrap" in (result.stdout + result.stderr)
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", port)) != 0

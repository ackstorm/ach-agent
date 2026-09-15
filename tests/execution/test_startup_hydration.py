"""Startup hydration acceptance tests for the engine-role service."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from ach_agent.execution.app import create_execution_app, create_execution_health_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import PublicEngineConfig


def _public(tmp_path: Path, batch: Path, *, binary: str = "true") -> PublicEngineConfig:
    return PublicEngineConfig(
        agent_name="startup-test",
        binary_path=binary,
        home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "workspace"),
        hydration_dir=str(batch),
    )


def _batch(tmp_path: Path, name: str = "batch") -> Path:
    batch = tmp_path / "transfer" / f".ach-harness-shared-files-{name}"
    (batch / "skills" / "fixture-skill").mkdir(parents=True)
    (batch / "skills" / "fixture-skill" / "SKILL.md").write_text("startup skill")
    (batch / "prompts").mkdir()
    (batch / "prompts" / "system.txt").write_text("startup prompt")
    (batch / "artifacts").mkdir()
    (batch / "artifacts" / "manifest.json").write_text("{}")
    return batch


async def _status(app, path: str) -> int:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://e") as client:
        return (await client.get(path)).status_code


@pytest.mark.asyncio
async def test_startup_installs_real_files_deletes_batch_and_does_not_launch_native(
    tmp_path: Path,
) -> None:
    batch = _batch(tmp_path)
    service = ExecutionService(None, None)
    app = create_execution_app(service)

    assert await _status(app, "/healthz") == 503
    assert await _status(app, "/readyz") == 503
    assert await _status(app, "/execution/v1/health") == 200

    await service.configure(_public(tmp_path, batch))

    assert not batch.exists()
    skill = tmp_path / "home" / ".config" / "opencode" / "skills" / "fixture-skill" / "SKILL.md"
    assert skill.is_file()
    prompt = tmp_path / "home" / ".ach-state" / "prompts" / "system.txt"
    assert prompt.read_text() == "startup prompt"
    assert (tmp_path / "home" / ".ach-state" / "artifacts" / "manifest.json").is_file()
    assert service.driver is not None
    assert not service.pool._servers
    assert await _status(app, "/healthz") == 200
    assert await _status(app, "/readyz") == 200
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("engine_type", ["opencode", "pi"])
async def test_nested_persistent_workspace_is_not_relocated_during_startup(
    tmp_path: Path, engine_type: str
) -> None:
    batch = _batch(tmp_path, f"nested-{engine_type}")
    home = tmp_path / "home"
    workspace = home / "workspace"
    nested = workspace / "session-keyed"
    nested.mkdir(parents=True)
    marker = nested / "native-session.jsonl"
    marker.write_text("history", encoding="utf-8")
    service = ExecutionService(None, None)

    public = _public(tmp_path, batch, binary="true").model_copy(
        update={"engine_type": engine_type, "home": str(home), "work_dir": str(nested)}
    )
    await service.configure(public)

    assert marker.read_text(encoding="utf-8") == "history"
    assert not (tmp_path / "workspace").exists()
    await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["binary", "copy", "delete"])
async def test_startup_failure_marks_service_unhealthy_and_requests_shutdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    batch = _batch(tmp_path, failure)
    if failure == "copy":
        monkeypatch.setattr(
            "ach_agent.engine.context.install_hydration",
            lambda *args: (_ for _ in ()).throw(OSError("copy failed")),
        )
    if failure == "delete":
        monkeypatch.setattr(
            "ach_agent.engine.context.delete_hydration_batch",
            lambda *_args: (_ for _ in ()).throw(OSError("delete failed")),
        )
    binary = "missing-startup-test-binary" if failure == "binary" else "true"
    service = ExecutionService(None, None)
    with pytest.raises((OSError, FileNotFoundError, RuntimeError)):
        await service.configure(_public(tmp_path, batch, binary=binary))
    assert service._unhealthy
    assert service.shutdown_requested
    assert await _status(create_execution_app(service), "/readyz") == 503


@pytest.mark.asyncio
async def test_execution_health_app_has_no_internal_routes(tmp_path: Path) -> None:
    service = ExecutionService(None, None)
    app = create_execution_health_app(service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://e") as client:
        assert (await client.get("/healthz")).status_code == 503
        assert (await client.get("/readyz")).status_code == 503
        assert (await client.get("/execution/v1/health")).status_code == 404
    await service.close()


@pytest.mark.asyncio
async def test_same_instance_reconnect_consumes_fresh_batch(tmp_path: Path) -> None:
    first = _batch(tmp_path, "first")
    service = ExecutionService(None, None)
    await service.configure(_public(tmp_path, first))
    second = _batch(tmp_path, "second")

    await service.configure(_public(tmp_path, second))

    assert not second.exists()
    await service.close()


@pytest.mark.asyncio
async def test_second_controller_is_rejected_during_owned_startup(tmp_path: Path) -> None:
    batch = _batch(tmp_path, "controller")
    service = ExecutionService(None, None)
    started = asyncio.Event()
    release = asyncio.Event()
    initialize = service._initialize_native

    async def delayed_initialize(public, driver):
        started.set()
        await release.wait()
        await initialize(public, driver)

    service._initialize_native = delayed_initialize
    first = asyncio.create_task(service.claim_controller("first", config=_public(tmp_path, batch)))
    await started.wait()
    second = asyncio.create_task(
        service.claim_controller("second", config=_public(tmp_path, batch))
    )
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await first
    with pytest.raises(RuntimeError, match="already has a controller"):
        await second
    await service.release_controller("first")
    await service.close()

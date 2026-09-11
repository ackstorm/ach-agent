from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from ach_agent.engine.workspace import WorkspaceHookTimedOut, run_public_hook
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import (
    AcquireRequest,
    PublicEngineConfig,
    ReleaseRequest,
    WorkspaceHandoffRequest,
    WorkspaceHook,
    WorkspacePrepareRequest,
)


def _prepare(tmp_path: Path, **changes: object) -> WorkspacePrepareRequest:
    values: dict[str, object] = {
        "controller_id": "controller",
        "invocation_id": "invocation",
        "session_key": "group/project:1",
        "event_id": "event-1",
        "channel_name": "review",
        "delivery_context": {"project_path": "group/project"},
        "home": str(tmp_path / "home"),
        "work_dir": str(tmp_path / "work"),
        "prepare": {
            "script": "printf '%s' \"$ACH_EVENT_PROJECT_PATH\" > prepared.txt",
            "env": {},
            "timeout_seconds": 2,
        },
        "remaining_seconds": 5,
    }
    values.update(changes)
    return WorkspacePrepareRequest.model_validate(values)


@pytest.mark.asyncio
async def test_public_prepare_completes_before_native_acquire(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    result = await service.prepare_workspace(_prepare(tmp_path))

    workspace = Path(result["workspace"])
    assert (workspace / "prepared.txt").read_text() == "group/project"
    assert fake_driver.servers == []
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_public_prepare_failure_does_not_launch_native(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare={"script": "exit 17", "timeout_seconds": 2})

    with pytest.raises(RuntimeError, match="public hook exited 17"):
        await service.prepare_workspace(request)
    assert fake_driver.servers == []
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_private_only_prepare_still_notifies_harness_on_stop(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare=None, cleanup=None)
    await service.prepare_workspace(request)
    await service.pool.discard(request.session_key)
    events = service.controller_events()
    assert events is not None
    event = await asyncio.wait_for(events.get(), timeout=1)
    assert event["kind"] == "workspace_stopped"
    assert event["event_id"] == "event-1"
    await service.release_controller("controller")


def test_workspace_hook_wire_has_no_secret_or_path_fields(tmp_path: Path) -> None:
    request = _prepare(tmp_path)
    with pytest.raises(ValidationError):
        WorkspacePrepareRequest.model_validate(
            {**request.model_dump(), "secret_env": {"TOKEN": "value"}}
        )
    with pytest.raises(ValidationError):
        WorkspaceHandoffRequest.model_validate(
            {
                "controller_id": "controller",
                "invocation_id": "invocation",
                "session_key": "group/project:1",
                "home": str(tmp_path / "home"),
                "work_dir": str(tmp_path / "work"),
                "bundle_path": ".ach-handoff/bundle",
                "head": "HEAD",
                "bundle": "private-file-content",
                "remaining_seconds": 5,
            }
        )


@pytest.mark.asyncio
async def test_controller_loss_cancels_public_prepare(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path, prepare={"script": "sleep 30", "timeout_seconds": 30})
    task = asyncio.create_task(service.prepare_workspace(request))
    await asyncio.sleep(0.05)
    await service.release_controller("controller")
    with pytest.raises(BaseException):
        await task
    assert service.can_accept_controller


@pytest.mark.skipif(os.name != "posix", reason="requires process ownership procfs")
@pytest.mark.asyncio
async def test_hook_timeout_reaps_fast_detached_setsid_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    hook = {
        "script": (
            "python3 -c 'import os,time; p=\"" + str(pid_file) + '"; c=os.fork(); '
            '(os.setsid(), open(p,"w").write(str(os.getpid())), time.sleep(30)) '
            "if c == 0 else time.sleep(30)'"
        ),
        "timeout_seconds": 0.2,
    }
    with pytest.raises(WorkspaceHookTimedOut):
        await run_public_hook(
            WorkspaceHook(**hook),
            cwd=tmp_path,
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
            remaining_seconds=2,
        )
    for _ in range(100):
        if pid_file.exists():
            pid = int(pid_file.read_text())
            if not Path(f"/proc/{pid}").exists():
                break
        await asyncio.sleep(0.02)
    assert pid_file.exists()
    assert not Path(f"/proc/{int(pid_file.read_text())}").exists()


def _bundle(source: Path, target: Path) -> str:
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "README").write_text("approved\n")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "initial"], check=True)
    head = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    subprocess.run(["git", "-C", str(source), "bundle", "create", str(target), "--all"], check=True)
    return head


@pytest.mark.asyncio
async def test_handoff_preserves_workspace_root_and_rejects_destination_symlink(
    fake_driver, tmp_path: Path
) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    prepared = await service.prepare_workspace(_prepare(tmp_path, prepare=None))
    workspace = Path(prepared["workspace"])
    artifact_dir = workspace / ".ach-handoff"
    artifact_dir.mkdir()
    bundle = artifact_dir / "repo.bundle"
    head = _bundle(tmp_path / "source", bundle)
    root_inode = workspace.stat().st_ino
    result = await service.handoff_workspace(
        WorkspaceHandoffRequest(
            controller_id="controller",
            invocation_id="invocation",
            session_key="group/project:1",
            home=str(tmp_path / "home"),
            work_dir=str(tmp_path / "work"),
            bundle_path=".ach-handoff/repo.bundle",
            head=head,
            remaining_seconds=5,
        )
    )
    assert Path(result["workspace"]).stat().st_ino == root_inode
    assert (workspace / "repo" / "README").read_text() == "approved\n"

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "repo.bundle").write_bytes(b"not a bundle")
    artifact_dir.rmdir()
    artifact_dir.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlink"):
        await service.handoff_workspace(
            WorkspaceHandoffRequest(
                controller_id="controller",
                invocation_id="invocation-2",
                session_key="group/project:1",
                home=str(tmp_path / "home"),
                work_dir=str(tmp_path / "work"),
                bundle_path=".ach-handoff/repo.bundle",
                head=head,
                remaining_seconds=5,
            )
        )
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_warm_expiry_stops_native_before_public_cleanup(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(
        tmp_path,
        prepare=None,
        cleanup={
            "script": 'printf cleanup > "$ACH_WORKSPACE/cleanup-marker"',
            "timeout_seconds": 2,
        },
    )
    prepared = await service.prepare_workspace(request)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="invocation",
            lane_key="group/project:1",
            conversation_key="repo",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(work_dir=str(tmp_path / "work"), home=str(tmp_path / "home")),
        )
    )
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="invocation",
            idle_ttl_seconds=0.02,
        )
    )
    marker = Path(prepared["workspace"]) / "cleanup-marker"
    for _ in range(100):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.read_text() == "cleanup"
    assert fake_driver.stopped_servers and fake_driver.stopped_servers[0].stopped
    events = service.controller_events()
    assert events is not None
    stopped = await asyncio.wait_for(events.get(), timeout=1)
    assert stopped == {
        "kind": "workspace_stopped",
        "session_key": "group/project:1",
        "event_id": "event-1",
        "invocation_id": "invocation",
        "workspace": str(marker.parent),
    }
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_new_prepare_cancels_stale_warm_cleanup(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    first = _prepare(
        tmp_path,
        prepare=None,
        cleanup={
            "script": 'printf first > "$ACH_WORKSPACE/cleanup-marker"',
            "timeout_seconds": 2,
        },
    )
    await service.prepare_workspace(first)
    handle = await service.acquire(
        AcquireRequest(
            controller_id="controller",
            invocation_id="invocation",
            lane_key="group/project:1",
            conversation_key="repo",
            reuse=True,
            remaining_seconds=5,
            config=PublicEngineConfig(work_dir=str(tmp_path / "work"), home=str(tmp_path / "home")),
        )
    )
    await service.release(
        ReleaseRequest(
            controller_id="controller",
            execution_id=handle.execution_id,
            invocation_id="invocation",
            idle_ttl_seconds=0.05,
        )
    )
    second = _prepare(
        tmp_path,
        invocation_id="invocation-2",
        event_id="event-2",
        prepare=None,
        cleanup={
            "script": 'printf second > "$ACH_WORKSPACE/cleanup-marker"',
            "timeout_seconds": 2,
        },
    )
    await service.prepare_workspace(second)
    await asyncio.sleep(0.08)
    workspace = Path(second.work_dir) / "group-project-1-ac555c4f"
    assert not (workspace / "cleanup-marker").exists()
    await service.release_controller("controller")


@pytest.mark.asyncio
async def test_execution_client_sends_only_public_hook_fields(tmp_path: Path) -> None:
    from ach_agent.boot.execution_client import ExecutionClient

    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, json={"status": "ok", "workspace": "/workspace/session"}, request=request
        )

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    try:
        await client.prepare_workspace(_prepare(tmp_path))
    finally:
        await client.close()
    prepare = seen["prepare"]
    assert isinstance(prepare, dict) and str(prepare["script"]).startswith("printf")
    assert "secret_env" not in seen and "secretEnv" not in seen
    assert "private_scratch" not in seen

# SPDX-License-Identifier: Apache-2.0
"""new_session detection, handoff import and hooks.sessionStart in the mini-harness."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.execution.service import ExecutionService, SessionHookFailed
from ach_agent.execution.wire import (
    HookSpec,
    PublicEngineConfig,
    WorkspacePrepareRequest,
    WorkspaceSessionStartRequest,
)


def _engine_config() -> EngineConfig:
    return EngineConfig(model_base_url="http://127.0.0.1:9/v1", engine_type="opencode")


def _prepare(tmp_path: Path, **changes: object) -> WorkspacePrepareRequest:
    values: dict[str, object] = {
        "controller_id": "controller",
        "invocation_id": "invocation",
        "session_key": "group/project:1",
        "event_id": "event-1",
        "home": str(tmp_path / "home"),
        "work_dir": str(tmp_path / "work"),
        "remaining_seconds": 5,
    }
    values.update(changes)
    return WorkspacePrepareRequest.model_validate(values)


async def _chunks(data: bytes, size: int = 8192):
    for i in range(0, len(data), size):
        yield data[i : i + size]


def _tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


# --------------------------------------------------------------------------- new_session


async def test_new_session_true_until_session_start_succeeds(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")

    first = await service.prepare_workspace(_prepare(tmp_path, invocation_id="inv-1"))
    assert first["new_session"] is True
    await service.session_start(
        WorkspaceSessionStartRequest(controller_id="controller", invocation_id="inv-1")
    )
    assert (Path(first["workspace"]) / ".ach-session-started").is_file()

    await service.release_controller("controller")
    await service.claim_controller("controller")
    second = await service.prepare_workspace(_prepare(tmp_path, invocation_id="inv-2"))
    assert second["new_session"] is False


async def test_failed_session_start_leaves_the_session_new(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    await service.configure(PublicEngineConfig(hook_session_start=HookSpec(script="exit 3")))

    first = await service.prepare_workspace(_prepare(tmp_path, invocation_id="inv-1"))
    assert first["new_session"] is True
    with pytest.raises(SessionHookFailed):
        await service.session_start(
            WorkspaceSessionStartRequest(controller_id="controller", invocation_id="inv-1")
        )
    assert not (Path(first["workspace"]) / ".ach-session-started").exists()
    await service.cancel("controller", "inv-1")

    second = await service.prepare_workspace(_prepare(tmp_path, invocation_id="inv-2"))
    assert second["new_session"] is True


async def test_restored_workspace_with_marker_is_not_new(fake_driver, tmp_path: Path) -> None:
    from ach_agent.engine.workspace import workspace_dir

    workspace = workspace_dir(str(tmp_path / "work"), "group/project:1")
    workspace.mkdir(parents=True)
    (workspace / ".ach-session-started").touch()  # as restored from a HOME archive
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    result = await service.prepare_workspace(_prepare(tmp_path))
    assert result["new_session"] is False


# --------------------------------------------------------------------------- handoff import


async def test_handoff_lands_under_workspace(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    result = await service.prepare_workspace(request)
    workspace = Path(result["workspace"])

    archive = _tar_gz({"repo/f.txt": b"hi"})
    await service.import_handoff("controller", request.invocation_id, _chunks(archive))

    assert (workspace / "handoff" / "repo" / "f.txt").read_text() == "hi"


async def test_handoff_replaces_a_previous_one(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    result = await service.prepare_workspace(request)
    workspace = Path(result["workspace"])

    await service.import_handoff(
        "controller", request.invocation_id, _chunks(_tar_gz({"old.txt": b"old"}))
    )
    assert (workspace / "handoff" / "old.txt").exists()

    await service.import_handoff(
        "controller", request.invocation_id, _chunks(_tar_gz({"new.txt": b"new"}))
    )
    assert not (workspace / "handoff" / "old.txt").exists()
    assert (workspace / "handoff" / "new.txt").read_text() == "new"


async def test_handoff_rejects_traversal_archive(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../escape")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))

    with pytest.raises(tarfile.TarError):
        await service.import_handoff("controller", request.invocation_id, _chunks(buf.getvalue()))


async def test_handoff_requires_a_live_reservation(fake_driver) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")

    with pytest.raises(ValueError, match="reservation"):
        await service.import_handoff("controller", "no-such-invocation", _chunks(_tar_gz({})))


# --------------------------------------------------------------------------- sessionStart


async def test_session_start_runs_hook_once_with_forward_env(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    marker = tmp_path / "marker.txt"
    await service.claim_controller("controller")
    await service.configure(
        PublicEngineConfig(
            engine_env_names=["VISIBLE_VAR"],
            hook_session_start=HookSpec(
                script=(
                    f'printf "%s|%s|%s|%s|%s" "$VISIBLE_VAR" "${{OTHER_VAR:-missing}}" '
                    '"$ACH_WORKSPACE" "$ACH_HANDOFF_DIR" "$ACH_SESSION_KEY" '
                    f"> {marker}"
                )
            ),
        )
    )
    request = _prepare(tmp_path)
    result = await service.prepare_workspace(request)
    workspace = Path(result["workspace"])

    import os

    os.environ["VISIBLE_VAR"] = "seen"
    os.environ["OTHER_VAR"] = "unfiltered"  # not in engine_env_names — must not reach the hook
    try:
        await service.session_start(
            WorkspaceSessionStartRequest(
                controller_id="controller", invocation_id=request.invocation_id
            )
        )
    finally:
        os.environ.pop("VISIBLE_VAR", None)
        os.environ.pop("OTHER_VAR", None)

    parts = marker.read_text().split("|")
    assert parts[0] == "seen"
    assert parts[1] == "missing"
    assert parts[2] == str(workspace)
    assert parts[3] == str(workspace / "handoff")
    assert parts[4] == "group/project:1"


async def test_session_start_is_a_no_op_without_a_hook(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)

    await service.session_start(
        WorkspaceSessionStartRequest(
            controller_id="controller", invocation_id=request.invocation_id
        )
    )


async def test_session_start_raises_on_nonzero_exit(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    await service.configure(PublicEngineConfig(hook_session_start=HookSpec(script="exit 3")))
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)

    with pytest.raises(SessionHookFailed):
        await service.session_start(
            WorkspaceSessionStartRequest(
                controller_id="controller", invocation_id=request.invocation_id
            )
        )


async def test_session_start_rejects_a_second_call(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    request = _prepare(tmp_path)
    await service.prepare_workspace(request)

    await service.session_start(
        WorkspaceSessionStartRequest(
            controller_id="controller", invocation_id=request.invocation_id
        )
    )
    with pytest.raises(ValueError, match="already ran"):
        await service.session_start(
            WorkspaceSessionStartRequest(
                controller_id="controller", invocation_id=request.invocation_id
            )
        )


# --------------------------------------------------------------------------- sessionSuspend


async def test_session_suspend_runs_before_native_stop_on_discard(
    fake_driver, tmp_path: Path
) -> None:
    marker = tmp_path / "suspend-marker.txt"
    work_dir = tmp_path / "work"
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    await service.configure(
        PublicEngineConfig(
            work_dir=str(work_dir),
            hook_session_suspend=HookSpec(script=f'printf "%s" "$ACH_SESSION_KEY" > {marker}'),
        )
    )
    request = _prepare(tmp_path, work_dir=str(work_dir))
    await service.prepare_workspace(request)

    await service.pool.acquire(request.session_key, _engine_config())
    await service.pool.discard(request.session_key)

    assert marker.read_text() == request.session_key


async def test_session_suspend_is_a_no_op_without_a_hook(fake_driver, tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    service = ExecutionService(fake_driver, {})
    await service.claim_controller("controller")
    await service.configure(PublicEngineConfig(work_dir=str(work_dir)))
    request = _prepare(tmp_path, work_dir=str(work_dir))
    await service.prepare_workspace(request)

    await service.pool.acquire(request.session_key, _engine_config())
    await service.pool.discard(request.session_key)

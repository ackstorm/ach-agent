# SPDX-License-Identifier: MIT
"""Lifecycle and archive-facing session state regressions."""

import asyncio
import errno
import io
import json
import tarfile
import threading
from types import SimpleNamespace
from pathlib import Path

import pytest

from ach_runtime.engine.opencode import OpenCodeEngine, SnapshotManifest, _NativeSession
from ach_runtime.execution.service import ExecutionService
from ach_runtime.execution.sessions import SESSION_STARTED_MARKER, SessionRegistry
from ach_runtime.execution.wire import StartRequest
from ach_runtime.storage.archive import pack


def test_inventory_includes_sessions_recovered_from_disk(tmp_path: Path) -> None:
    first = SessionRegistry(tmp_path)
    state = first.get_or_create("opaque-session-ref")
    (state.home / SESSION_STARTED_MARKER).touch()
    (state.home / ".ach-registry.json").write_text(
        json.dumps({"sessionRef": "opaque-session-ref", "nativeSessionId": "sid-real"}),
        encoding="utf-8",
    )

    recreated = SessionRegistry(tmp_path)
    records = recreated.inventory()

    assert len(records) == 1
    assert records[0].session_ref == "opaque-session-ref"
    assert records[0].started is True
    assert records[0].native_session_id == "sid-real"


class _IdleEngine:
    def __init__(self) -> None:
        self.start_calls: list[str] = []
        self.suspend_calls: list[str] = []
        self.suspend_result = True
        self.on_suspend = None
        self._sessions = {}

    async def start(self, invocation, *, home, scratch, password=None):
        self.start_calls.append(invocation.invocation_id)
        raise AssertionError("the test must not start a native invocation")

    async def suspend(self, session_ref: str) -> bool:
        self.suspend_calls.append(session_ref)
        if self.on_suspend is not None:
            self.on_suspend()
        if self.suspend_result:
            native = self._sessions.get(session_ref)
            if native is not None:
                native.alive = False
        return self.suspend_result

    async def close(self) -> None:
        return None


class _SettlingEngine:
    """Starts and settles to "completed" immediately — an owned fake adapted explicitly to
    the `Engine` Protocol (including `retire_session`), rather than a getattr fallback."""

    def __init__(self) -> None:
        self._sessions: dict[str, object] = {}
        self.start_calls: list[str] = []
        self.retire_calls: list[str] = []

    async def start(self, invocation, *, home, scratch, password=None):
        from ach_runtime.engine.opencode import EngineReceipt

        self.start_calls.append(invocation.invocation_id)
        return EngineReceipt(native_session_id="native-sid", input_id="input-1")

    async def status(self, invocation_id: str) -> str:
        return "completed"

    async def wait_settled(self, invocation_id: str) -> str:
        return "completed"

    def result(self, invocation_id: str) -> str | None:
        return "ok"

    async def cancel(self, invocation_id, reason, *, _halt_budget=None) -> None:
        return None

    async def suspend(self, session_ref: str) -> bool:
        return True

    async def retire_session(self, session_ref: str) -> None:
        self.retire_calls.append(session_ref)

    async def close(self) -> None:
        return None


def _service(tmp_path: Path, *, engine=None, workspace_ref: str | None = None, public=None):
    workspace = tmp_path / "cold" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    service = ExecutionService(
        engine=engine or _IdleEngine(),
        sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=workspace,
        workspace_ref=workspace_ref,
    )
    if public is not None:
        service.configure(public)
    return service


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


async def _chunks(data: bytes):
    yield data


async def test_archive_cancel_drains_extractor_and_preserves_previous_workspace(
    tmp_path: Path, monkeypatch
) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    workspace = service._workspace_dir
    sentinel = workspace / "sentinel"
    sentinel.write_text("old-tree")
    archive = _tar_bytes({"new-file": b"new-tree"})
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    extract = snapshots.extract

    def slow_extract(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test release was not signalled")
        try:
            return extract(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(snapshots, "extract", slow_extract)
    task = asyncio.create_task(snapshots.import_workspace(service, _chunks(archive)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    await asyncio.sleep(0.03)
    assert not task.done(), "cancellation must wait for the owned extractor"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert finished.is_set()
    assert sentinel.read_text() == "old-tree"
    assert not (workspace / "new-file").exists()
    assert list(workspace.glob(".workspace-import-*")) == []


async def test_workspace_import_freezes_start_and_targets_only_pinned_workspace(
    tmp_path: Path, public_config_fixture
) -> None:
    import ach_runtime.execution.snapshots as snapshots

    engine = _IdleEngine()
    service = _service(tmp_path, engine=engine, workspace_ref="ws-ref")
    workspace = service._workspace_dir
    (workspace / "old").write_text("replace me")
    archive = _tar_bytes({"restored": b"workspace"})
    waiting, release = asyncio.Event(), asyncio.Event()

    async def paused_chunks():
        waiting.set()
        await release.wait()
        yield archive

    task = asyncio.create_task(snapshots.import_workspace(service, paused_chunks()))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    request = StartRequest(workspaceKey="wk", sessionKey="sk", 
        invocationId="racing-start", channel="webhook", workspaceRef="ws-ref",
        sessionRef="session-ref", configVersion=public_config_fixture.config_version,
        prompt="must not start", deadlineAt="2026-10-01T23:59:59+00:00",
    )
    with pytest.raises(RuntimeError, match="archive operation"):
        await service.start(request)
    assert engine.start_calls == []
    release.set()
    await task

    assert (workspace / "restored").read_text() == "workspace"
    assert not (workspace / "old").exists()


async def test_workspace_import_rejects_warm_service(tmp_path: Path, public_config_fixture) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path, public=public_config_fixture)
    with pytest.raises(RuntimeError, match="cold"):
        await snapshots.import_workspace(service, _chunks(_tar_bytes({"file": b"x"})))


async def test_workspace_publication_failure_restores_previous_tree(tmp_path: Path, monkeypatch) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    workspace = service._workspace_dir
    sentinel = workspace / "sentinel"
    sentinel.write_text("prior")
    original_rename = Path.rename
    failed = False

    def fail_staged_publication(source: Path, target: Path):
        nonlocal failed
        if source.parent.name == "workspace" and source.name == "new" and not failed:
            failed = True
            raise OSError("simulated Workspace publish failure")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_staged_publication)
    with pytest.raises(OSError, match="simulated"):
        await snapshots.import_workspace(service, _chunks(_tar_bytes({"new": b"data"})))

    assert sentinel.read_text() == "prior"
    assert not (workspace / "new").exists()
    assert list(workspace.glob(".workspace-import-*")) == []


async def test_workspace_import_preserves_mount_root_and_rolls_back_contents(
    tmp_path: Path, monkeypatch
) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    workspace = service._workspace_dir
    (workspace / "old-a").write_text("old-a")
    (workspace / "old-b").write_text("old-b")
    inode = workspace.stat().st_ino
    original_rename = Path.rename

    def reject_root_move(source: Path, target: Path):
        if source == workspace:
            raise OSError(errno.EBUSY, "mount root cannot be moved")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", reject_root_move)
    await snapshots.import_workspace(service, _chunks(_tar_bytes({"new": b"new"})))
    assert workspace.stat().st_ino == inode
    assert (workspace / "new").read_bytes() == b"new"
    assert not (workspace / "old-a").exists()

    # Fail publication after one staged child is visible; every original child
    # must be restored and the mount root itself must remain the same inode.
    (workspace / "old-a").write_text("old-a")
    (workspace / "old-b").write_text("old-b")
    failed = False

    def fail_second_child(source: Path, target: Path):
        nonlocal failed
        if source.parent.name == "workspace" and source.name == "new-b" and not failed:
            failed = True
            raise OSError("simulated later child publication failure")
        return original_rename(source, target)

    monkeypatch.setattr(Path, "rename", fail_second_child)
    with pytest.raises(OSError, match="later child"):
        await snapshots.import_workspace(
            service, _chunks(_tar_bytes({"new-a": b"a", "new-b": b"b"}))
        )
    assert workspace.stat().st_ino == inode
    assert (workspace / "old-a").read_text() == "old-a"
    assert (workspace / "old-b").read_text() == "old-b"
    assert not (workspace / "new-a").exists()

    frozen_during_cleanup: list[bool] = []
    real_rmtree = snapshots.shutil.rmtree

    def record_cleanup(path, *args, **kwargs):
        if Path(path).name.startswith(".workspace-import-"):
            frozen_during_cleanup.append(service._workspace_frozen)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(snapshots.shutil, "rmtree", record_cleanup)
    with pytest.raises(Exception):
        await snapshots.import_workspace(service, _chunks(b"not an archive"))
    assert frozen_during_cleanup == [True]
    assert service._workspace_frozen is False
    assert (workspace / "old-a").read_text() == "old-a"


async def test_handoff_staging_stays_on_workspace_device_and_rejects_workspace_root(
    tmp_path: Path, monkeypatch
) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    workspace = service._workspace_dir
    target = workspace / "handoff"
    target.mkdir()
    (target / "old").write_text("old")
    original_rename = Path.rename

    def reject_cross_device(source: Path, destination: Path):
        if source.parent != workspace and not source.is_relative_to(workspace):
            raise OSError(errno.EXDEV, "cross-device rename")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", reject_cross_device)
    await snapshots.import_handoff(service, str(target), _chunks(_tar_bytes({"new": b"ok"})))
    assert (target / "new").read_bytes() == b"ok"
    assert not (target / "old").exists()

    before = {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()}
    with pytest.raises(ValueError):
        await snapshots.import_handoff(service, str(workspace), _chunks(_tar_bytes({"x": b"x"})))
    assert {p.name: p.read_bytes() for p in target.iterdir() if p.is_file()} == before


async def test_handoff_publish_failure_restores_previous_tree(tmp_path: Path, monkeypatch) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    target = service._workspace_dir / "handoff"
    target.mkdir()
    sentinel = target / "sentinel"
    sentinel.write_text("prior")
    original_rename = Path.rename
    failed = False

    def fail_staged_handoff(source: Path, destination: Path):
        nonlocal failed
        if source.parent.name.startswith(".ach-handoff-") and source.name == "staged" and not failed:
            failed = True
            raise OSError("simulated handoff publish failure")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", fail_staged_handoff)
    with pytest.raises(OSError, match="simulated"):
        await snapshots.import_handoff(
            service, str(target), _chunks(_tar_bytes({"new": b"handoff"}))
        )

    assert sentinel.read_text() == "prior"
    assert not (target / "new").exists()
    assert list(service._workspace_dir.parent.glob(".ach-handoff-*")) == []


async def test_hydration_failure_rolls_back_all_managed_directories(
    tmp_path: Path, monkeypatch
) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    state = service._sessions.get_or_create("session-ref")
    public = service._workspace_dir / ".ach-state"
    for kind in ("skills", "prompts", "artifacts"):
        target = public / kind
        target.mkdir(parents=True)
        (target / "previous").write_text("old")
    original_rename = Path.rename
    failed = False

    def fail_prompts_install(source: Path, destination: Path):
        nonlocal failed
        if source.name == "prompts.staged" and destination == public / "prompts" and not failed:
            failed = True
            raise OSError("simulated prompts install failure")
        return original_rename(source, destination)

    monkeypatch.setattr(Path, "rename", fail_prompts_install)
    archive = _tar_bytes({"skills/new/SKILL.md": b"new", "prompts/new.md": b"new"})
    with pytest.raises(OSError, match="simulated"):
        await snapshots.import_hydration(service, "session-ref", _chunks(archive))

    for kind in ("skills", "prompts", "artifacts"):
        assert (public / kind / "previous").read_text() == "old"
        assert not (public / kind / "new").exists()
    assert not (state.home / ".ach-state").exists()


async def test_idle_session_hydration_can_repeat(tmp_path: Path) -> None:
    import ach_runtime.execution.snapshots as snapshots

    session_ref = "session-ref"
    engine = _IdleEngine()
    service = _service(tmp_path, engine=engine)
    state = service._sessions.get_or_create(session_ref)
    public = service._workspace_dir / ".ach-state"
    skills_link = state.home / ".config" / "opencode" / "skills"
    skills_link.parent.mkdir(parents=True)
    skills_link.symlink_to(Path("../../.ach-state/skills"), target_is_directory=True)

    await snapshots.import_hydration(
        service, session_ref, _chunks(_tar_bytes({"prompts/a.md": b"context A"}))
    )
    state.native_session_id = "native-sid"
    native = SimpleNamespace(alive=True)
    engine._sessions[session_ref] = native
    observed_prior: list[str] = []
    engine.on_suspend = lambda: observed_prior.append(
        (public / "prompts/a.md").read_text(encoding="utf-8")
    )

    engine.suspend_result = False
    with pytest.raises(RuntimeError, match="suspend"):
        await snapshots.import_hydration(
            service, session_ref, _chunks(_tar_bytes({"prompts/b.md": b"context B"}))
        )
    assert observed_prior == ["context A"]
    assert (public / "prompts/a.md").read_text(encoding="utf-8") == "context A"
    assert (state.home / ".ach-state/prompts/a.md").read_text(encoding="utf-8") == "context A"
    assert not (public / "prompts/b.md").exists()
    assert native.alive is True

    engine.suspend_result = True
    await snapshots.import_hydration(
        service, session_ref, _chunks(_tar_bytes({"prompts/b.md": b"context B"}))
    )
    assert observed_prior == ["context A", "context A"]
    assert not (public / "prompts/a.md").exists()
    assert (public / "prompts/b.md").read_text(encoding="utf-8") == "context B"
    assert (state.home / ".ach-state/prompts/b.md").read_text(encoding="utf-8") == "context B"
    assert state.native_session_id == "native-sid"
    assert state.home.is_dir()
    assert skills_link.is_symlink()
    assert not Path(skills_link.readlink()).is_absolute()
    assert skills_link.resolve() == (state.home / ".ach-state" / "skills").resolve()
    assert engine.suspend_calls == [session_ref, session_ref]


async def test_hydrated_context_survives_home_archive_round_trip(tmp_path: Path) -> None:
    import ach_runtime.execution.snapshots as snapshots
    from ach_runtime.storage.archive import extract

    service = _service(tmp_path)
    state = service._sessions.get_or_create("ticket-session-ref")
    public = service._workspace_dir / ".ach-state"
    (public / "skills").mkdir(parents=True)
    (state.home / ".ach-state").symlink_to(public, target_is_directory=True)
    skill_link = state.home / ".config" / "opencode" / "skills"
    skill_link.parent.mkdir(parents=True)
    skill_link.symlink_to(public / "skills", target_is_directory=True)
    (state.home / "native-home-file").write_bytes(b"keep")

    archive = _tar_bytes({
        "skills/example/SKILL.md": b"skill bytes",
        "prompts/system.md": b"prompt bytes",
    })
    await snapshots.import_hydration(service, "ticket-session-ref", _chunks(archive))

    home_context = state.home / ".ach-state"
    assert home_context.is_dir() and not home_context.is_symlink()
    assert (home_context / "skills/example/SKILL.md").read_bytes() == b"skill bytes"
    assert (home_context / "prompts/system.md").read_bytes() == b"prompt bytes"
    assert (state.home / "native-home-file").read_bytes() == b"keep"
    assert skill_link.is_symlink()
    assert not Path(skill_link.readlink()).is_absolute()
    assert skill_link.resolve().is_relative_to(state.home.resolve())

    home_archive = tmp_path / "hydrated-home.tar.gz"
    pack(state.home, home_archive, max_bytes=1024 * 1024)
    fresh_home = tmp_path / "fresh-home"
    extract(home_archive, fresh_home, max_expanded_bytes=8 * 1024 * 1024, max_members=100)
    assert (fresh_home / ".ach-state/skills/example/SKILL.md").read_bytes() == b"skill bytes"
    assert (fresh_home / ".ach-state/prompts/system.md").read_bytes() == b"prompt bytes"
    fresh_skill_link = fresh_home / ".config/opencode/skills"
    assert fresh_skill_link.is_symlink()
    assert fresh_skill_link.resolve().is_relative_to(fresh_home.resolve())


async def test_cold_import_restores_exact_sid_without_prompt_posts(
    tmp_path: Path, public_config_fixture, monkeypatch
) -> None:
    import ach_runtime.execution.snapshots as snapshots

    session_ref, workspace_ref, sid = "opaque-session", "pinned-workspace", "archived-native-sid"
    cold_root = tmp_path / "cold"
    workspace = cold_root / "workspace"
    workspace.mkdir(parents=True)
    manifest = SnapshotManifest(
        snapshot_version="session-v1", engine_package="@opencode/cli", engine_version="2.0.20",
        agent={"name": public_config_fixture.agent.name,
               "namespace": public_config_fixture.agent.namespace,
               "uid": public_config_fixture.agent.uid},
        workspace_ref=workspace_ref, session_ref=session_ref, native_session_id=sid,
        workspace_path=str(workspace),
    )
    archive_home = tmp_path / "archive-home"
    archive_home.mkdir()
    (archive_home / ".ach-registry.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    (archive_home / SESSION_STARTED_MARKER).touch()
    (archive_home / "user-data").write_text("restored", encoding="utf-8")
    archive_path = tmp_path / "session.tar.gz"
    pack(archive_home, archive_path, max_bytes=1024 * 1024)
    archive = archive_path.read_bytes()
    observed: list[tuple[str, str]] = []

    async def fake_start(native, *, env, config_path, deadline=None):
        assert config_path.is_file()

    async def fake_request(native, method, path, **kwargs):
        observed.append((method, path))
        return 200, {"id": sid}

    monkeypatch.setattr(_NativeSession, "ensure_started", fake_start)
    monkeypatch.setattr(_NativeSession, "request", fake_request)
    engine = OpenCodeEngine()
    service = _service(
        tmp_path, engine=engine, workspace_ref=workspace_ref, public=public_config_fixture
    )

    await asyncio.wait_for(snapshots.import_session(service, session_ref, _chunks(archive)), timeout=3)

    state = service._sessions.get(session_ref)
    assert state is not None and state.native_session_id == sid
    assert (state.home / "user-data").read_text() == "restored"
    assert observed == [("GET", f"/api/session/{sid}")]
    assert engine._sessions[session_ref].sid == sid


async def _start_and_settle(service: ExecutionService, request: StartRequest):
    started = await service.start(request)
    inv = service._invocations[request.invocation_id]
    if inv.settle_task is not None:
        await inv.settle_task
    return started, inv


async def test_retire_rejects_active_unknown_or_cleanup_owned_session(
    tmp_path: Path, public_config_fixture
) -> None:
    from datetime import datetime, timedelta, timezone

    engine = _SettlingEngine()
    service = _service(tmp_path, engine=engine, public=public_config_fixture)
    session_ref = "retire-reject-session"
    request = StartRequest(workspaceKey="wk", sessionKey="sk", 
        invocationId="inv-1", channel="webhook", workspaceRef="ws-ref",
        sessionRef=session_ref, configVersion=public_config_fixture.config_version,
        prompt="do it", deadlineAt=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    )
    _, inv = await _start_and_settle(service, request)
    assert inv.terminal is True

    engine._sessions[session_ref] = SimpleNamespace(alive=True)
    with pytest.raises(RuntimeError, match="active or unconfirmed"):
        await service.retire_session(session_ref)
    engine._sessions[session_ref] = SimpleNamespace(alive=False)

    inv.state = "unknown"
    with pytest.raises(RuntimeError, match="active or unconfirmed"):
        await service.retire_session(session_ref)
    inv.state = "completed"

    pending = asyncio.create_task(asyncio.sleep(10))
    inv.owner_stop_task = pending
    try:
        with pytest.raises(RuntimeError, match="active or unconfirmed"):
            await service.retire_session(session_ref)
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        inv.owner_stop_task = None

    assert engine.retire_calls == []
    await service.retire_session(session_ref)
    assert engine.retire_calls == [session_ref]


async def test_retire_is_idempotent_and_preserves_old_duplicate_status(
    tmp_path: Path, public_config_fixture
) -> None:
    from datetime import datetime, timedelta, timezone

    engine = _SettlingEngine()
    service = _service(tmp_path, engine=engine, public=public_config_fixture)
    session_ref = "retire-idempotent-session"
    request = StartRequest(workspaceKey="wk", sessionKey="sk", 
        invocationId="inv-1", channel="webhook", workspaceRef="ws-ref",
        sessionRef=session_ref, configVersion=public_config_fixture.config_version,
        prompt="do it", deadlineAt=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    )
    first_started, inv = await _start_and_settle(service, request)
    assert inv.result["text"] == "ok"
    old_result = inv.result

    await service.retire_session(session_ref)
    assert engine.retire_calls == [session_ref]
    assert service._sessions.get(session_ref) is None

    # Idempotent: the session is already absent — a repeated retire is a no-op, never a
    # second engine.retire_session call.
    await service.retire_session(session_ref)
    assert engine.retire_calls == [session_ref]

    # A duplicate replay of the exact same prior Start returns the retained receipt and old
    # result without invoking the engine again — retire never touched `self._invocations`.
    duplicate = await service.start(request)
    assert duplicate == first_started
    assert engine.start_calls == ["inv-1"]
    assert service._invocations["inv-1"].result == old_result


async def test_session_skipped_by_session_start_can_be_retired(
    tmp_path: Path, public_config_fixture
) -> None:
    from datetime import datetime, timedelta, timezone

    from ach_runtime.config.schema import HookBlock

    engine = _SettlingEngine()
    public = public_config_fixture.model_copy(
        update={"hooks": public_config_fixture.hooks.model_copy(
            update={"session_start": HookBlock(script="exit 78")}
        )}
    )
    service = _service(tmp_path, engine=engine, public=public)
    session_ref = "skipped-session"
    request = StartRequest(workspaceKey="wk", sessionKey="sk", 
        invocationId="inv-1", channel="tick", workspaceRef="ws-ref",
        sessionRef=session_ref, configVersion=public.config_version,
        prompt="do it", deadlineAt=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    )
    await service.start(request)
    assert (await service.status("inv-1")).state == "skipped"
    assert engine.start_calls == []

    await service.retire_session(session_ref)
    assert service._sessions.get(session_ref) is None


@pytest.mark.parametrize("tree", ["member_cap", "absolute_link"])
async def test_workspace_export_rejects_archive_import_would_refuse(
    tmp_path: Path, monkeypatch, tree: str
) -> None:
    """An export must never yield bytes the restore policy rejects: the close path retires
    local state once those bytes are durable."""
    import tempfile

    import ach_runtime.execution.snapshots as snapshots
    from ach_runtime.storage.archive import ArchiveTooLarge

    service = _service(tmp_path)
    workspace = service._workspace_dir
    if tree == "member_cap":
        # 10,000 files plus the archive root member is one past the import cap.
        for index in range(snapshots.MAX_ARCHIVE_MEMBERS):
            (workspace / f"f{index}").touch()
        expected = ArchiveTooLarge
    else:
        (workspace / "escape").symlink_to("/etc/passwd")
        expected = tarfile.TarError
    out = tmp_path / "out"
    out.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(out))

    with pytest.raises(expected):
        await snapshots.export_workspace(service)

    assert list(out.iterdir()) == []
    assert service._workspace_frozen is False


async def test_hydration_cancel_after_completed_rename_keeps_managed_context(
    tmp_path: Path, monkeypatch
) -> None:
    """Cancellation landing after a backup rename completed must not lose the prior
    directory: the batch either fully installs or fully rolls back."""
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    public = service._workspace_dir / ".ach-state"
    for kind in ("skills", "prompts", "artifacts"):
        (public / kind).mkdir(parents=True)
        (public / kind / "old").write_text("old")
    loop = asyncio.get_running_loop()
    original_rename = Path.rename
    task: asyncio.Task | None = None

    def cancel_after_backup(source: Path, destination: Path):
        result = original_rename(source, destination)
        if source == public / "skills" and destination.name == "skills.previous":
            loop.call_soon_threadsafe(task.cancel)
        return result

    monkeypatch.setattr(Path, "rename", cancel_after_backup)
    archive = _tar_bytes({f"{kind}/new": b"new" for kind in ("skills", "prompts", "artifacts")})
    task = asyncio.create_task(snapshots.import_hydration(service, "session-ref", _chunks(archive)))
    with pytest.raises(asyncio.CancelledError):
        await task

    states = {
        kind: sorted(path.name for path in (public / kind).iterdir())
        for kind in ("skills", "prompts", "artifacts")
    }
    assert states in ({k: ["old"] for k in states}, {k: ["new"] for k in states})
    assert list(public.parent.glob(".ach-hydration-*")) == []


async def test_handoff_cancel_after_backup_rename_keeps_target(tmp_path: Path, monkeypatch) -> None:
    import ach_runtime.execution.snapshots as snapshots

    service = _service(tmp_path)
    target = service._workspace_dir / "handoff"
    target.mkdir()
    (target / "sentinel").write_text("prior")
    loop = asyncio.get_running_loop()
    original_rename = Path.rename
    task: asyncio.Task | None = None

    def cancel_after_backup(source: Path, destination: Path):
        result = original_rename(source, destination)
        if source == target and ".previous-" in destination.name:
            loop.call_soon_threadsafe(task.cancel)
        return result

    monkeypatch.setattr(Path, "rename", cancel_after_backup)
    task = asyncio.create_task(
        snapshots.import_handoff(service, str(target), _chunks(_tar_bytes({"new": b"handoff"})))
    )
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(p.name for p in target.iterdir()) in (["sentinel"], ["new"])
    assert list(service._workspace_dir.glob(".handoff.previous-*")) == []
    assert list(service._workspace_dir.glob(".ach-handoff-*")) == []

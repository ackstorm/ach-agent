# SPDX-License-Identifier: MIT
"""Persistence type volume in the execution pod: the session HOMEs stay on the Workspace PVC,
a session resumes from the HOME it left, and unused HOMEs are pruned at Workspace start."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from ach_runtime.engine.opencode import (
    ENGINE_PACKAGE,
    ENGINE_VERSION,
    SNAPSHOT_VERSION,
    RestoreMismatch,
    SnapshotManifest,
)
from ach_runtime.execution.service import ExecutionService
from ach_runtime.execution.sessions import SessionRegistry
from ach_runtime.execution.snapshots import resume_session, suspend_session

REF = "se" * 16


class _Engine:
    """Records in-place restores; the resume path calls nothing else on it."""

    def __init__(self, error: Exception | None = None) -> None:
        self.restores: list[tuple[str, bool, Path]] = []
        self.error = error
        self._sessions: dict = {}

    async def restore(self, session_ref, manifest, **kwargs):
        if self.error is not None:
            raise self.error
        self.restores.append((session_ref, kwargs["in_place"], kwargs["home"]))


def _service(tmp_path: Path, public, engine: _Engine) -> ExecutionService:
    service = ExecutionService(engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
                               workspace_dir=tmp_path / "workspace", workspace_ref="ws-ref")
    service.configure(public)
    return service


def _write_manifest(service: ExecutionService, public, workspace_path: str) -> Path:
    home = service._sessions.get_or_create(REF).home
    manifest = SnapshotManifest(
        snapshot_version=SNAPSHOT_VERSION, engine_package=ENGINE_PACKAGE, engine_version=ENGINE_VERSION,
        agent={"name": public.agent.name, "namespace": public.agent.namespace, "uid": public.agent.uid},
        workspace_ref="ws-ref", session_ref=REF, native_session_id="ses_1",
        workspace_path=workspace_path, registry={"workspace_path": workspace_path, "runs": []},
    )
    (home / ".ach-registry.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    return home


async def test_resume_from_the_home_on_the_volume(tmp_path, public_config_fixture) -> None:
    engine = _Engine()
    service = _service(tmp_path, public_config_fixture, engine)
    assert await resume_session(service, REF) == "fresh"  # nothing saved yet
    home = _write_manifest(service, public_config_fixture, str(tmp_path / "workspace"))
    assert await resume_session(service, REF) == "resumed"
    assert engine.restores == [(REF, True, home)]


async def test_resume_from_another_workspace_path_starts_fresh(tmp_path, public_config_fixture) -> None:
    engine = _Engine()
    service = _service(tmp_path, public_config_fixture, engine)
    _write_manifest(service, public_config_fixture, "/workspace")
    assert await resume_session(service, REF) == "fresh"
    assert engine.restores == []


async def test_resume_of_another_identity_starts_fresh(tmp_path, public_config_fixture) -> None:
    engine = _Engine()
    service = _service(tmp_path, public_config_fixture, engine)
    home = _write_manifest(service, public_config_fixture, str(tmp_path / "workspace"))
    original = json.loads((home / ".ach-registry.json").read_text(encoding="utf-8"))
    for field, value in (("workspaceRef", "other-ws"), ("agent", {"name": "x", "namespace": "y", "uid": "z"})):
        (home / ".ach-registry.json").write_text(json.dumps({**original, field: value}), encoding="utf-8")
        assert await resume_session(service, REF) == "fresh"
    assert engine.restores == []


async def test_a_dead_session_is_suspended_once_without_a_clean_exit(tmp_path, public_config_fixture) -> None:
    class Dead(_Engine):
        def __init__(self) -> None:
            super().__init__()
            self._sessions = {REF: SimpleNamespace(alive=False, sid="ses_1")}
            self.snapshots = 0

        async def suspend(self, session_ref):
            return False  # killed: no proof of a clean exit

        async def snapshot(self, session_ref, **kwargs):
            self.snapshots += 1

    engine = Dead()
    service = _service(tmp_path, public_config_fixture, engine)
    service._sessions.get_or_create(REF)
    assert (await service.session_info(REF))["needsSuspend"] is True

    await suspend_session(service, REF)

    assert engine.snapshots == 1
    assert (await service.session_info(REF))["needsSuspend"] is False


async def test_a_session_this_pod_never_ran_needs_no_suspend(tmp_path, public_config_fixture) -> None:
    service = _service(tmp_path, public_config_fixture, _Engine())
    _write_manifest(service, public_config_fixture, str(tmp_path / "workspace"))
    assert (await service.session_info(REF))["needsSuspend"] is False


async def test_resume_with_a_corrupt_manifest_starts_fresh(tmp_path, public_config_fixture) -> None:
    engine = _Engine()
    service = _service(tmp_path, public_config_fixture, engine)
    home = _write_manifest(service, public_config_fixture, str(tmp_path / "workspace"))
    manifest = home / ".ach-registry.json"
    for content in (manifest.read_text(encoding="utf-8")[:20], "[1]", "{}"):
        manifest.write_text(content, encoding="utf-8")
        assert await resume_session(service, REF) == "fresh"
    assert engine.restores == []


async def test_resume_of_an_unknown_native_session_starts_fresh(tmp_path, public_config_fixture) -> None:
    service = _service(tmp_path, public_config_fixture, _Engine(RestoreMismatch("restored sid did not resolve")))
    _write_manifest(service, public_config_fixture, str(tmp_path / "workspace"))
    assert await resume_session(service, REF) == "fresh"


async def test_resume_leaves_a_session_held_by_work_alone(tmp_path, public_config_fixture) -> None:
    engine = _Engine()
    service = _service(tmp_path, public_config_fixture, engine)
    _write_manifest(service, public_config_fixture, str(tmp_path / "workspace"))
    async with service._sessions.lock(REF):
        assert await resume_session(service, REF) == "live"
    assert engine.restores == []


async def test_prune_deletes_unused_sessions_and_empties_the_scratch_of_the_rest(tmp_path) -> None:
    registry = SessionRegistry(tmp_path)
    old, recent, held = (registry.get_or_create(r) for r in ("old", "recent", "held"))
    for state in (old, recent, held):
        (state.home / "opencode.db").write_text("x", encoding="utf-8")
        (state.scratch / "tmp.bin").write_bytes(b"x")
    eight_days_ago = time.time() - 8 * 86400
    for state in (old, held):
        os.utime(state.home, (eight_days_ago, eight_days_ago))
    async with registry.lock("held"):
        assert await registry.prune(7) == 1
    assert not old.home.parent.exists() and registry.get("old") is None
    assert (recent.home / "opencode.db").is_file() and list(recent.scratch.iterdir()) == []
    assert (held.home / "opencode.db").is_file()  # reserved by work in this pod


async def test_prune_ages_a_suspended_session_by_its_manifest(tmp_path) -> None:
    registry = SessionRegistry(tmp_path)
    stale, fresh = registry.get_or_create("stale"), registry.get_or_create("fresh")
    now, eight_days_ago = time.time(), time.time() - 8 * 86400
    for state, manifest_time, home_time in ((stale, eight_days_ago, now), (fresh, now, eight_days_ago)):
        manifest = state.home / ".ach-registry.json"
        manifest.write_text("{}", encoding="utf-8")
        os.utime(manifest, (manifest_time, manifest_time))
        os.utime(state.home, (home_time, home_time))
    assert await registry.prune(7) == 1
    assert registry.get("stale") is None
    assert (fresh.home / ".ach-registry.json").is_file()

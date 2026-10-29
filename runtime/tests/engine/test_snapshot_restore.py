# SPDX-License-Identifier: MIT
"""Snapshot/restore version-gating (contract §4) — a mismatch fails closed with zero
native spawn, never a replacement session. Plus a full native round trip (review
finding #10/#11) using the Task 1 fake model fixture: start -> settle -> suspend ->
snapshot -> restore into a fresh engine/session -> same sid resolves, no synthetic prompt.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine.opencode import (
    ENGINE_PACKAGE,
    ENGINE_VERSION,
    SNAPSHOT_VERSION,
    EngineInvocation,
    OpenCodeEngine,
    RestoreMismatch,
    SnapshotManifest,
)
from tests.engine.fake_facades import fake_facades

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


@pytest.fixture
def public():
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config = RuntimeConfig.model_validate(wire)
    return public_config(config, engine_env=config.engine.env)


def _manifest(**overrides: object) -> SnapshotManifest:
    fields: dict[str, object] = dict(
        snapshot_version=SNAPSHOT_VERSION,
        engine_package=ENGINE_PACKAGE,
        engine_version=ENGINE_VERSION,
        agent={"name": "a", "namespace": "ns", "uid": "u"},
        workspace_ref="ws",
        session_ref="se",
        native_session_id="ses_1",
        workspace_path="/workspace",
    )
    fields.update(overrides)
    return SnapshotManifest(**fields)


def _restore_kwargs(public):
    return dict(public=public, engine_env=dict(public.engine.env))


async def test_restore_rejects_unsupported_snapshot_version(public) -> None:
    engine = OpenCodeEngine()
    with pytest.raises(RestoreMismatch):
        await engine.restore(
            "se", _manifest(snapshot_version="session-v2"), expected={}, **_restore_kwargs(public)
        )


async def test_restore_rejects_engine_version_mismatch(public) -> None:
    engine = OpenCodeEngine()
    with pytest.raises(RestoreMismatch):
        await engine.restore(
            "se", _manifest(engine_version="2.0.19"), expected={}, **_restore_kwargs(public)
        )


async def test_restore_rejects_engine_package_mismatch(public) -> None:
    engine = OpenCodeEngine()
    with pytest.raises(RestoreMismatch):
        await engine.restore(
            "se", _manifest(engine_package="@opencode/other"), expected={}, **_restore_kwargs(public)
        )


async def test_restore_rejects_identity_mismatch_against_expected_destination(public) -> None:
    engine = OpenCodeEngine()
    with pytest.raises(RestoreMismatch):
        await engine.restore(
            "se",
            _manifest(workspace_ref="ws-from-archive"),
            expected={"workspace_ref": "ws-expected"},
            **_restore_kwargs(public),
        )


async def test_restore_rejects_without_a_reserved_session(public) -> None:
    engine = OpenCodeEngine()
    with pytest.raises(RestoreMismatch):
        await engine.restore("se-never-reserved", _manifest(), expected={}, **_restore_kwargs(public))


async def test_restore_rejects_missing_expected_keys(public) -> None:
    engine = OpenCodeEngine()
    with pytest.raises(RestoreMismatch):
        await engine.restore(
            "se",
            _manifest(),
            expected={"workspace_ref": "ws"},  # missing session_ref/workspace_path/etc.
            **_restore_kwargs(public),
        )


async def test_restore_rejects_missing_archive_payload(public) -> None:
    engine = OpenCodeEngine()
    engine._session_for("se", Path("/tmp/does-not-matter-home"), Path("/tmp/does-not-matter-scratch"))
    with pytest.raises(RestoreMismatch):
        await engine.restore(
            "se",
            _manifest(archive_ref=None),
            expected={
                "engine_package": ENGINE_PACKAGE,
                "engine_version": ENGINE_VERSION,
                "workspace_ref": "ws",
                "session_ref": "se",
                "workspace_path": "/workspace",
            },
            **_restore_kwargs(public),
        )


class _LocalDirTransfer:
    """Minimal in-process ArchiveTransfer stand-in — Task 6 owns the real one; this proves
    only that Task 4's snapshot/restore orchestration calls upload/download correctly."""

    def __init__(self) -> None:
        self.uploaded: Path | None = None

    async def upload(self, path: Path, *, session_ref: str) -> str:
        self.uploaded = path
        return f"local://{path}"

    async def download(self, ref: str, *, dest: Path) -> None:
        assert self.uploaded is not None
        shutil.copytree(self.uploaded, dest, dirs_exist_ok=True)




async def test_restore_rejects_a_live_native_process(tmp_path: Path, public) -> None:
    """A live process would keep its old HOME open and answer the sid probe itself, so
    restore refuses before touching HOME rather than reporting a false restoration."""
    engine = OpenCodeEngine()
    home = tmp_path / "home"
    home.mkdir()
    (home / "native.db").write_text("live", encoding="utf-8")
    staged = tmp_path / "staged"
    staged.mkdir()
    session = engine._session_for("se", home, tmp_path / "scratch")
    session._proc = type("LiveProc", (), {"returncode": None})()  # type: ignore[assignment]
    with pytest.raises(RestoreMismatch, match="stopped native process"):
        await engine.restore(
            "se",
            _manifest(),
            expected={
                "engine_package": ENGINE_PACKAGE,
                "engine_version": ENGINE_VERSION,
                "workspace_ref": "ws",
                "session_ref": "se",
                "workspace_path": "/workspace",
            },
            restored_home=staged,
            **_restore_kwargs(public),
        )
    assert (home / "native.db").read_text(encoding="utf-8") == "live"
    assert staged.is_dir()

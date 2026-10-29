# SPDX-License-Identifier: MIT
"""Bounded HOME, Workspace and handoff archive adapters for the private mini API."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

from ach_runtime.engine.opencode import SnapshotManifest
from ach_runtime.execution.service import ExecutionService
from ach_runtime.storage.archive import _preflight, _run_owned, extract, pack, write_capped

MAX_SNAPSHOT_BYTES = 2 * 1024**3
MAX_HANDOFF_BYTES = 512 * 1024**2
MAX_ARCHIVE_MEMBERS = 10_000


async def _owned(func, /, *args, **kwargs):
    result, cancelled, error = await _run_owned(func, *args, **kwargs)
    if cancelled:
        raise asyncio.CancelledError()
    if error is not None:
        raise error
    return result


def _archive_path(parent: Path) -> Path:
    return parent / f".ach-archive-{uuid.uuid4().hex}.tar.gz"


def _pack_restorable(src: Path, dest: Path) -> None:
    """Pack ``src`` and reject the archive unless the import path would accept it: a
    persisted close snapshot authorizes retiring local state, so it must be restorable."""
    pack(src, dest, max_bytes=MAX_SNAPSHOT_BYTES)
    # ponytail: preflight only; extract's `data` filter is not dry-run here. Add a trial
    # extract if a member preflight accepts is ever seen rejected by that filter.
    _preflight(dest, max_expanded_bytes=MAX_SNAPSHOT_BYTES * 8, max_members=MAX_ARCHIVE_MEMBERS)


async def export_session(service: ExecutionService, session_ref: str) -> Path:
    async with service._sessions.lock(session_ref):
        state = service._sessions.get(session_ref)
        if state is None:
            raise KeyError(session_ref)
        await service.snapshot(session_ref)
        archive = _archive_path(state.home.parent)
        try:
            await _owned(_pack_restorable, state.home, archive)
            return archive
        except BaseException:
            await _owned(archive.unlink, missing_ok=True)
            raise


async def import_session(
    service: ExecutionService, session_ref: str, chunks: AsyncIterator[bytes],
    *, deadline_at: datetime | None = None,
) -> None:
    state = service._sessions.get_or_create(session_ref)
    async with service._sessions.lock(session_ref):
        root = Path(tempfile.mkdtemp(prefix=".ach-session-import-", dir=state.home.parent))
        archive = root / "snapshot.tar.gz"
        staged = root / "home"
        try:
            await write_capped(chunks, archive, max_bytes=MAX_SNAPSHOT_BYTES)
            await _owned(
                extract,
                archive,
                staged,
                max_expanded_bytes=MAX_SNAPSHOT_BYTES * 8,
                max_members=MAX_ARCHIVE_MEMBERS,
            )
            manifest = SnapshotManifest.from_dict(
                json.loads((staged / ".ach-registry.json").read_text(encoding="utf-8"))
            )
            if manifest.session_ref != session_ref:
                raise ValueError("snapshot sessionRef does not match the requested session")
            workspace_ref = service._workspace_ref
            if not workspace_ref:
                raise ValueError("execution service has no pinned workspaceRef")
            await service.restore(
                session_ref, workspace_ref, manifest, restored_home=staged,
                _reservation_held=True, deadline_at=deadline_at,
            )
        finally:
            await _owned(shutil.rmtree, root, ignore_errors=True)


async def export_workspace(service: ExecutionService) -> Path:
    if getattr(service, "_workspace_frozen", False):
        raise RuntimeError("Workspace archive operation is already active")
    service._workspace_frozen = True
    try:
        if any(not inv.terminal for inv in service._invocations.values()):
            raise RuntimeError("cannot export Workspace while work is active")
        if any(lock.locked() for lock in service._sessions._locks.values()):
            raise RuntimeError("cannot export Workspace while session cleanup is held")
        archive = _archive_path(Path(tempfile.gettempdir()))
        try:
            await _owned(_pack_restorable, service._workspace_dir, archive)
            return archive
        except BaseException:
            await _owned(archive.unlink, missing_ok=True)
            raise
    finally:
        service._workspace_frozen = False


async def import_workspace(service: ExecutionService, chunks: AsyncIterator[bytes]) -> None:
    if getattr(service, "_workspace_frozen", False):
        raise RuntimeError("Workspace archive operation is already active")
    if (
        service.ready
        or service._sessions.inventory()
        or any(lock.locked() for lock in service._sessions._locks.values())
        or any(not inv.terminal for inv in service._invocations.values())
    ):
        raise RuntimeError("Workspace archive import requires a cold service")
    service._workspace_frozen = True
    destination = service._workspace_dir
    destination.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=f".{destination.name}-import-", dir=destination))
    archive, staged = root / "workspace.tar.gz", root / "workspace"
    try:
        if any(not inv.terminal for inv in service._invocations.values()):
            raise RuntimeError("cannot import Workspace while work is active")
        await write_capped(chunks, archive, max_bytes=MAX_SNAPSHOT_BYTES)
        await _owned(extract, archive, staged, max_expanded_bytes=MAX_SNAPSHOT_BYTES * 8,
                     max_members=MAX_ARCHIVE_MEMBERS)
        await _owned(_replace_workspace_contents, destination, staged, root)
    finally:
        await _owned(shutil.rmtree, root, ignore_errors=True)
        service._workspace_frozen = False


def _replace_workspace_contents(destination: Path, staged: Path, transaction: Path) -> None:
    """Replace visible Workspace children while preserving the mounted root."""
    if any(child.name == transaction.name for child in staged.iterdir()):
        raise ValueError("archive child collides with the Workspace transaction")
    backup = transaction / "previous"
    backup.mkdir()
    moved: list[str] = []
    installed: list[str] = []
    try:
        for child in tuple(destination.iterdir()):
            if child == transaction:
                continue
            child.rename(backup / child.name)
            moved.append(child.name)
        for child in tuple(staged.iterdir()):
            child.rename(destination / child.name)
            installed.append(child.name)
    except BaseException:
        for name in reversed(installed):
            _remove_path(destination / name)
        for name in reversed(moved):
            saved = backup / name
            if saved.exists() or saved.is_symlink():
                saved.rename(destination / name)
        raise


async def import_handoff(
    service: ExecutionService,
    destination: str,
    chunks: AsyncIterator[bytes],
) -> None:
    workspace = service._workspace_dir.resolve()
    target = Path(destination)
    if not target.is_absolute() or not target.resolve().is_relative_to(workspace):
        raise ValueError("handoff destination must be a fixed path inside the Workspace")
    target = target.resolve()
    if target == workspace:
        raise ValueError("handoff destination cannot be the Workspace root")
    root = Path(tempfile.mkdtemp(prefix=".ach-handoff-", dir=workspace))
    archive, staged = root / "handoff.tar.gz", root / "staged"
    previous = target.with_name(f".{target.name}.previous-{uuid.uuid4().hex}")
    try:
        await write_capped(chunks, archive, max_bytes=MAX_HANDOFF_BYTES)
        await _owned(extract, archive, staged, max_expanded_bytes=MAX_HANDOFF_BYTES,
                     max_members=MAX_ARCHIVE_MEMBERS)
        await _owned(_replace_path, staged, target, previous)
    finally:
        await _owned(shutil.rmtree, root, ignore_errors=True)


def _replace_path(staged: Path, target: Path, previous: Path) -> None:
    """Swap ``staged`` onto ``target``, restoring the prior ``target`` on failure. One
    synchronous `_owned` worker, so no cancellation lands between a rename and its rollback."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        target.rename(previous)
    try:
        staged.rename(target)
    except BaseException:
        if previous.exists() or previous.is_symlink():
            previous.rename(target)
        raise
    _remove_path(previous)


def _link_managed(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() == target.resolve():
            return
        raise ValueError(f"engine context path is occupied: {link}")
    elif link.exists():
        if not link.is_dir() or any(link.iterdir()):
            raise ValueError(f"engine context path is occupied: {link}")
        link.rmdir()
    relative_target = Path(os.path.relpath(target, link.parent))
    link.symlink_to(relative_target, target_is_directory=True)


def _remove_managed_link(link: Path, target: Path) -> None:
    """Remove only the link previously installed by this adapter for `target`."""
    if link.is_symlink() and link.resolve() == target.resolve():
        link.unlink()


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    else:
        shutil.rmtree(path, ignore_errors=True)


def _install_managed_batch(staged: Path, public: Path) -> None:
    """Replace skills/prompts/artifacts as one rollback-capable HOME batch. Synchronous and
    run as one `_owned` worker, so no cancellation lands between a rename and its rollback
    bookkeeping."""
    kinds = ("skills", "prompts", "artifacts")
    public.mkdir(parents=True, exist_ok=True)
    transaction = Path(tempfile.mkdtemp(prefix=".ach-hydration-", dir=public.parent))
    staged_dirs = {kind: transaction / f"{kind}.staged" for kind in kinds}
    previous_dirs = {kind: transaction / f"{kind}.previous" for kind in kinds}
    targets = {kind: public / kind for kind in kinds}
    try:
        for kind in kinds:
            source = staged / kind
            if source.exists():
                shutil.copytree(source, staged_dirs[kind], symlinks=False)
            else:
                staged_dirs[kind].mkdir()
        changed: list[str] = []
        try:
            for kind in kinds:
                target = targets[kind]
                if target.exists() or target.is_symlink():
                    target.rename(previous_dirs[kind])
                    changed.append(kind)
                staged_dirs[kind].rename(target)
                if kind not in changed:
                    changed.append(kind)
        except BaseException:
            for kind in reversed(changed):
                target, previous = targets[kind], previous_dirs[kind]
                if target.exists() or target.is_symlink():
                    _remove_path(target)
                if previous.exists() or previous.is_symlink():
                    previous.rename(target)
            raise
    finally:
        shutil.rmtree(transaction, ignore_errors=True)


async def import_hydration(
    service: ExecutionService, session_ref: str, chunks: AsyncIterator[bytes]
) -> None:
    state = service._sessions.get_or_create(session_ref)
    lock = service._sessions.lock(session_ref)
    if lock.locked():
        raise RuntimeError("session reservation is active")
    native = getattr(service._engine, "_sessions", {}).get(session_ref)
    transfer_root = Path("/tmp/ach-agent-transfer")
    await _owned(transfer_root.mkdir, parents=True, exist_ok=True)
    batch = Path(tempfile.mkdtemp(prefix=".ach-harness-shared-files-", dir=transfer_root))
    archive = _archive_path(batch)
    staged = batch / "payload"
    async with lock:
        try:
            session_invocations = [
                inv for inv in service._invocations.values()
                if inv.request.session_ref == session_ref
            ]
            if any(
                not inv.terminal
                or inv.state == "unknown"
                or inv.reservation is not None
                or (inv.cancel_task is not None and not inv.cancel_task.done())
                or (inv.engine_cancel_task is not None and not inv.engine_cancel_task.done())
                or (inv.owner_stop_task is not None and not inv.owner_stop_task.done())
                for inv in session_invocations
            ):
                raise RuntimeError("cannot import hydration while session work or cleanup is active")
            await write_capped(chunks, archive, max_bytes=MAX_HANDOFF_BYTES)
            await _owned(extract, archive, staged, max_expanded_bytes=MAX_HANDOFF_BYTES * 8,
                         max_members=MAX_ARCHIVE_MEMBERS)
            if native is not None and getattr(native, "alive", False):
                if not await service._engine.suspend(session_ref):
                    raise RuntimeError("cannot import hydration without confirmed native suspend")
            public = service._workspace_dir / ".ach-state"
            await _owned(_install_managed_batch, staged, public)
            home_context = state.home / ".ach-state"
            await _owned(_remove_managed_link, home_context, public)
            await _owned(_install_managed_batch, staged, home_context)
            skills_link = state.home / ".config" / "opencode" / "skills"
            await _owned(_remove_managed_link, skills_link, public / "skills")
            await _owned(_link_managed, skills_link, home_context / "skills")
        finally:
            await _owned(shutil.rmtree, batch, ignore_errors=True)

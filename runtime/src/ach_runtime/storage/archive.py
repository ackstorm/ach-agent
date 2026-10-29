# SPDX-License-Identifier: MIT
"""Bounded, safe-extraction archives (dispatch §required-behavior 5).

Ported from `old-ach-agent/src/ach_agent/sandbox/archive.py` verbatim where the contract
allows it. Review round 1/2 fixes (`task-6-storage-review.md`,
`task-6-storage-fix-1-review.md`):
  - No public unsafe extraction mode: only PEP 706's `data` filter ever runs.
  - A full preflight pass validates member count, path traversal/absolute paths, link
    escape, member type and total expansion — ALL of it before any destination write, not
    interleaved with extraction and not relying on the `data` filter to catch anything late.
  - Extraction never writes into the caller's `dest` directly: it extracts into a freshly
    created, privately-owned sibling staging directory and only renames it onto `dest` after
    full success. Any failure — preflight or extraction — removes ONLY that staging
    directory; a pre-existing `dest` (or no `dest` at all) is never touched or deleted.
  - `pack` enforces its byte cap DURING creation via a counting writer, and cleans up on
    ANY failure (not only `ArchiveTooLarge`).
"""

from __future__ import annotations

import asyncio
import shutil
import tarfile
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

_MAX_MEMBER_NAME_LEN = 4096
_ALLOWED_TYPES = {
    tarfile.REGTYPE,
    tarfile.AREGTYPE,
    tarfile.DIRTYPE,
    tarfile.SYMTYPE,
    tarfile.LNKTYPE,
}


class ArchiveTooLarge(ValueError):
    """An archive (or its expansion, or its member count) exceeds the configured cap."""


class _CappedWriter:
    """File-like `write()` target that raises `ArchiveTooLarge` the instant the configured
    byte budget would be exceeded — used as `tarfile.open(..., fileobj=...)` so `pack`'s cap
    is enforced streaming, not by stat-ing the finished file."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        self._fh = path.open("wb")
        self._max_bytes = max_bytes
        self._written = 0

    def write(self, data: bytes) -> int:
        self._written += len(data)
        if self._written > self._max_bytes:
            raise ArchiveTooLarge(f"archive exceeds {self._max_bytes} bytes during creation")
        return self._fh.write(data)

    def flush(self) -> None:
        self._fh.flush()

    def tell(self) -> int:
        return self._fh.tell()

    def close(self) -> None:
        self._fh.close()


def pack(src: Path, dest: Path, *, max_bytes: int) -> int:
    """Write ``src``'s contents to ``dest`` as tar.gz, capped during creation; return size."""
    writer = _CappedWriter(dest, max_bytes)
    try:
        tar = tarfile.open(fileobj=writer, mode="w:gz")
        try:
            tar.add(src, arcname=".")
        finally:
            tar.close()
    except BaseException:
        # Any failure — the cap, a read error on `src`, a tar-format error — leaves no
        # partial archive and no open file handle (review: "pack closes/unlinks its writer
        # only for ArchiveTooLarge").
        writer.close()
        dest.unlink(missing_ok=True)
        raise
    writer.close()
    return dest.stat().st_size


async def _run_owned(
    func: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> tuple[Any, bool, BaseException | None]:
    """Run a blocking filesystem call for an owned handle in a worker thread, off the
    event loop. The syscall can't be interrupted once it starts, so a cancellation of the
    *awaiting* coroutine is absorbed (shielded) until the worker actually finishes rather
    than abandoned mid-flight -- losing track of what a worker just opened/wrote/created is
    how an owned handle or staging file leaks across cancellation. Returns
    ``(result, was_cancelled, exc)``: the worker's own exception (if any) is handed back
    instead of raised here, so a cancellation observed while draining isn't lost underneath
    it -- the caller decides whether that cancellation should still win over the worker's
    own failure.
    """
    task = asyncio.ensure_future(asyncio.to_thread(func, *args, **kwargs))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001 - the worker's own failure, returned below
            pass
    exc = task.exception()
    return (None if exc is not None else task.result()), cancelled, exc


async def write_capped(chunks: AsyncIterator[bytes], dest: Path, *, max_bytes: int) -> int:
    """Stream ``chunks`` into ``dest``; remove it and raise past ``max_bytes``.

    Open/write/close each run off-loop via `_run_owned`; a cancellation while one is in
    flight is drained to completion before it is unwound, so a cancelled caller never
    leaves an open handle or a partial file behind. A cancellation observed before a later
    step's own failure still wins: the caller sees `CancelledError`, not that failure, once
    the owned handle is closed and the partial file is unlinked.
    """
    fh, cancelled, exc = await _run_owned(dest.open, "wb")
    total = 0
    if exc is None and not cancelled:
        try:
            async for chunk in chunks:
                total += len(chunk)
                if total > max_bytes:
                    exc = ArchiveTooLarge(f"stream exceeds {max_bytes} bytes")
                    break
                _, write_cancelled, write_exc = await _run_owned(fh.write, chunk)
                cancelled = cancelled or write_cancelled
                if write_exc is not None:
                    exc = write_exc
                    break
                if cancelled:
                    break
        except BaseException as caught:  # noqa: BLE001 - re-raised below, after cleanup
            exc = caught
    if fh is not None:
        # A terminal outcome already received before close starts (a prior cancellation or
        # an ordinary open/write failure) is the first one the caller gets -- cleanup's own
        # cancellation/error must not replace it (I2-R1: a close-phase cancellation was
        # overwriting an already-recorded write `OSError` with `CancelledError`).
        had_outcome_before_close = cancelled or exc is not None
        _, close_cancelled, close_exc = await _run_owned(fh.close)
        if not had_outcome_before_close:
            cancelled = cancelled or close_cancelled
            if exc is None:
                exc = close_exc
    if cancelled or exc is not None:
        await _run_owned(dest.unlink, missing_ok=True)
        if cancelled:
            raise asyncio.CancelledError()
        raise exc
    return total


def _split(path_name: str) -> list[str]:
    return path_name.replace("\\", "/").split("/")


def _resolve_link_target(base: list[str], link: str) -> list[str] | None:
    """Normalize ``link`` against ``base`` path components (a `..` pops a segment);
    returns ``None`` the instant it would pop past the archive root -- an actual escape,
    not merely a `..` appearing somewhere in the target."""
    stack = list(base)
    for part in _split(link):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return None
            stack.pop()
        else:
            stack.append(part)
    return stack


def _normalize_parent(parts: list[str]) -> list[str]:
    """Drop a member's own redundant `.`/empty path separators before using it as the base
    a relative link target resolves against. A member's name can't itself contain `..`
    (preflight already rejects that elsewhere), so only `.`/`` need filtering here -- unlike
    a link target, which legitimately contains `..`. Left unfiltered, a no-op `.` or empty
    component is still popped by a target `..` as if it were a real directory, letting one
    extra level of escape slip past `_resolve_link_target`."""
    return [part for part in parts if part not in ("", ".")]


def _preflight(archive: Path, *, max_expanded_bytes: int, max_members: int) -> None:
    """Validate count, path traversal/absolute paths, link escape, member type and total
    expansion — entirely before any write (review: "Validate all allowed member/path/link/
    type constraints before destination writes")."""
    with tarfile.open(archive, "r:gz") as tar:
        total_size = 0
        count = 0
        for member in tar:
            count += 1
            if count > max_members:
                raise ArchiveTooLarge(f"archive has more than {max_members} members")
            name = member.name
            if len(name) > _MAX_MEMBER_NAME_LEN:
                raise tarfile.TarError(f"member name exceeds {_MAX_MEMBER_NAME_LEN} characters")
            if name.startswith("/") or name.startswith("\\"):
                raise tarfile.TarError(f"refusing absolute path in archive: {name!r}")
            if ".." in _split(name):
                raise tarfile.TarError(f"refusing path traversal in archive: {name!r}")
            if member.type not in _ALLOWED_TYPES:
                raise tarfile.TarError(
                    f"refusing unsupported member type {member.type!r} for {name!r}"
                )
            if member.issym() or member.islnk():
                link = member.linkname
                if len(link) > _MAX_MEMBER_NAME_LEN:
                    raise tarfile.TarError(f"link target exceeds {_MAX_MEMBER_NAME_LEN} characters")
                if link.startswith("/") or link.startswith("\\"):
                    raise tarfile.TarError(f"refusing absolute link target: {link!r}")
                # Hardlinks resolve against the archive root; symlinks resolve against
                # their own member's parent directory. A `..` is only an escape once it
                # would pop past that base, not merely for appearing in the target.
                base = [] if member.islnk() else _normalize_parent(_split(name)[:-1])
                if _resolve_link_target(base, link) is None:
                    raise tarfile.TarError(f"refusing escaping link target: {link!r}")
            total_size += member.size
            if total_size > max_expanded_bytes:
                raise ArchiveTooLarge("archive expands past the cap")


def extract(archive: Path, dest: Path, *, max_expanded_bytes: int, max_members: int) -> None:
    """Extract ``archive`` into ``dest`` with PEP 706's ``data`` filter.

    ``dest`` must not already exist: extraction writes into a freshly created, privately
    owned sibling staging directory and only `rename`s it onto ``dest`` after full success.
    A failure at any point — preflight or the real extraction pass — removes ONLY that
    staging directory; pre-existing state at (or the absence of) ``dest`` is never touched
    (review Critical: "failed extraction can delete valid existing state").
    """
    if dest.exists():
        raise FileExistsError(f"refusing to extract into an existing destination: {dest}")

    _preflight(archive, max_expanded_bytes=max_expanded_bytes, max_members=max_members)

    staging = dest.parent / f".{dest.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(staging, filter="data")
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

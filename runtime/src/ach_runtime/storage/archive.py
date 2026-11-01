# SPDX-License-Identifier: MIT
"""Bounded, safe-extraction archives (dispatch §required-behavior 5).

Ported from `old-ach-agent/src/ach_agent/sandbox/archive.py` verbatim where the contract
allows it.
  - No public unsafe extraction mode: only PEP 706's `data` filter ever runs.
  - `_MemberCheck` validates member count, path traversal/absolute paths, link escape,
    member type and total expansion. `extract` runs it on each member before that member
    is written, in one streaming gzip pass; `pack` runs it while packing, so an archive it
    returns is one `extract` accepts.
  - Extraction never writes into the caller's `dest` directly: it extracts into a freshly
    created, privately-owned sibling staging directory and only renames it onto `dest` after
    full success. Any failure — a rejected member or an extraction error — removes ONLY that
    staging directory; a pre-existing `dest` (or no `dest` at all) is never touched or
    deleted. A rejected member stops extraction before its own bytes are written, so
    staging never holds more than the expansion cap.
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


def pack(
    src: Path, dest: Path, *, max_bytes: int,
    member_filter: Callable[[tarfile.TarInfo], tarfile.TarInfo] | None = None,
) -> int:
    """Write ``src``'s contents to ``dest`` as tar.gz, capped during creation; return size.
    ``member_filter`` is `TarFile.add`'s ``filter``: it sees each member before it is added
    and raises to reject the archive."""
    writer = _CappedWriter(dest, max_bytes)
    try:
        # Level 1: workspaces are mostly git packs and binaries gzip cannot shrink; level 9
        # took 9.0 s against 7.6 s on a 329 MB clone, and 1.36 s against 0.45 s on its
        # 35 MB of source for 6 MiB against 7 MiB of output (prod, 1 CPU).
        tar = tarfile.open(fileobj=writer, mode="w:gz", compresslevel=1)
        try:
            tar.add(src, arcname=".", filter=member_filter)
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
    (`_MemberCheck` already rejects that), so only `.`/`` need filtering here -- unlike
    a link target, which legitimately contains `..`. Left unfiltered, a no-op `.` or empty
    component is still popped by a target `..` as if it were a real directory, letting one
    extra level of escape slip past `_resolve_link_target`."""
    return [part for part in parts if part not in ("", ".")]


class _MemberCheck:
    """Validates one archive's members in order: count, path traversal/absolute paths,
    link escape, member type and running expansion total. Returns the member unchanged, so
    it also serves as `TarFile.add`'s ``filter``."""

    def __init__(self, *, max_expanded_bytes: int, max_members: int) -> None:
        self._max_expanded_bytes = max_expanded_bytes
        self._max_members = max_members
        self._count = 0
        self._total_size = 0

    def __call__(self, member: tarfile.TarInfo) -> tarfile.TarInfo:
        self._count += 1
        if self._count > self._max_members:
            raise ArchiveTooLarge(f"archive has more than {self._max_members} members")
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
        self._total_size += member.size
        if self._total_size > self._max_expanded_bytes:
            raise ArchiveTooLarge("archive expands past the cap")
        return member


def extract(archive: Path, dest: Path, *, max_expanded_bytes: int, max_members: int) -> None:
    """Extract ``archive`` into ``dest`` with PEP 706's ``data`` filter, in one streaming
    pass that validates each member before writing it.

    ``dest`` must not already exist: extraction writes into a freshly created, privately
    owned sibling staging directory and only `rename`s it onto ``dest`` after full success.
    A failure at any point — a rejected member or the extraction itself — removes ONLY that
    staging directory; pre-existing state at (or the absence of) ``dest`` is never touched
    (review Critical: "failed extraction can delete valid existing state"). One pass instead
    of validate-then-extract: 1.7 s against 3.4 s for a 312 MB Workspace (prod, 1 CPU).
    """
    if dest.exists():
        raise FileExistsError(f"refusing to extract into an existing destination: {dest}")

    check = _MemberCheck(max_expanded_bytes=max_expanded_bytes, max_members=max_members)
    staging = dest.parent / f".{dest.name}.staging-{uuid.uuid4().hex}"
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r|gz") as tar:
            tar.extractall(staging, members=(check(member) for member in tar), filter="data")
        staging.rename(dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

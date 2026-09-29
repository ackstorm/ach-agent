# SPDX-License-Identifier: Apache-2.0
"""Session archives: tar.gz pack, capped receive, safe extract.

Extraction defaults to tarfile's ``data`` filter (PEP 706): absolute paths, ``..``, links
that escape the destination, devices and FIFOs are refused. The sandbox restores its own
HOME with ``tar`` instead (absolute symlinks such as ``.venv/bin/python`` survive; member
paths are still confined). It runs only inside the sandbox — the harness never extracts an
agent-written archive, it only moves the bytes (spec §5).
"""

from __future__ import annotations

import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal


class ArchiveTooLarge(ValueError):
    """An archive (or its expansion) exceeds the configured cap."""


def pack(src: Path, dest: Path, *, max_bytes: int) -> int:
    """Write ``src``'s contents to ``dest`` as tar.gz; return the archive size."""
    # ponytail: size is checked after writing; a streaming cap only matters if HOMEs
    # routinely approach maxArchiveBytes.
    with tarfile.open(dest, "w:gz") as tar:
        tar.add(src, arcname=".")
    size = dest.stat().st_size
    if size > max_bytes:
        dest.unlink()
        raise ArchiveTooLarge(f"archive is {size} bytes, cap is {max_bytes}")
    return size


async def write_capped(chunks: AsyncIterator[bytes], dest: Path, *, max_bytes: int) -> int:
    """Stream ``chunks`` into ``dest``; remove it and raise past ``max_bytes``."""
    total = 0
    try:
        with dest.open("wb") as fh:
            async for chunk in chunks:
                total += len(chunk)
                if total > max_bytes:
                    raise ArchiveTooLarge(f"stream exceeds {max_bytes} bytes")
                fh.write(chunk)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return total


def extract(
    archive: Path,
    dest: Path,
    *,
    max_expanded_bytes: int,
    filter: Literal["data", "tar"] = "data",  # noqa: A002
) -> None:
    """Extract ``archive`` into ``dest`` with ``filter`` and an expansion cap."""
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        # The `data` filter strips a leading "/" instead of rejecting it (safe, but
        # silent) — reject absolute paths outright so a hostile archive fails loudly.
        for member in members:
            if member.name.startswith("/"):
                raise tarfile.TarError(f"refusing absolute path in archive: {member.name!r}")
        if sum(m.size for m in members) > max_expanded_bytes:
            raise ArchiveTooLarge("archive expands past the cap")
        dest.mkdir(parents=True, exist_ok=True)
        tar.extractall(dest, filter=filter)

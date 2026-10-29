# SPDX-License-Identifier: MIT
"""Bounded, checksummed local read cache for restored snapshot bytes (contract §7, dispatch §4).

Optimization only, never a second active-writer copy: `S3Storage` is the sole writer; this
cache is consulted and populated only on the `restore()` read path. Keys are versioned
(`registry key + committed version`) so a cache entry can never serve a stale `latest` once
the registry pointer has moved — a request for the new version simply misses and re-fetches.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from pathlib import Path

_TMP_INFIX = ".tmp-"


def _cache_key(registry_key: str, version: int) -> str:
    return hashlib.sha256(f"{registry_key}@v{version}".encode()).hexdigest()


class BoundedCache:
    def __init__(self, cache_dir: Path, *, max_bytes: int) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_bytes = max_bytes
        # Storage now offloads the formerly-synchronous get/put calls onto separate worker
        # threads (dispatch Task 2), so concurrent calls against the same instance are a real
        # possibility, not just a hypothetical. Serialize complete get/put/eviction behind a
        # per-instance lock so one worker's eviction can never unlink another worker's
        # in-flight temporary file (I4: "offloading whole cache calls introduces overlapping
        # access to an unsynchronized directory").
        self._lock = threading.Lock()

    def _path(self, registry_key: str, version: int) -> Path:
        return self._dir / _cache_key(registry_key, version)

    def get(self, registry_key: str, version: int, *, expected_checksum: str) -> bytes | None:
        with self._lock:
            path = self._path(registry_key, version)
            if not path.exists():
                return None
            if path.stat().st_size > self._max_bytes:
                # An entry alone larger than the budget should never have been written; treat
                # it as corrupt rather than reading an unbounded amount off disk (review:
                # "`get` also reads a full entry without a size cap").
                path.unlink(missing_ok=True)
                return None
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != expected_checksum:
                path.unlink(missing_ok=True)  # corrupted cache entry — never serve it
                return None
            path.touch()  # LRU bump
            return data

    def put(self, registry_key: str, version: int, data: bytes) -> None:
        if len(data) > self._max_bytes:
            # A single object larger than the configured budget is skipped entirely, never
            # written and kept around indefinitely (review: "a single object larger than
            # max_bytes remains on disk indefinitely").
            return
        path = self._path(registry_key, version)
        # Write to a private temp file and publish by atomic rename — a mid-write failure
        # (or a concurrent `get` of the same key) must never observe a partial entry at the
        # real cache path (Task 2 brief: "owned-cache cleanup").
        tmp = path.with_name(f"{path.name}{_TMP_INFIX}{uuid.uuid4().hex}")
        with self._lock:
            try:
                tmp.write_bytes(data)
                tmp.replace(path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            self._evict_over_budget(keep=path)

    def _evict_over_budget(self, *, keep: Path) -> None:
        # Only published entries participate in eviction — a concurrent worker's own
        # not-yet-published `.tmp-*` file must never be picked (I4: eviction must not unlink
        # another worker's temporary file).
        entries = [p for p in self._dir.iterdir() if p.is_file() and _TMP_INFIX not in p.name]
        total = sum(p.stat().st_size for p in entries)
        for p in sorted(entries, key=lambda p: p.stat().st_mtime):
            if total <= self._max_bytes:
                break
            if p == keep:
                continue
            total -= p.stat().st_size
            p.unlink(missing_ok=True)

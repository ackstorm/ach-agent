# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import time
from pathlib import Path

from ach_agent.sandbox.store import SessionStore

D = "0" * 32


class _NotFound(Exception):
    response = {"Error": {"Code": "404"}}


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail_upload = False

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        if self.fail_upload:
            raise RuntimeError("s3 down")
        self.objects[key] = Path(filename).read_bytes()

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        if key not in self.objects:
            raise _NotFound()
        Path(filename).write_bytes(self.objects[key])


def _store(tmp_path: Path, s3: FakeS3) -> SessionStore:
    return SessionStore(
        tmp_path / "cache", bucket="b", prefix="ns/bot", cache_ttl_seconds=86400, s3=s3
    )


async def test_miss_everywhere(tmp_path: Path) -> None:
    assert await _store(tmp_path, FakeS3()).fetch(D) is None


async def test_commit_then_fetch_from_cache(tmp_path: Path) -> None:
    s3 = FakeS3()
    store = _store(tmp_path, s3)
    src = tmp_path / "x.tar.gz"
    src.write_bytes(b"data")
    await store.commit(D, src)
    await store.wait_uploads()
    assert store.object_key(D) == f"ns/bot/{D}.tar.gz"
    assert s3.objects[store.object_key(D)] == b"data"
    path = await store.fetch(D)
    assert path is not None and path.read_bytes() == b"data"


async def test_fetch_falls_back_to_s3(tmp_path: Path) -> None:
    s3 = FakeS3()
    store = _store(tmp_path, s3)
    s3.objects[store.object_key(D)] = b"remote"
    path = await store.fetch(D)
    assert path is not None and path.read_bytes() == b"remote"


async def test_sweep_retries_pending_then_evicts(tmp_path: Path) -> None:
    s3 = FakeS3()
    s3.fail_upload = True
    store = _store(tmp_path, s3)
    src = tmp_path / "x.tar.gz"
    src.write_bytes(b"data")
    await store.commit(D, src)
    await store.wait_uploads()
    old = time.time() - 2 * 86400
    os.utime(store.cache_path(D), (old, old))
    s3.fail_upload = False
    await store.sweep()
    assert s3.objects[store.object_key(D)] == b"data"
    assert not store.cache_path(D).exists()

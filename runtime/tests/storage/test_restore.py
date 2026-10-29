# SPDX-License-Identifier: MIT
"""Restore / missing-copy / corruption / cache tests (dispatch §required-behavior 4)."""

from __future__ import annotations

import hashlib
import asyncio
import json
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from ach_runtime.config.schema import AgentIdentity
from ach_runtime.storage.api import RestoreFailed
from ach_runtime.storage.cache import BoundedCache
from ach_runtime.storage import s3 as s3_module
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage
from fakes import FakeS3Client

_AGENT = AgentIdentity(name="a", namespace="agents", uid="11111111-1111-1111-1111-111111111111")
_WS = "a" * 64


async def _bytes(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _ref() -> StateRef:
    return StateRef(agent=_AGENT, workspace_ref=_WS, domain="workspace")


async def _read(receipt) -> bytes:  # noqa: ANN001 — test helper, receipt is RestoreReceipt
    chunks = [chunk async for chunk in receipt.open()]
    return b"".join(chunks)


async def test_restore_never_persisted_domain_returns_none() -> None:
    storage = S3Storage(bucket="b", prefix="pfx", client=FakeS3Client())
    assert await storage.restore(_ref()) is None


async def test_restore_registry_present_bytes_missing_raises_restore_failed() -> None:
    client = FakeS3Client()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    client.delete_object_for_test(ref.snapshot_key("pfx", "snap-1"))

    with pytest.raises(RestoreFailed):
        await storage.restore(ref)


async def test_restore_verifies_checksum_before_exposing_content() -> None:
    client = FakeS3Client()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    client.corrupt_for_test(ref.snapshot_key("pfx", "snap-1"), b"evil")  # same length as b"data"

    receipt = await storage.restore(ref)
    assert receipt is not None
    with pytest.raises(RestoreFailed):
        await _read(receipt)


async def test_restore_round_trips_committed_bytes() -> None:
    client = FakeS3Client()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    receipt = await storage.restore(ref)
    assert receipt is not None
    assert await _read(receipt) == b"data"


@pytest.mark.parametrize("domain", ["workspace", "session"])
async def test_snapshot_retention_expires_registered_state_before_cache_or_download(
    tmp_path: Path, domain: str
) -> None:
    client = FakeS3Client()
    now = [datetime(2026, 10, 2, tzinfo=timezone.utc)]
    cache = BoundedCache(tmp_path / domain, max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache,
                        clock=lambda: now[0])
    ref = _ref() if domain == "workspace" else StateRef(
        agent=_AGENT, workspace_ref=_WS, domain="session", session_ref="c" * 64
    )
    lease = await storage.acquire_writer(ref, "pod-1")
    committed = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None,
                                      retention_days=2)
    assert committed.committed_at == "2026-10-02T00:00:00Z"
    assert committed.expires_at == "2026-10-04T00:00:00Z"
    receipt = await storage.restore(ref, retention_days=2)
    assert receipt is not None
    assert await _read(receipt) == b"data"  # warm the verified cache
    now[0] = datetime(2026, 10, 4, tzinfo=timezone.utc)

    with pytest.raises(Exception) as restore_error:
        await storage.restore(ref, retention_days=2)
    assert type(restore_error.value).__name__ == "SnapshotExpired"
    with pytest.raises(Exception) as open_error:
        await _read(receipt)
    assert type(open_error.value).__name__ == "SnapshotExpired"
    assert ref.registry_key("pfx") in client.objects
    assert ref.snapshot_key("pfx", "snap-1") in client.objects


async def test_snapshot_zero_retention_has_explicit_no_expiry() -> None:
    client = FakeS3Client()
    now = datetime(2026, 10, 2, tzinfo=timezone.utc)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, clock=lambda: now)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    saved = await storage.persist(ref, "snap-zero", _bytes(b"data"), lease, None,
                                  retention_days=0)
    assert saved.committed_at == "2026-10-02T00:00:00Z"
    assert saved.expires_at is None
    restored = await storage.restore(ref, retention_days=0)
    assert restored is not None and restored.expires_at is None
    assert await _read(restored) == b"data"
    # A later stricter caller policy cannot invent an expiry for a commit captured at zero.
    stricter = await storage.restore(ref, retention_days=30)
    assert stricter is not None and stricter.expires_at is None


@pytest.mark.parametrize("bad_pair", [
    {"committed_at": "2026-10-02T00:00:00Z"},
    {"committed_at": None, "expires_at": None},
    {"committed_at": 12, "expires_at": None},
    {"committed_at": "2026-10-02T00:00:00", "expires_at": None},
    {"committed_at": "invalid", "expires_at": None},
    {"committed_at": "2026-10-04T00:00:00Z", "expires_at": "2026-10-03T00:00:00Z"},
])
def test_snapshot_retention_rejects_partial_or_invalid_metadata(bad_pair: dict[str, object]) -> None:
    base = {"snapshot_id": "snap", "checksum": "a" * 64, "size": 4, "version": 1}
    with pytest.raises(RestoreFailed):
        s3_module._validate_latest_entry({**base, **bad_pair})
    record = {"snapshot_id": "snap", "writer_epoch": 1, "generation": 2,
              "etag": "etag", "checksum": "a" * 64, "size": 4, "version": 1}
    with pytest.raises(RestoreFailed):
        s3_module._validate_commit_record({**record, **bad_pair})


async def test_legacy_snapshot_metadata_requires_zero_retention_policy() -> None:
    client = FakeS3Client()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "legacy", _bytes(b"data"), lease, None)
    record_key = ref.commit_record_key("pfx", "legacy")
    record = json.loads(client.objects[record_key].body)
    record.pop("committed_at", None)
    record.pop("expires_at", None)
    client.put_raw_for_test(record_key, json.dumps(record).encode())
    with pytest.raises(RestoreFailed):
        await storage.persist(ref, "legacy", _bytes(b"data"), lease, None, retention_days=1)
    registry_key = ref.registry_key("pfx")
    registry = json.loads(client.objects[registry_key].body)
    registry["latest"].pop("committed_at", None)
    registry["latest"].pop("expires_at", None)
    client.put_raw_for_test(registry_key, json.dumps(registry).encode())
    with pytest.raises(RestoreFailed):
        await storage.restore(ref, retention_days=1)
    receipt = await storage.restore(ref, retention_days=0)
    assert receipt is not None
    assert receipt.committed_at is None and receipt.expires_at is None
    assert await _read(receipt) == b"data"


async def test_cache_serves_without_refetching_from_backend(tmp_path: Path) -> None:
    client = FakeS3Client()
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    first = await storage.restore(ref)
    assert first is not None
    assert await _read(first) == b"data"

    # Remove the backend copy — a cache hit must still serve the already-verified bytes.
    client.delete_object_for_test(ref.snapshot_key("pfx", "snap-1"))
    second = await storage.restore(ref)
    assert second is not None
    assert await _read(second) == b"data"


async def test_cache_cannot_hide_a_changed_latest_pointer(tmp_path: Path) -> None:
    client = FakeS3Client()
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"v1-data"), lease, None)

    v1 = await storage.restore(ref)
    assert v1 is not None
    assert await _read(v1) == b"v1-data"

    await storage.persist(ref, "snap-2", _bytes(b"v2-data"), lease, v1.etag)

    v2 = await storage.restore(ref)
    assert v2 is not None
    assert await _read(v2) == b"v2-data"


async def test_cache_bounded_evicts_oldest_entries(tmp_path: Path) -> None:
    cache = BoundedCache(tmp_path / "cache", max_bytes=10)
    cache.put("k1", 1, b"0123456789")
    cache.put("k2", 1, b"abcdefghij")
    assert cache.get("k1", 1, expected_checksum=hashlib.sha256(b"0123456789").hexdigest()) is None
    assert cache.get("k2", 1, expected_checksum=hashlib.sha256(b"abcdefghij").hexdigest()) == b"abcdefghij"


def test_cache_skips_an_entry_larger_than_the_budget(tmp_path: Path) -> None:
    """Review: "a single object larger than max_bytes remains on disk indefinitely"."""
    cache = BoundedCache(tmp_path / "cache", max_bytes=5)
    oversized = b"0123456789"  # 10 bytes > 5-byte budget
    cache.put("k1", 1, oversized)

    assert list((tmp_path / "cache").iterdir()) == []
    assert cache.get("k1", 1, expected_checksum=hashlib.sha256(oversized).hexdigest()) is None


def test_cache_get_never_reads_an_oversized_entry_in_full(tmp_path: Path) -> None:
    """A stray oversized file at the exact cache path (e.g. written under a looser budget by
    a prior process) must not be read in full on `get` — it is treated as corrupt and removed."""
    cache_dir = tmp_path / "cache"
    cache = BoundedCache(cache_dir, max_bytes=5)
    oversized = b"0123456789"
    checksum = hashlib.sha256(oversized).hexdigest()
    cache._path("some-registry-key", 1).write_bytes(oversized)  # bypass `put`'s own guard

    assert cache.get("some-registry-key", 1, expected_checksum=checksum) is None
    assert not cache._path("some-registry-key", 1).exists()


def test_cache_put_failure_never_serves_partial_entry(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """cache.py: a failure mid-write must never leave a corrupt or partial entry servable
    (or even present) at the real cache path — `put` writes to a private temp file and only
    publishes it by rename after a full, successful write."""
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    real_write_bytes = Path.write_bytes

    def _failing_write_bytes(self, data):  # noqa: ANN001
        real_write_bytes(self, data[: len(data) // 2])
        raise OSError("simulated partial write")

    monkeypatch.setattr(Path, "write_bytes", _failing_write_bytes)

    with pytest.raises(OSError, match="simulated partial write"):
        cache.put("k1", 1, b"0123456789")

    assert list((tmp_path / "cache").iterdir()) == []
    assert cache.get("k1", 1, expected_checksum=hashlib.sha256(b"0123456789").hexdigest()) is None


def test_cache_put_eviction_failure_propagates_but_keeps_complete_entry(  # noqa: ANN001
    tmp_path, monkeypatch
) -> None:
    """cache.py: eviction runs only after the entry itself is already fully and atomically
    written — an eviction-phase failure must still propagate (never be swallowed), but the
    just-written complete entry must remain servable."""
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)

    def _boom(self, *, keep):  # noqa: ANN001
        raise OSError("simulated eviction failure")

    monkeypatch.setattr(BoundedCache, "_evict_over_budget", _boom)

    data = b"0123456789"
    with pytest.raises(OSError, match="simulated eviction failure"):
        cache.put("k1", 1, data)

    assert cache.get("k1", 1, expected_checksum=hashlib.sha256(data).hexdigest()) == data


def test_cache_overlapping_puts_do_not_corrupt_each_other(tmp_path) -> None:  # noqa: ANN001
    """I4: the formerly-synchronous cache calls now run on separate worker threads
    (`Storage` offloads them via `_run_owned`), so concurrent `put`s on the SAME instance are
    a real possibility. Hold one put's temp write mid-flight, start a second concurrently,
    prove the second cannot make progress (not even create its own temp file) until the
    first's complete put/evict cycle releases the per-instance lock — no early finish, no
    publication corruption. Release, let both complete: no temp leak, the surviving entry's
    checksum is valid, and total published bytes stay within budget."""
    cache = BoundedCache(tmp_path / "cache", max_bytes=15)
    hold = threading.Event()
    release = threading.Event()
    held_once = {"done": False}
    real_write_bytes = Path.write_bytes

    def _holding_write_bytes(self, data):  # noqa: ANN001
        result = real_write_bytes(self, data)
        if ".tmp-" in self.name and not held_once["done"]:
            held_once["done"] = True
            hold.set()
            assert release.wait(timeout=5), "watchdog: B's temp write was never released"
        return result

    errors: list[BaseException] = []

    def _run_put(key: str, data: bytes, started: threading.Event | None = None) -> None:
        if started is not None:
            started.set()
        try:
            cache.put(key, 1, data)
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    with mock.patch.object(Path, "write_bytes", _holding_write_bytes):
        thread_b = threading.Thread(target=_run_put, args=("k-b", b"b" * 10))
        thread_b.start()
        assert hold.wait(timeout=5), "watchdog: B's write never started"

        thread_a_started = threading.Event()
        thread_a = threading.Thread(target=_run_put, args=("k-a", b"a" * 10, thread_a_started))
        thread_a.start()
        try:
            assert thread_a_started.wait(timeout=5), "watchdog: A's thread never started"
            in_flight = [p.name for p in (tmp_path / "cache").iterdir() if ".tmp-" in p.name]
            assert len(in_flight) == 1  # only B's — A is blocked on the lock, not yet started
        finally:
            release.set()
            thread_b.join(timeout=5)
            thread_a.join(timeout=5)

    assert not thread_b.is_alive()
    assert not thread_a.is_alive()
    assert errors == []
    assert cache.get("k-a", 1, expected_checksum=hashlib.sha256(b"a" * 10).hexdigest()) == b"a" * 10
    assert cache.get("k-b", 1, expected_checksum=hashlib.sha256(b"b" * 10).hexdigest()) is None
    remaining = list((tmp_path / "cache").iterdir())
    assert [p for p in remaining if ".tmp-" in p.name] == []
    assert sum(p.stat().st_size for p in remaining) <= 15


async def test_restore_cancellation_drains_download_before_cleanup(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """I2/I3: cancelling a restore while the download worker is blocked mid-`body.read` must
    drain that worker to real completion (closing its body) before the staging file is
    cleaned up, and a cancelled/unverified download must never populate the cache."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    hold = threading.Event()
    release = threading.Event()
    bodies: list = []

    class _BlockingBody:
        def __init__(self, inner) -> None:  # noqa: ANN001
            self._inner = inner
            self._first = True
            self.closed = False

        def read(self, *args, **kwargs):  # noqa: ANN002, ANN003
            if self._first:
                self._first = False
                hold.set()
                assert release.wait(timeout=5), "watchdog: body.read was never released"
            return self._inner.read(*args, **kwargs)

        def close(self) -> None:
            if hasattr(self._inner, "close"):
                self._inner.close()
            self.closed = True

    ref = _ref()
    hold_key = ref.snapshot_key("pfx", "snap-1")

    class _BlockingDownloadClient(FakeS3Client):
        def get_object(self, *, Bucket, Key):  # noqa: ANN001
            result = dict(super().get_object(Bucket=Bucket, Key=Key))
            if Key != hold_key:
                return result
            body = _BlockingBody(result["Body"])
            bodies.append(body)
            result["Body"] = body
            return result

    client = _BlockingDownloadClient()
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache)
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    receipt = await storage.restore(ref)
    assert receipt is not None

    async def run() -> None:
        async for _ in receipt.open():
            pass

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(wait_for_hold(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert bodies and bodies[-1].closed
    assert list(tmp_path.glob("ach-storage-*")) == []
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.parametrize("held_phase", ["staging_creation", "hash", "read"])
async def test_restore_staging_phase_cancellation_drains_worker(  # noqa: ANN001
    tmp_path, monkeypatch, held_phase
) -> None:
    """I2/I3: cancelling restore while staging creation, hashing, or the final disk read is
    in flight must drain that worker to real completion before any cleanup, leaving no
    staging file and no cache entry behind.

    I5: also asserts the held phase genuinely ran off the event loop's own thread — the
    previously omitted loop-thread assertion."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    loop_thread_id = threading.get_ident()

    client = FakeS3Client()
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    hold = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    io_thread_ids: list[int] = []

    real_new_staging = s3_module._new_staging_path
    real_hash = s3_module._hash_file
    real_read_bytes = Path.read_bytes

    def _holding_new_staging_path() -> Path:
        if held_phase != "staging_creation":
            return real_new_staging()
        p = real_new_staging()
        io_thread_ids.append(threading.get_ident())
        hold.set()
        assert release.wait(timeout=5), "watchdog: staging creation was never released"
        worker_finished.set()
        return p

    def _holding_hash(path, *, block_size=1024 * 1024):  # noqa: ANN001
        if held_phase != "hash":
            return real_hash(path, block_size=block_size)
        io_thread_ids.append(threading.get_ident())
        hold.set()
        assert release.wait(timeout=5), "watchdog: hash was never released"
        result = real_hash(path, block_size=block_size)
        worker_finished.set()
        return result

    def _holding_read_bytes(self):  # noqa: ANN001
        if held_phase != "read" or not self.name.startswith("ach-storage-"):
            return real_read_bytes(self)
        io_thread_ids.append(threading.get_ident())
        hold.set()
        assert release.wait(timeout=5), "watchdog: staging read was never released"
        result = real_read_bytes(self)
        worker_finished.set()
        return result

    monkeypatch.setattr(s3_module, "_new_staging_path", _holding_new_staging_path)
    monkeypatch.setattr(s3_module, "_hash_file", _holding_hash)
    monkeypatch.setattr(Path, "read_bytes", _holding_read_bytes)

    receipt = await storage.restore(ref)
    assert receipt is not None

    async def run() -> None:
        async for _ in receipt.open():
            pass

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(wait_for_hold(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert worker_finished.is_set()
    assert io_thread_ids and io_thread_ids[0] != loop_thread_id
    assert list(tmp_path.glob("ach-storage-*")) == []
    assert list((tmp_path / "cache").iterdir()) == []


@pytest.mark.parametrize("failing_phase", ["download", "hash"])
async def test_restore_failed_download_or_hash_cleans_owned_staging(  # noqa: ANN001
    tmp_path, monkeypatch, failing_phase
) -> None:
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")

    client = FakeS3Client()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    if failing_phase == "download":
        def _boom(client_, bucket, key, dest, *, max_bytes):  # noqa: ANN001
            raise OSError("simulated download failure")

        monkeypatch.setattr(s3_module, "_download_capped", _boom)
    else:
        def _boom(path, *, block_size=1024 * 1024):  # noqa: ANN001
            raise OSError("simulated hash failure")

        monkeypatch.setattr(s3_module, "_hash_file", _boom)

    receipt = await storage.restore(ref)
    assert receipt is not None

    with pytest.raises(OSError):
        async for _ in receipt.open():
            pass

    assert list(tmp_path.glob("ach-storage-*")) == []
    assert unrelated.read_bytes() == b"keep"


@pytest.mark.parametrize("held_method", ["get", "put"])
@pytest.mark.parametrize("cancel_count", [0, 1, 2])
async def test_restore_cache_io_is_off_loop_and_drained(  # noqa: ANN001
    tmp_path, monkeypatch, held_method, cancel_count
) -> None:
    """I2/I3: `BoundedCache.get`/`put` stay synchronous (unchanged signatures) but Storage
    must offload the WHOLE call off the event loop and drain a cancelled call before
    relinquishing the cache's worker-owned state."""
    loop_thread_id = threading.get_ident()
    client = FakeS3Client()
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    hold = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    io_thread_ids: list[int] = []

    real_method = getattr(BoundedCache, held_method)

    def _holding(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        io_thread_ids.append(threading.get_ident())
        hold.set()
        assert release.wait(timeout=5), f"watchdog: cache.{held_method} was never released"
        result = real_method(self, *args, **kwargs)
        worker_finished.set()
        return result

    monkeypatch.setattr(BoundedCache, held_method, _holding)

    async def run() -> None:
        receipt = await storage.restore(ref)
        assert receipt is not None
        async for _ in receipt.open():
            pass

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(wait_for_hold(), timeout=5)
        for _ in range(cancel_count):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()  # I5: task must remain pending while the worker is held
    finally:
        release.set()
        if cancel_count == 0:
            await asyncio.wait_for(task, timeout=5)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)

    assert worker_finished.is_set()
    assert io_thread_ids and io_thread_ids[0] != loop_thread_id

    # I5: a surviving entry (completed or cancelled-but-drained) must be internally
    # consistent, and the cache directory must never exceed its configured budget.
    registry_key = ref.registry_key("pfx")
    if held_method == "put":
        cached = cache.get(registry_key, 1, expected_checksum=hashlib.sha256(b"data").hexdigest())
        assert cached == b"data"
        assert [p for p in (tmp_path / "cache").iterdir() if ".tmp-" in p.name] == []
    total = sum(p.stat().st_size for p in (tmp_path / "cache").iterdir() if p.is_file())
    assert total <= cache._max_bytes


async def test_restore_unlink_failure_after_successful_read_raises_and_skips_cache(  # noqa: ANN001
    tmp_path, monkeypatch
) -> None:
    """I3: an unlink OSError after an otherwise successful download/hash/read must raise —
    before any cache write or yield — never be silently discarded while serving the bytes
    whose own cleanup never actually completed."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    client = FakeS3Client()
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    real_unlink = Path.unlink

    def _failing_unlink(self, missing_ok=False):  # noqa: ANN001
        if self.name.startswith("ach-storage-"):
            raise OSError("simulated unlink failure")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    receipt = await storage.restore(ref)
    assert receipt is not None
    with pytest.raises(OSError, match="simulated unlink failure"):
        async for _ in receipt.open():
            pass

    assert list((tmp_path / "cache").iterdir()) == []


async def test_restore_unlink_cancellation_after_successful_read_drains_and_raises(  # noqa: ANN001
    tmp_path, monkeypatch
) -> None:
    """I2/I3/I5: the restore staging unlink itself gets a held-phase regression — off-loop,
    drained to real completion on cancellation — and a cancellation discovered there (after
    an otherwise-successful read) must raise, never silently populate the cache."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    loop_thread_id = threading.get_ident()
    client = FakeS3Client()
    cache = BoundedCache(tmp_path / "cache", max_bytes=10_000)
    storage = S3Storage(bucket="b", prefix="pfx", client=client, cache=cache)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    hold = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    io_thread_ids: list[int] = []
    real_unlink = Path.unlink

    def _holding_unlink(self, missing_ok=False):  # noqa: ANN001
        if not self.name.startswith("ach-storage-"):
            return real_unlink(self, missing_ok=missing_ok)
        io_thread_ids.append(threading.get_ident())
        hold.set()
        assert release.wait(timeout=5), "watchdog: unlink was never released"
        result = real_unlink(self, missing_ok=missing_ok)
        worker_finished.set()
        return result

    monkeypatch.setattr(Path, "unlink", _holding_unlink)

    receipt = await storage.restore(ref)
    assert receipt is not None

    async def run() -> None:
        async for _ in receipt.open():
            pass

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(wait_for_hold(), timeout=5)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert worker_finished.is_set()
    assert io_thread_ids and io_thread_ids[0] != loop_thread_id
    assert list((tmp_path / "cache").iterdir()) == []


async def test_restore_preserves_primary_failure_over_cleanup_unlink_failure(  # noqa: ANN001
    tmp_path, monkeypatch
) -> None:
    """I3: a pre-existing primary failure (checksum mismatch) must win over an unrelated
    cleanup unlink failure — cleanup still runs, but its own error never replaces the
    already-established terminal outcome."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    client = FakeS3Client()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)
    client.corrupt_for_test(ref.snapshot_key("pfx", "snap-1"), b"evil")  # same length, bad checksum

    real_unlink = Path.unlink

    def _failing_unlink(self, missing_ok=False):  # noqa: ANN001
        if self.name.startswith("ach-storage-"):
            raise OSError("simulated unlink failure")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    receipt = await storage.restore(ref)
    assert receipt is not None
    with pytest.raises(RestoreFailed):
        async for _ in receipt.open():
            pass


async def test_restore_download_body_read_failure_closes_body(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """I5: a REAL body.read failure inside the actual download loop (not a synthetic
    exception substituted around `_download_capped`'s boundary) must still close the body
    and propagate."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))

    class _FailingBody:
        def __init__(self, inner) -> None:  # noqa: ANN001
            self._inner = inner
            self.closed = False

        def read(self, *args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("simulated body read failure")

        def close(self) -> None:
            if hasattr(self._inner, "close"):
                self._inner.close()
            self.closed = True

    ref = _ref()
    hold_key = ref.snapshot_key("pfx", "snap-1")
    bodies: list = []

    class _FailingDownloadClient(FakeS3Client):
        def get_object(self, *, Bucket, Key):  # noqa: ANN001
            result = dict(super().get_object(Bucket=Bucket, Key=Key))
            if Key != hold_key:
                return result
            body = _FailingBody(result["Body"])
            bodies.append(body)
            result["Body"] = body
            return result

    client = _FailingDownloadClient()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"0123456789"), lease, None)

    receipt = await storage.restore(ref)
    assert receipt is not None
    with pytest.raises(OSError, match="simulated body read failure"):
        async for _ in receipt.open():
            pass

    assert bodies and bodies[-1].closed
    assert list(tmp_path.glob("ach-storage-*")) == []


async def test_restore_cancellation_during_get_object_drains_before_cleanup(  # noqa: ANN001
    tmp_path, monkeypatch
) -> None:
    """I5: cancelling restore while the owned download worker is blocked INSIDE
    `get_object` itself — before any body is even acquired — must still drain that worker to
    real completion before cleanup, not only the already-covered `body.read` hold."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    hold = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    bodies: list = []

    ref = _ref()
    hold_key = ref.snapshot_key("pfx", "snap-1")

    class _ClosingBody:
        def __init__(self, inner) -> None:  # noqa: ANN001
            self._inner = inner
            self.closed = False

        def read(self, *args, **kwargs):  # noqa: ANN002, ANN003
            return self._inner.read(*args, **kwargs)

        def close(self) -> None:
            if hasattr(self._inner, "close"):
                self._inner.close()
            self.closed = True
            worker_finished.set()

    class _BlockingGetClient(FakeS3Client):
        def get_object(self, *, Bucket, Key):  # noqa: ANN001
            if Key == hold_key:
                hold.set()
                assert release.wait(timeout=5), "watchdog: get_object was never released"
            result = dict(super().get_object(Bucket=Bucket, Key=Key))
            if Key == hold_key:
                body = _ClosingBody(result["Body"])
                bodies.append(body)
                result["Body"] = body
            return result

    client = _BlockingGetClient()
    storage = S3Storage(bucket="b", prefix="pfx", client=client)
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    receipt = await storage.restore(ref)
    assert receipt is not None

    async def run() -> None:
        async for _ in receipt.open():
            pass

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(wait_for_hold(), timeout=5)
        assert not task.done()  # I5: task must remain pending while get_object is held
        assert bodies == []  # still blocked inside get_object — no body acquired yet
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # a second cancellation while still held must also drain, not corrupt state
        await asyncio.sleep(0)
        assert not task.done()
        assert bodies == []
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert worker_finished.is_set()
    assert bodies and bodies[-1].closed
    assert list(tmp_path.glob("ach-storage-*")) == []


async def test_current_writer_reads_registry_writer_or_none() -> None:
    storage = S3Storage(bucket="b", prefix="pfx", client=FakeS3Client())
    ref = _ref()
    assert await storage.current_writer(ref) is None
    await storage.acquire_writer(ref, "pod-1")
    assert await storage.current_writer(ref) == "pod-1"
    await storage.acquire_writer(ref, "pod-2")
    assert await storage.current_writer(ref) == "pod-2"

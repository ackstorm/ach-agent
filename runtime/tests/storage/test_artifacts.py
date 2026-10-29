# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest

from ach_runtime.config.schema import AgentIdentity, ArtifactsBlock
from ach_runtime.storage.api import (
    ArtifactDisabled,
    ArtifactConflict,
    ArtifactNotFound,
    ArtifactTooLarge,
    RestoreFailed,
    StorageUnavailable,
    ArtifactExpired,
)
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage
from tests.storage.fakes import FakeS3Client

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _owner(workspace: str = "a" * 64, uid: str = "11111111-1111-1111-1111-111111111111") -> StateRef:
    return StateRef(
        agent=AgentIdentity(name="a", namespace="agents", uid=uid),
        workspace_ref=workspace,
        domain="workspace",
    )


def _producer(session: str = "b" * 64, invocation: str = "c" * 64):
    from ach_runtime.storage.api import ArtifactProducer

    return ArtifactProducer(session_ref=session, invocation_id=invocation)


def _policy(retention: int = 90) -> ArtifactsBlock:
    return ArtifactsBlock(enabled=True, maxArtifactBytes=4, retentionDays=retention)


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _storage(client: FakeS3Client | None = None) -> S3Storage:
    return S3Storage(bucket="bucket", prefix="ach", client=client or FakeS3Client())


@pytest.mark.asyncio
async def test_disabled_create_raises_policy_error() -> None:
    class _CountCalls(FakeS3Client):
        calls = 0

        def head_object(self, **kwargs):  # noqa: ANN003, ANN201
            self.calls += 1
            return super().head_object(**kwargs)

        def put_object(self, **kwargs):  # noqa: ANN003, ANN201
            self.calls += 1
            return super().put_object(**kwargs)

    client = _CountCalls()
    policy = ArtifactsBlock(enabled=False, maxArtifactBytes=4, retentionDays=90)
    with pytest.raises(ArtifactDisabled):
        await _storage(client).create_artifact(
            _owner(), _producer(), "disabled", _chunks(b"data"),
            mime="text/plain", name="report.txt", policy=policy,
        )
    assert client.calls == 0


class _FailHead(FakeS3Client):
    def head_object(self, **kwargs):  # noqa: ANN003, ANN201
        raise RuntimeError("injected HEAD failure")


@pytest.mark.asyncio
async def test_initial_head_failure_cleans_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    import ach_runtime.storage.s3 as storage_s3

    received: list[object] = []
    receive = storage_s3._receive_bounded

    async def capture_receive(*args, **kwargs):  # noqa: ANN002, ANN003
        result = await receive(*args, **kwargs)
        received.append(result[0])
        return result

    monkeypatch.setattr(storage_s3, "_receive_bounded", capture_receive)
    with pytest.raises(RuntimeError, match="injected HEAD failure"):
        await _storage(_FailHead()).create_artifact(
            _owner(), _producer(), "head-failure", _chunks(b"data"),
            mime="text/plain", name="report.txt", policy=_policy(),
        )
    assert len(received) == 1
    assert not received[0].exists()


class _BlockedBodyPut(FakeS3Client):
    def __init__(self) -> None:
        super().__init__()
        self.put_entered = threading.Event()
        self.release_put = threading.Event()

    def put_object(self, **kwargs):  # noqa: ANN003, ANN201
        if kwargs["Key"].endswith("/blob"):
            self.put_entered.set()
            assert self.release_put.wait(timeout=5)
        return super().put_object(**kwargs)


@pytest.mark.asyncio
async def test_cancelled_body_put_drains_before_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    import ach_runtime.storage.s3 as storage_s3

    client = _BlockedBodyPut()
    received: list[object] = []
    receive = storage_s3._receive_bounded

    async def capture_receive(*args, **kwargs):  # noqa: ANN002, ANN003
        result = await receive(*args, **kwargs)
        received.append(result[0])
        return result

    monkeypatch.setattr(storage_s3, "_receive_bounded", capture_receive)
    task = asyncio.create_task(_storage(client).create_artifact(
        _owner(), _producer(), "cancel-put", _chunks(b"data"),
        mime="text/plain", name="report.txt", policy=_policy(),
    ))
    try:
        assert await asyncio.to_thread(client.put_entered.wait, 5)
        assert len(received) == 1 and received[0].exists()
        task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
        assert received[0].exists()
    finally:
        client.release_put.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not received[0].exists()


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ach_runtime.storage.artifacts._utc_now", lambda: T0)


@pytest.mark.asyncio
async def test_body_manifest_receipt_and_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeS3Client()
    store = _storage(client)
    receipt = await store.create_artifact(
        _owner(), _producer(), "report-1", _chunks(b"data"),
        mime="text/plain", name="report.txt", policy=_policy(),
    )
    assert receipt.reference == f"artifact:{receipt.artifact_id}"
    assert len(receipt.artifact_id) == 64
    assert receipt.metadata.created_at == T0
    assert receipt.metadata.expires_at == T0 + timedelta(days=90)
    assert receipt.metadata.size == 4
    assert receipt.metadata.checksum == hashlib.sha256(b"data").hexdigest()
    assert receipt.metadata.owner == _owner()
    assert receipt.metadata.producer == _producer()
    assert receipt.metadata.mime == "text/plain"
    assert receipt.metadata.name == "report.txt"
    keys = list(client.objects)
    assert len(keys) == 2
    assert keys[0].endswith("/blob") and keys[1].endswith("/metadata.json")
    assert all(client.objects[k].metadata for k in keys)
    assert all(k.count("artifacts/") == 1 for k in keys)

    empty = await store.create_artifact(
        _owner(), _producer(), "empty", _chunks(),
        mime="application/octet-stream", name="empty", policy=_policy(),
    )
    assert empty.metadata.size == 0
    assert empty.metadata.checksum == hashlib.sha256(b"").hexdigest()


@pytest.mark.asyncio
async def test_limit_plus_one_fails_before_put() -> None:
    client = FakeS3Client()
    with pytest.raises(ArtifactTooLarge):
        await _storage(client).create_artifact(
            _owner(), _producer(), "large", _chunks(b"data", b"x"),
            mime="text/plain", name="large", policy=_policy(),
        )
    assert not client.objects


@pytest.mark.asyncio
async def test_restart_retry_preserves_receipt_and_conflicts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeS3Client()
    original = await _storage(client).create_artifact(
        _owner(), _producer(), "stable", _chunks(b"data"),
        mime="text/plain", name="report.txt", policy=_policy(),
    )
    first_keys = set(client.objects)
    later = T0 + timedelta(days=10)
    import ach_runtime.storage.artifacts as artifacts

    monkeypatch.setattr(artifacts, "_utc_now", lambda: later)
    retry = await _storage(client).create_artifact(
        _owner(), _producer(), "stable", _chunks(b"data"),
        mime="text/plain", name="report.txt", policy=_policy(120),
    )
    assert retry == original
    assert set(client.objects) == first_keys
    for payload, name, mime, producer in (
        (b"diff", "report.txt", "text/plain", _producer()),
        (b"data", "other.txt", "text/plain", _producer()),
        (b"data", "report.txt", "application/json", _producer()),
        (b"data", "report.txt", "text/plain", _producer("d" * 64)),
    ):
        with pytest.raises(ArtifactConflict):
            await _storage(client).create_artifact(
                _owner(), producer, "stable", _chunks(payload), mime=mime,
                name=name, policy=_policy(),
            )


class _FailManifest(FakeS3Client):
    def __init__(self) -> None:
        super().__init__()
        self.fail_manifest = True

    def put_object(self, **kwargs):  # noqa: ANN003, ANN201
        if kwargs["Key"].endswith("/metadata.json") and self.fail_manifest:
            self.fail_manifest = False
            raise RuntimeError("injected manifest failure")
        return super().put_object(**kwargs)


@pytest.mark.asyncio
async def test_orphan_is_invisible_and_matching_retry_repairs() -> None:
    client = _FailManifest()
    store = _storage(client)
    args = (_owner(), _producer(), "orphan", _chunks(b"data"))
    with pytest.raises(RuntimeError, match="injected"):
        await store.create_artifact(
            *args, mime="text/plain", name="report.txt", policy=_policy(),
        )
    assert len(client.objects) == 1
    with pytest.raises(ArtifactNotFound):
        await store.read_artifact(_owner(), "0" * 64)
    repaired = await _storage(client).create_artifact(
        _owner(), _producer(), "orphan", _chunks(b"data"),
        mime="text/plain", name="report.txt", policy=_policy(),
    )
    assert repaired.metadata.created_at == T0
    assert len(client.objects) == 2
    with pytest.raises(ArtifactConflict):
        await _storage(client).create_artifact(
            _owner(), _producer(), "orphan", _chunks(b"diff"),
            mime="text/plain", name="report.txt", policy=_policy(),
        )


@pytest.mark.asyncio
async def test_read_is_verified_bounded_and_expiry_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeS3Client()
    store = _storage(client)
    receipt = await store.create_artifact(
        _owner(), _producer(), "read", _chunks(b"data"),
        mime="text/plain", name="report.txt", policy=_policy(),
    )
    read = await _storage(client).read_artifact(_owner(), receipt.artifact_id)
    assert read.metadata == receipt.metadata
    assert b"".join([chunk async for chunk in read.open()]) == b"data"
    blob_key = next(k for k in client.objects if k.endswith("/blob"))
    client.corrupt_for_test(blob_key, b"evil")
    with pytest.raises(RestoreFailed):
        b"".join([chunk async for chunk in read.open()])

    monkeypatch.setattr("ach_runtime.storage.artifacts._utc_now", lambda: T0 + timedelta(days=90))
    with pytest.raises(ArtifactExpired):
        await _storage(client).read_artifact(_owner(), receipt.artifact_id)


@pytest.mark.asyncio
async def test_zero_retention_and_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeS3Client()
    receipt = await _storage(client).create_artifact(
        _owner(), _producer(), "forever", _chunks(b"data"),
        mime="text/plain", name="report.txt", policy=_policy(0),
    )
    import ach_runtime.storage.artifacts as artifacts

    monkeypatch.setattr(artifacts, "_utc_now", lambda: T0 + timedelta(days=10000))
    assert (await _storage(client).read_artifact(_owner(), receipt.artifact_id)).metadata.expires_at is None
    with pytest.raises(StorageUnavailable):
        await S3Storage(bucket=None, prefix="ach").read_artifact(_owner(), receipt.artifact_id)


class _LostAck(FakeS3Client):
    def __init__(self, lost_suffix: str) -> None:
        super().__init__()
        self.lost_suffix = lost_suffix

    def put_object(self, **kwargs):  # noqa: ANN003, ANN201
        result = super().put_object(**kwargs)
        if kwargs["Key"].endswith(self.lost_suffix):
            self.lost_suffix = "already-lost"
            raise RuntimeError("lost acknowledgement")
        return result


@pytest.mark.asyncio
async def test_lost_body_and_manifest_acknowledgements_reconcile() -> None:
    body_client = _LostAck("/blob")
    body_receipt = await _storage(body_client).create_artifact(
        _owner(), _producer(), "body-ack", _chunks(b"data"),
        mime="text/plain", name="body.txt", policy=_policy(),
    )
    assert len(body_client.objects) == 2
    assert (await _storage(body_client).read_artifact(_owner(), body_receipt.artifact_id)).metadata.created_at == T0

    manifest_client = _LostAck("/metadata.json")
    manifest_receipt = await _storage(manifest_client).create_artifact(
        _owner(), _producer(), "manifest-ack", _chunks(b"data"),
        mime="text/plain", name="manifest.txt", policy=_policy(),
    )
    assert len(manifest_client.objects) == 2
    assert (await _storage(manifest_client).read_artifact(_owner(), manifest_receipt.artifact_id)).metadata == manifest_receipt.metadata


@pytest.mark.asyncio
async def test_expiry_is_rechecked_when_opening_and_manifest_corruption_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeS3Client()
    receipt = await _storage(client).create_artifact(
        _owner(), _producer(), "deferred-expiry", _chunks(b"data"),
        mime="text/plain", name="expires.txt", policy=_policy(),
    )
    opened = await _storage(client).read_artifact(_owner(), receipt.artifact_id)
    monkeypatch.setattr(
        "ach_runtime.storage.artifacts._utc_now", lambda: T0 + timedelta(days=90)
    )
    with pytest.raises(ArtifactExpired):
        async for _ in opened.open():
            pytest.fail("expired bytes must not be yielded")

    other = FakeS3Client()
    blob_key = next(k for k in client.objects if k.endswith("/blob"))
    manifest_key = next(k for k in client.objects if k.endswith("/metadata.json"))
    other.put_raw_for_test(manifest_key, b"not json")
    other.put_raw_for_test(blob_key, b"data")
    with pytest.raises(RestoreFailed):
        await _storage(other).read_artifact(_owner(), receipt.artifact_id)


@pytest.mark.asyncio
async def test_same_key_races_and_owner_scope_isolation() -> None:
    client = FakeS3Client()
    stores = (_storage(client), _storage(client))
    results = await asyncio.gather(*(
        store.create_artifact(
            _owner(), _producer(), "race", _chunks(payload),
            mime="text/plain", name="race.txt", policy=_policy(),
        )
        for store, payload in zip(stores, (b"data", b"diff"))
    ), return_exceptions=True)
    winners = [result for result in results if not isinstance(result, BaseException)]
    failures = [result for result in results if isinstance(result, BaseException)]
    assert len(winners) == 1
    assert len(failures) == 1 and isinstance(failures[0], ArtifactConflict)
    data = b"".join([part async for part in (await stores[0].read_artifact(_owner(), winners[0].artifact_id)).open()])
    assert data in {b"data", b"diff"}

    same = await asyncio.gather(*(
        store.create_artifact(
            _owner(), _producer(), "same-race", _chunks(b"data"),
            mime="text/plain", name="same.txt", policy=_policy(),
        )
        for store in stores
    ))
    assert same[0] == same[1]

    shared_key = "scope"
    ids = set()
    for owner in (_owner(), _owner(workspace="e" * 64), _owner(uid="22222222-2222-2222-2222-222222222222")):
        value = await _storage(client).create_artifact(
            owner, _producer(), shared_key, _chunks(b"data"),
            mime="text/plain", name="scope.txt", policy=_policy(),
        )
        ids.add(value.artifact_id)
    assert len(ids) == 3

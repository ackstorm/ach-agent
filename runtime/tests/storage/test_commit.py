# SPDX-License-Identifier: MIT
"""Deterministic commit / CAS / writer-fencing tests (dispatch §required-behavior 2/3)."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

from ach_runtime.config.schema import AgentIdentity
from ach_runtime.storage.api import (
    ConcurrentModification,
    RestoreFailed,
    SnapshotConflict,
    SnapshotTransientFailure,
    StorageUnavailable,
    WriterFenced,
    WriterLease,
)
from ach_runtime.storage import s3 as s3_module
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage
from fakes import FakeS3Client

_OTHER_WS = "b" * 64

_AGENT = AgentIdentity(name="a", namespace="agents", uid="11111111-1111-1111-1111-111111111111")
_WS = "a" * 64


async def _bytes(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _storage(client: FakeS3Client) -> S3Storage:
    return S3Storage(bucket="b", prefix="pfx", client=client)


def _ref(*, workspace_ref: str = _WS) -> StateRef:
    return StateRef(agent=_AGENT, workspace_ref=workspace_ref, domain="workspace")


async def test_persist_without_configured_backend_fails_explicitly() -> None:
    storage = S3Storage(bucket=None, prefix="pfx", client=None)
    ref = _ref()
    with pytest.raises(StorageUnavailable):
        await storage.acquire_writer(ref, "pod-1")


async def test_identical_snapshot_retry_returns_same_receipt() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    first = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)
    second = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, first.etag)

    assert first == second


async def test_same_snapshot_id_different_bytes_conflicts() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    first = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    with pytest.raises(SnapshotConflict):
        await storage.persist(ref, "snap-1", _bytes(b"other"), lease, first.etag)


async def test_immutable_upload_precedes_registry_cas() -> None:
    """The snapshot blob must exist under its key before the registry pointer moves."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    receipt = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    assert ref.snapshot_key("pfx", "snap-1") in client.objects
    assert client.objects[ref.snapshot_key("pfx", "snap-1")].body == b"data"
    assert receipt.checksum


async def test_two_writer_acquisitions_compete_with_increasing_epochs() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()

    lease_a = await storage.acquire_writer(ref, "pod-a")
    lease_b = await storage.acquire_writer(ref, "pod-b")

    assert lease_b.epoch > lease_a.epoch


async def test_stale_writer_cannot_commit_after_replacement() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()

    stale_lease = await storage.acquire_writer(ref, "pod-a")
    await storage.acquire_writer(ref, "pod-b")  # replaces pod-a's epoch

    with pytest.raises(WriterFenced):
        await storage.persist(ref, "snap-1", _bytes(b"data"), stale_lease, None)


async def test_new_epoch_preserves_prior_committed_snapshot() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()

    lease_a = await storage.acquire_writer(ref, "pod-a")
    committed = await storage.persist(ref, "snap-1", _bytes(b"data"), lease_a, None)

    lease_b = await storage.acquire_writer(ref, "pod-b")
    restored = await storage.restore(ref)

    assert restored is not None
    assert restored.snapshot_id == committed.snapshot_id
    assert restored.checksum == committed.checksum
    assert lease_b.epoch > lease_a.epoch


async def test_acquire_writer_captures_post_acquire_base_etag() -> None:
    """`WriterLease.base_etag` comes from the registry write `acquire_writer` itself just
    made, never from a separate restore — so it is available even once the previously
    committed snapshot has expired (a cold `restore()`/`open()` must still reject it)."""
    client = FakeS3Client()
    now = [datetime(2026, 10, 2, tzinfo=timezone.utc)]
    storage = S3Storage(bucket="b", prefix="pfx", client=client, clock=lambda: now[0])
    ref = _ref()
    registry_key = ref.registry_key("pfx")

    empty_lease = await storage.acquire_writer(ref, "pod-1")
    assert empty_lease.base_etag is None

    committed = await storage.persist(ref, "snap-1", _bytes(b"data"), empty_lease, None,
                                      retention_days=2)

    committed_lease = await storage.acquire_writer(ref, "pod-2")
    assert committed_lease.base_etag is not None
    assert committed_lease.base_etag == client.objects[registry_key].etag
    assert committed_lease.base_etag != committed.etag

    now[0] = datetime(2026, 10, 5, tzinfo=timezone.utc)  # past the 2-day retention

    expired_lease = await storage.acquire_writer(ref, "pod-3")
    assert expired_lease.base_etag is not None

    with pytest.raises(Exception) as exc_info:
        await storage.restore(ref, retention_days=2)
    assert type(exc_info.value).__name__ == "SnapshotExpired"


async def test_lost_cas_response_resolves_by_idempotent_receipt_not_new_generation() -> None:
    """Simulate: our PUT landed server-side but the client never saw the ack, so the retry
    is issued with `expected_base=None` (the client has no real base to offer) — persist
    must recognise the already-committed identical snapshot and return it unchanged, never
    mint a fresh generation just because the caller's `expected_base` was stale/absent."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    first = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)
    retried = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    assert first == retried


async def test_snapshot_retry_and_writer_acquisition_preserve_retention() -> None:
    client = FakeS3Client()
    now = [datetime(2026, 10, 2, tzinfo=timezone.utc)]
    storage = S3Storage(bucket="b", prefix="pfx", client=client, clock=lambda: now[0])
    ref = _ref()
    lease_a = await storage.acquire_writer(ref, "pod-a")
    first = await storage.persist(ref, "snap-retained", _bytes(b"same"), lease_a, None,
                                  retention_days=2)
    assert first.committed_at == "2026-10-02T00:00:00Z"
    assert first.expires_at == "2026-10-04T00:00:00Z"

    # A retry must return the original registration even when caller policy and time change.
    now[0] = datetime(2026, 10, 3, tzinfo=timezone.utc)
    retried = await storage.persist(ref, "snap-retained", _bytes(b"same"), lease_a, None,
                                    retention_days=30)
    assert retried == first

    # Writer acquisition preserves exact historical metadata. Repairing a lost current
    # commit record is idempotent and does not publish another registry generation.
    registry_key = ref.registry_key("pfx")
    current_doc = json.loads(client.objects[registry_key].body)
    generation = current_doc["generation"]
    client.delete_object_for_test(ref.commit_record_key("pfx", "snap-retained"))
    lease_b = await storage.acquire_writer(ref, "pod-b")
    latest = json.loads(client.objects[registry_key].body)["latest"]
    assert latest["committed_at"] == first.committed_at
    assert latest["expires_at"] == first.expires_at
    repaired = await storage.persist(ref, "snap-retained", _bytes(b"same"), lease_b, None,
                                     retention_days=30)
    assert repaired.committed_at == first.committed_at
    assert repaired.expires_at == first.expires_at
    assert json.loads(client.objects[registry_key].body)["generation"] == generation + 1

    # Historical receipt recovery after a later commit keeps the first receipt unchanged.
    later = await storage.persist(ref, "snap-next", _bytes(b"next"), lease_b,
                                  repaired.etag, retention_days=0)
    historical = await storage.persist(ref, "snap-retained", _bytes(b"same"), lease_b,
                                       None, retention_days=0)
    assert historical.committed_at == first.committed_at
    assert historical.expires_at == first.expires_at
    assert historical == repaired
    assert historical.generation < later.generation


@pytest.mark.parametrize("retention_days", [-1, True])
async def test_snapshot_retention_rejects_negative_and_boolean_days(retention_days: int) -> None:
    storage = _storage(FakeS3Client())
    with pytest.raises(ValueError, match="non-negative integer"):
        await storage.restore(_ref(), retention_days=retention_days)


async def test_s3_storage_classifies_supported_transient_backend_failures(monkeypatch) -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    original_put = client.put_object

    class BackendError(Exception):
        def __init__(self, code):
            super().__init__(code)
            self.response = {"Error": {"Code": code}}

    def fail_snapshot_upload(**kwargs):
        if kwargs["Key"] == ref.snapshot_key("pfx", "transient"):
            raise BackendError("SlowDown")
        return original_put(**kwargs)

    monkeypatch.setattr(client, "put_object", fail_snapshot_upload)
    with pytest.raises(SnapshotTransientFailure) as failure:
        await storage.persist(ref, "transient", _bytes(b"data"), lease, None)
    assert isinstance(failure.value.__cause__, BackendError)
    assert ref.registry_key("pfx") in client.objects
    assert ref.snapshot_key("pfx", "transient") not in client.objects


async def test_historical_receipt_recovered_after_intervening_commit() -> None:
    """This is the exact round-2 fix: committing B, then retrying the OLDER A with
    `expected_base=None` (exactly as a client that lost A's original ack and never learned
    its real etag would) must return A's EXACT original receipt — unchanged generation,
    version and etag — and must NOT touch `latest`, which stays B. Rejecting this retry
    outright (round-1's behavior) is not the contract; recovering the historical receipt is."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    receipt_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    receipt_b = await storage.persist(ref, "snap-B", _bytes(b"b-data"), lease, receipt_a.etag)

    retried_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    assert retried_a == receipt_a

    restored = await storage.restore(ref)
    assert restored is not None
    assert restored.snapshot_id == receipt_b.snapshot_id  # latest did not regress to A


async def test_historical_snapshot_id_reused_with_different_bytes_still_conflicts() -> None:
    """An old ID reused with different content must continue to conflict, even long after
    `latest` moved past it (review: "An old ID reused with different content must continue
    to conflict")."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    receipt_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    await storage.persist(ref, "snap-B", _bytes(b"b-data"), lease, receipt_a.etag)

    with pytest.raises(SnapshotConflict):
        await storage.persist(ref, "snap-A", _bytes(b"different-bytes"), lease, None)


async def test_genuine_concurrent_writers_racing_a_new_snapshot_id() -> None:
    """Review: the earlier "immutable collision under concurrent writers" test "performs one
    pre-seeded object PUT, then one persist; it creates no concurrent writers" — not a real
    race. Here two coroutines genuinely race via `asyncio.gather` (each internally offloads
    its conditional PUT to a separate OS thread via `asyncio.to_thread`), racing to create
    the SAME brand-new `snapshot_id` with DIFFERENT bytes. Exactly one must win; the loser
    must see `SnapshotConflict`, and the winning committed bytes must be exactly what the
    backend actually stored — not merely "a persist() call returned successfully"."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    results = await asyncio.gather(
        storage.persist(ref, "snap-race", _bytes(b"writer-x-bytes"), lease, None),
        storage.persist(ref, "snap-race", _bytes(b"writer-y-bytes"), lease, None),
        return_exceptions=True,
    )

    successes = [r for r in results if not isinstance(r, BaseException)]
    failures = [r for r in results if isinstance(r, BaseException)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], SnapshotConflict)

    winner = successes[0]
    stored = client.objects[ref.snapshot_key("pfx", "snap-race")]
    assert stored.body in {b"writer-x-bytes", b"writer-y-bytes"}
    assert hashlib.sha256(stored.body).hexdigest() == winner.checksum

    restored = await storage.restore(ref)
    assert restored is not None
    assert restored.checksum == winner.checksum


async def test_stale_expected_base_for_a_new_snapshot_is_rejected() -> None:
    """A caller supplying the WRONG (not just missing) predecessor ETag for a brand-new
    snapshot must also be rejected — "enforce supplied predecessor semantics"."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    with pytest.raises(ConcurrentModification):
        await storage.persist(ref, "snap-B", _bytes(b"b-data"), lease, "completely-wrong-etag")


async def test_lease_for_a_different_ref_is_rejected() -> None:
    """Review: "It never compares `lease.ref` with the requested ref" — a lease minted for
    one domain must not be usable to write a different one, even under the same epoch."""
    client = FakeS3Client()
    storage = _storage(client)
    ref_a = _ref(workspace_ref=_WS)
    ref_b = _ref(workspace_ref=_OTHER_WS)
    lease_for_a = await storage.acquire_writer(ref_a, "pod-1")

    with pytest.raises(WriterFenced):
        await storage.persist(ref_b, "snap-1", _bytes(b"data"), lease_for_a, None)


async def test_lease_with_mismatched_instance_id_is_rejected() -> None:
    """Review: "a caller-constructed lease with a different instance ID[...] is accepted"."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    real_lease = await storage.acquire_writer(ref, "pod-1")
    forged_lease = dataclasses.replace(real_lease, writer_instance_id="pod-attacker")

    with pytest.raises(WriterFenced):
        await storage.persist(ref, "snap-1", _bytes(b"data"), forged_lease, None)


async def test_fabricated_lease_against_never_acquired_ref_is_rejected() -> None:
    """Review: "A missing registry is also accepted with a supplied lease" — a lease that
    was never produced by a real `acquire_writer` call must not grant any write."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    fabricated = WriterLease(ref=ref, writer_instance_id="pod-1", epoch=1)

    with pytest.raises(WriterFenced):
        await storage.persist(ref, "snap-1", _bytes(b"data"), fabricated, None)


async def test_corrupt_registry_latest_fails_explicitly_not_as_never_persisted() -> None:
    """Review: "A corrupt falsey `latest` such as `{}` is normalized to `None`" — it must
    raise, not read back as a never-persisted domain."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    client.put_raw_for_test(
        ref.registry_key("pfx"),
        b'{"writer_epoch": 1, "writer_instance_id": "pod-1", "generation": 1, "latest": {}}',
    )

    with pytest.raises(RestoreFailed):
        await storage.restore(ref)


async def test_corrupt_registry_missing_required_field_fails_explicitly() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    client.put_raw_for_test(ref.registry_key("pfx"), b'{"writer_epoch": 1}')

    with pytest.raises(RestoreFailed):
        await storage.restore(ref)


async def test_registry_with_latest_key_omitted_entirely_fails_explicitly() -> None:
    """Review: "An omitted latest still becomes never-persisted" — the previous missing-
    field test used a registry missing EVERY field, so it never actually reached this hole.
    Here only `latest` is omitted; everything else is valid."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    client.put_raw_for_test(
        ref.registry_key("pfx"),
        b'{"writer_epoch": 1, "writer_instance_id": "pod-1", "generation": 1}',
    )

    with pytest.raises(RestoreFailed):
        await storage.restore(ref)


async def test_registry_with_explicit_null_latest_is_legitimate_never_persisted() -> None:
    """The flip side of the above: an explicit JSON `null` for `latest` (what `acquire_writer`
    itself writes before any snapshot exists) is NOT corrupt."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    client.put_raw_for_test(
        ref.registry_key("pfx"),
        b'{"writer_epoch": 1, "writer_instance_id": "pod-1", "generation": 1, "latest": null}',
    )

    assert await storage.restore(ref) is None


async def test_corrupt_registry_non_positive_epoch_fails_explicitly() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    client.put_raw_for_test(
        ref.registry_key("pfx"),
        b'{"writer_epoch": 0, "writer_instance_id": "pod-1", "generation": 1, "latest": null}',
    )

    with pytest.raises(RestoreFailed):
        await storage.restore(ref)


async def test_oversized_snapshot_stream_rejected_and_staging_cleaned_up() -> None:
    """Review: "persist collects the entire incoming stream without a byte cap" — persist
    must bound the receive and leave no staging file behind on overflow."""
    import glob
    import tempfile

    from ach_runtime.storage.archive import ArchiveTooLarge

    client = FakeS3Client()
    storage = S3Storage(bucket="b", prefix="pfx", client=client, max_snapshot_bytes=8)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    async def _big():
        yield b"0123456789"  # 10 bytes > the 8-byte cap

    before = set(glob.glob(f"{tempfile.gettempdir()}/ach-storage-*"))
    with pytest.raises(ArchiveTooLarge):
        await storage.persist(ref, "snap-1", _big(), lease, None)
    after = set(glob.glob(f"{tempfile.gettempdir()}/ach-storage-*"))

    assert after - before == set()  # no leaked staging file
    assert ref.snapshot_key("pfx", "snap-1") not in client.objects


class _FaultyS3Client(FakeS3Client):
    """Local fault-injecting fake (Task 2 brief: "a local FakeS3Client subclass"). `arm`
    schedules a one-shot fault for `put_object` on an exact `Key`: "raise_before_apply"
    simulates a write that never reached the backend at all; "apply_then_raise" simulates
    the write genuinely landing server-side but the client never seeing a clean ack (a lost
    response) — the two ambiguous outcomes `persist`'s reconciliation must tell apart."""

    def __init__(self) -> None:
        super().__init__()
        self.put_calls: list[str] = []
        self._faults: dict[str, list[tuple[str, BaseException, object]]] = {}

    def arm(self, key: str, mode: str, exc: BaseException, *, before=None) -> None:  # noqa: ANN001
        self._faults.setdefault(key, []).append((mode, exc, before))

    def put_object(self, *, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, Metadata=None):  # noqa: ANN001
        self.put_calls.append(Key)
        faults = self._faults.get(Key)
        if faults:
            mode, exc, before = faults.pop(0)
            if before is not None:
                before()
            if mode == "raise_before_apply":
                raise exc
            if mode == "apply_then_raise":
                super().put_object(
                    Bucket=Bucket, Key=Key, Body=Body, IfMatch=IfMatch, IfNoneMatch=IfNoneMatch,
                    Metadata=Metadata,
                )
                raise exc
        return super().put_object(
            Bucket=Bucket, Key=Key, Body=Body, IfMatch=IfMatch, IfNoneMatch=IfNoneMatch,
            Metadata=Metadata,
        )


async def test_applied_registry_put_lost_response_reconciles() -> None:
    """I1: the registry PUT genuinely applies server-side, but the client's call raises
    before it sees the ack (a lost response) — persist must reconcile via one read and
    return the already-committed receipt, never attempt a second registry PUT.

    I5: also counts the reconciliation registry READS (not just the PUTs) and compares the
    result against an independently captured (freshly re-read) registered receipt, rather
    than only trusting persist's own return value."""
    client = _FaultyS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    registry_key = ref.registry_key("pfx")
    registry_puts_before = client.put_calls.count(registry_key)  # acquire_writer wrote once
    client.arm(registry_key, "apply_then_raise", OSError("lost ack"))

    real_get_object = client.get_object
    registry_get_calls: list[str] = []

    def _counting_get_object(*, Bucket, Key):  # noqa: ANN001
        if Key == registry_key:
            registry_get_calls.append(Key)
        return real_get_object(Bucket=Bucket, Key=Key)

    client.get_object = _counting_get_object  # type: ignore[method-assign]

    result = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    assert result.snapshot_id == "snap-A"
    assert client.put_calls.count(registry_key) - registry_puts_before == 1
    # 1 initial read (at the top of persist) + exactly 1 reconciliation read after the
    # ambiguous PUT — never a blind retry loop or a second unread PUT.
    assert len(registry_get_calls) == 2
    assert await storage._read_commit_record(ref, "snap-A") == result

    independently_read, independent_etag = await storage._read_registry(ref)
    assert independently_read is not None and independently_read.latest is not None
    expected = s3_module._receipt_from(ref, independently_read, independent_etag or "")
    assert expected == result


async def test_ambiguous_unapplied_put_preserves_error_and_latest() -> None:
    """I1: the registry PUT raises before it ever reaches the backend (genuinely
    unapplied) — persist must surface the original error, and must not have touched the
    registry or fabricated a commit record for the failed attempt."""
    client = _FaultyS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    registry_key = ref.registry_key("pfx")
    registry_before_attempt = client.objects[registry_key]
    client.arm(registry_key, "raise_before_apply", OSError("unapplied put"))

    with pytest.raises(OSError, match="unapplied"):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    assert client.objects[registry_key] == registry_before_attempt
    assert await storage._read_commit_record(ref, "snap-A") is None


async def test_missing_record_current_latest_retry_repairs_without_generation_bump() -> None:
    """I1: a retry of the snapshot that is ALREADY `current.latest`, whose durable commit
    record was lost, must repair the record in place — no registry write, no generation
    bump — regardless of what (possibly stale) `expected_base` the retrying caller supplies."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    record_key = ref.commit_record_key("pfx", "snap-A")
    registry_key = ref.registry_key("pfx")
    registry_before_retry = client.objects[registry_key]

    for stale_base in (None, "completely-stale-etag", receipt_a.etag):
        client.delete_object_for_test(record_key)
        retry = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, stale_base)
        assert retry == receipt_a
        assert client.objects[registry_key] == registry_before_retry
        assert await storage._read_commit_record(ref, "snap-A") == receipt_a


async def test_failed_record_put_retry_repairs_current_latest() -> None:
    """I1: the registry CAS succeeds (latest is genuinely A) but writing A's durable record
    fails outright — persist raises, yet a later retry of identical A must still repair the
    missing record from the already-current latest, without a second registry write."""
    client = _FaultyS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    registry_key = ref.registry_key("pfx")
    record_key = ref.commit_record_key("pfx", "snap-A")
    client.arm(record_key, "raise_before_apply", OSError("record put failed"))

    with pytest.raises(OSError, match="record put failed"):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    assert await storage._read_commit_record(ref, "snap-A") is None
    committed = await storage.restore(ref)
    assert committed is not None
    assert committed.snapshot_id == "snap-A"
    registry_before_retry = client.objects[registry_key]

    retry = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    assert retry.snapshot_id == "snap-A"
    assert retry.checksum == committed.checksum
    assert retry.size == committed.size
    assert retry.version == committed.version
    assert client.objects[registry_key] == registry_before_retry
    assert await storage._read_commit_record(ref, "snap-A") == retry


async def test_missing_historical_record_preserves_new_latest() -> None:
    """I1: A committed, its record lost, and `latest` has since advanced to B — a retry of
    A can no longer reconstruct an original receipt and must report the conflict, never
    silently regress `latest` back to A nor fabricate a receipt from thin air."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    client.delete_object_for_test(ref.commit_record_key("pfx", "snap-A"))
    await storage.persist(ref, "snap-B", _bytes(b"b-data"), lease, receipt_a.etag)
    registry_key = ref.registry_key("pfx")
    registry_b = client.objects[registry_key]

    with pytest.raises(ConcurrentModification):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    assert client.objects[registry_key] == registry_b
    assert await storage._read_commit_record(ref, "snap-A") is None


async def test_historical_replay_with_valid_successor_etag_cannot_resurrect_latest() -> None:
    """I1: the exact fix-round-1 regression — A committed, A's commit record removed, B
    committed. Retrying the identical A using B's CURRENT, genuinely valid ETag as
    `expected_base` must still be rejected: the snapshot object for A already exists
    (immutable, never deleted) but has no durable receipt and is not current latest, which
    is ambiguous (root ruling: "an indistinguishable orphan is also ambiguity; do not invent
    history or silently commit an existing receipt-less nonlatest object"). Must never
    regress `latest` back to A nor mint a fabricated new A receipt."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    client.delete_object_for_test(ref.commit_record_key("pfx", "snap-A"))
    receipt_b = await storage.persist(ref, "snap-B", _bytes(b"b-data"), lease, receipt_a.etag)
    registry_key = ref.registry_key("pfx")
    registry_b = client.objects[registry_key]

    with pytest.raises(ConcurrentModification):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, receipt_b.etag)

    assert client.objects[registry_key] == registry_b
    assert await storage._read_commit_record(ref, "snap-A") is None

    restored = await storage.restore(ref)
    assert restored is not None
    assert restored.snapshot_id == "snap-B"  # latest did not regress to A


async def test_ambiguous_retry_sees_concurrent_commit_reports_conflict() -> None:
    """I1: the exact "inject a concurrent commit during ambiguous-A reconciliation" case —
    A is no longer `latest` (B already is, committed normally), so the current-latest repair
    shortcut does not apply; retrying A's registry CAS raises an ambiguous (non-precondition)
    error, and by the time persist's one reconciliation read runs, a THIRD snapshot (C) has
    become latest — DIFFERENT from the `latest` (B) persist actually started with. That must
    report `ConcurrentModification`, never silently reconcile to A nor to B."""
    client = _FaultyS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    receipt_b = await storage.persist(ref, "snap-B", _bytes(b"b-data"), lease, receipt_a.etag)
    client.delete_object_for_test(ref.commit_record_key("pfx", "snap-A"))
    registry_key = ref.registry_key("pfx")

    def _commit_c_concurrently() -> None:
        doc = json.loads(client.objects[registry_key].body)
        doc["generation"] += 1
        doc["latest"] = {
            "snapshot_id": "snap-C",
            "checksum": hashlib.sha256(b"c-data").hexdigest(),
            "size": len(b"c-data"),
            "version": doc["latest"]["version"] + 1,
        }
        client.put_raw_for_test(registry_key, json.dumps(doc).encode())
        client.put_raw_for_test(ref.snapshot_key("pfx", "snap-C"), b"c-data")

    client.arm(
        registry_key, "raise_before_apply", OSError("ambiguous"), before=_commit_c_concurrently
    )

    with pytest.raises(ConcurrentModification):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, receipt_b.etag)

    assert await storage._read_commit_record(ref, "snap-A") is None


def _valid_commit_record(snapshot_id: str = "snap-A") -> dict:
    return {
        "snapshot_id": snapshot_id,
        "writer_epoch": 1,
        "generation": 2,
        "etag": "etag-a",
        "checksum": "a" * 64,
        "size": 4,
        "version": 1,
    }


_INVALID_RECORD_FIELDS = [
    ("snapshot_id", "wrong-ID"),
    ("snapshot_id", ""),
    ("snapshot_id", None),
    *[(f, v) for f in ("writer_epoch", "generation", "version") for v in (-1, 0, True, "1", None)],
    *[("size", v) for v in (-1, False, "4", None)],
    ("etag", ""),
    ("etag", None),
    ("etag", 1),
    ("checksum", "not-hex"),
    ("checksum", "a" * 63),
    ("checksum", "A" * 64),
    ("checksum", ("a" * 64) + "\n"),
    ("checksum", 12345),
    ("checksum", None),
]


@pytest.mark.parametrize("field,value", _INVALID_RECORD_FIELDS)
async def test_commit_record_rejects_invalid_receipt_fields(field: str, value: object) -> None:
    """I6: every commit-record field is validated using the same conventions as the
    registry document — positive integer epoch/generation/version (bool rejected even
    though it IS an int in Python), nonnegative integer size, nonempty string etag/ID, an
    exact lowercase 64-hex checksum (including the "64 hex chars + newline" `$`-vs-`\\Z`
    boundary), and requested-ID equality."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    record = _valid_commit_record()
    record[field] = value
    client.put_raw_for_test(ref.commit_record_key("pfx", "snap-A"), json.dumps(record).encode())

    with pytest.raises(RestoreFailed):
        await storage._read_commit_record(ref, "snap-A")


@pytest.mark.parametrize(
    "missing_field",
    ["snapshot_id", "writer_epoch", "generation", "etag", "checksum", "size", "version"],
)
async def test_commit_record_rejects_missing_field(missing_field: str) -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    record = _valid_commit_record()
    del record[missing_field]
    client.put_raw_for_test(ref.commit_record_key("pfx", "snap-A"), json.dumps(record).encode())

    with pytest.raises(RestoreFailed):
        await storage._read_commit_record(ref, "snap-A")


@pytest.mark.parametrize("raw", [b"not json", b'"a string, not an object"', b"[1,2,3]"])
async def test_commit_record_rejects_non_object_or_invalid_json(raw: bytes) -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    client.put_raw_for_test(ref.commit_record_key("pfx", "snap-A"), raw)

    with pytest.raises(RestoreFailed):
        await storage._read_commit_record(ref, "snap-A")


async def test_persist_retry_against_corrupt_commit_record_raises_restore_failed() -> None:
    """End-to-end (not just `_read_commit_record` directly): a retry of a snapshot whose
    durable record is corrupt must fail loudly through `persist` too, and must not touch the
    registry while deciding that."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    record_key = ref.commit_record_key("pfx", "snap-A")
    mutated = json.loads(client.objects[record_key].body)
    mutated["checksum"] = "not-a-valid-digest"
    client.corrupt_for_test(record_key, json.dumps(mutated).encode())
    registry_key = ref.registry_key("pfx")
    registry_before_retry = client.objects[registry_key]

    with pytest.raises(RestoreFailed):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    assert client.objects[registry_key] == registry_before_retry


async def test_historical_retry_compares_size_as_well_as_checksum() -> None:
    """I6: a historical record with a matching checksum but the WRONG size must not be
    silently accepted as a valid retry receipt — checksum equality alone is not enough."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    record_key = ref.commit_record_key("pfx", "snap-A")
    record = json.loads(client.objects[record_key].body)
    record["size"] = record["size"] + 1  # same checksum, wrong size
    client.corrupt_for_test(record_key, json.dumps(record).encode())

    with pytest.raises(SnapshotConflict):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)


@pytest.mark.parametrize("identical", [True, False])
async def test_conditional_record_collision_is_validated(identical: bool) -> None:
    """I6: a conditional commit-record PUT that collides with an existing key must verify
    the existing content matches, rather than treating any `PreconditionFailed` there as
    "already recorded, fine" — and must never overwrite the winner either way."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    record_key = ref.commit_record_key("pfx", "snap-A")
    collision_record = client.objects[record_key]

    if identical:
        assert await storage._write_commit_record(ref, receipt) is None
    else:
        other = dataclasses.replace(receipt, size=receipt.size + 1)
        with pytest.raises(RestoreFailed):
            await storage._write_commit_record(ref, other)

    assert client.objects[record_key] == collision_record  # never overwrite the winner


async def test_conditional_record_collision_with_malformed_existing_record_fails_loudly() -> None:
    """I5: the conditional-collision path must also be exercised with a MALFORMED existing
    record at collision time, not only well-typed identical/differing ones — a broken
    existing record must fail loudly, never be silently treated as "already recorded, fine"."""
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    record_key = ref.commit_record_key("pfx", "snap-A")
    client.corrupt_for_test(record_key, b"not valid json")

    with pytest.raises(RestoreFailed):
        await storage._write_commit_record(ref, receipt)


async def test_conditional_record_collision_with_vanished_existing_record_fails_loudly() -> None:
    """I5: a conditional PUT that raises PreconditionFailed, but whose existing record has
    vanished by the time of the follow-up read (a TOCTOU race), must also fail loudly rather
    than assume the missing collision was harmless."""
    client = _FaultyS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    record_key = ref.commit_record_key("pfx", "snap-A")

    class _FakePreconditionFailed(Exception):
        response = {"Error": {"Code": "PreconditionFailed"}}

    def _vanish() -> None:
        client.delete_object_for_test(record_key)

    client.arm(record_key, "raise_before_apply", _FakePreconditionFailed(), before=_vanish)

    with pytest.raises(RestoreFailed):
        await storage._write_commit_record(ref, receipt)


async def test_commit_record_valid_boundary_receipt_roundtrips() -> None:
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    receipt = await storage.persist(ref, "snap-A", _bytes(b""), lease, None)
    retry = await storage.persist(ref, "snap-A", _bytes(b""), lease, receipt.etag)

    assert retry == receipt
    assert receipt.size == 0
    assert receipt.writer_epoch == 1
    assert receipt.generation == 2
    assert receipt.version == 1
    assert receipt.etag
    assert receipt.checksum == hashlib.sha256(b"").hexdigest()


async def test_receive_staging_creation_cancellation_cleans_owned_file(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """I2/I3: a cancellation while staging creation itself is still in flight must be
    drained to that worker's real completion (the file DOES get created) before the created
    file is cleaned up — never abandoned, never raced."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    loop_thread_id = threading.get_ident()
    hold = threading.Event()
    release = threading.Event()
    created: list[Path] = []
    io_thread_ids: list[int] = []
    real_new_staging = s3_module._new_staging_path

    def _holding_new_staging_path() -> Path:
        p = real_new_staging()
        created.append(p)
        io_thread_ids.append(threading.get_ident())
        hold.set()
        assert release.wait(timeout=5), "watchdog: staging creation was never released"
        return p

    monkeypatch.setattr(s3_module, "_new_staging_path", _holding_new_staging_path)

    async def chunks():  # noqa: ANN202
        yield b"data"

    async def run() -> None:
        await s3_module._receive_bounded(chunks(), max_bytes=1000)

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(wait_for_hold(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # a second cancellation while still held must also drain, not corrupt state
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert created
    assert not created[0].exists()
    assert list(tmp_path.glob("ach-storage-*")) == []
    assert io_thread_ids and io_thread_ids[0] != loop_thread_id


async def test_receive_hash_failure_cleans_staging(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"keep")

    def _failing_hash(path, *, block_size=1024 * 1024):  # noqa: ANN001
        raise OSError("simulated hash failure")

    monkeypatch.setattr(s3_module, "_hash_file", _failing_hash)

    async def chunks():  # noqa: ANN202
        yield b"data"

    with pytest.raises(OSError, match="simulated hash failure"):
        await s3_module._receive_bounded(chunks(), max_bytes=1000)

    assert list(tmp_path.glob("ach-storage-*")) == []
    assert unrelated.read_bytes() == b"keep"


async def test_receive_hash_cancellation_drains_worker(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    loop_thread_id = threading.get_ident()
    real_hash = s3_module._hash_file
    hold = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    io_thread_ids: list[int] = []

    def _holding_hash(path, *, block_size=1024 * 1024):  # noqa: ANN001
        io_thread_ids.append(threading.get_ident())
        hold.set()
        assert release.wait(timeout=5), "watchdog: hash was never released"
        result = real_hash(path, block_size=block_size)
        worker_finished.set()
        return result

    monkeypatch.setattr(s3_module, "_hash_file", _holding_hash)

    async def chunks():  # noqa: ANN202
        yield b"data"

    async def run() -> None:
        await s3_module._receive_bounded(chunks(), max_bytes=1000)

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
    assert list(tmp_path.glob("ach-storage-*")) == []
    assert io_thread_ids and io_thread_ids[0] != loop_thread_id


async def test_persist_upload_cancellation_drains_staging_user(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """I2/I3: cancelling `persist` while the immutable-upload worker holds `dest` open must
    drain that worker to real completion (closing its handle) before `persist`'s own
    `finally` unlinks the same staging path — the exact "cleanup races a worker that reopens
    the path" hazard."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    real_open = Path.open
    opened_handles: list = []

    def _tracking_open(self, mode="r", *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        fh = real_open(self, mode, *args, **kwargs)
        if mode == "rb":
            opened_handles.append(fh)
        return fh

    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    registry_key = ref.registry_key("pfx")
    registry_before_attempt = client.objects.get(registry_key)
    snapshot_key = ref.snapshot_key("pfx", "snap-1")

    hold = threading.Event()
    release = threading.Event()
    worker_finished = threading.Event()
    real_put_object = client.put_object

    def _holding_put_object(*, Bucket, Key, Body, IfMatch=None, IfNoneMatch=None, Metadata=None):  # noqa: ANN001
        if Key == snapshot_key:
            hold.set()
            assert release.wait(timeout=5), "watchdog: upload was never released"
        result = real_put_object(
            Bucket=Bucket, Key=Key, Body=Body, IfMatch=IfMatch, IfNoneMatch=IfNoneMatch,
            Metadata=Metadata,
        )
        if Key == snapshot_key:
            worker_finished.set()
        return result

    client.put_object = _holding_put_object  # type: ignore[method-assign]

    async def chunks():  # noqa: ANN202
        yield b"data"

    async def run() -> None:
        await storage.persist(ref, "snap-1", chunks(), lease, None)

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    with mock.patch.object(Path, "open", _tracking_open):
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
    assert opened_handles and all(h.closed for h in opened_handles)
    assert list(tmp_path.glob("ach-storage-*")) == []
    assert client.objects.get(registry_key) == registry_before_attempt


async def test_persist_cleanup_unlink_is_off_loop_and_drained(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """I2: persist's own owned staging cleanup (the final `dest.unlink`) must run off the
    event loop like every other owned blocking step, and a cancellation while it is in
    flight must drain to the worker's real completion before persist unwinds."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    loop_thread_id = threading.get_ident()
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

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
        assert release.wait(timeout=5), "watchdog: cleanup unlink was never released"
        result = real_unlink(self, missing_ok=missing_ok)
        worker_finished.set()
        return result

    monkeypatch.setattr(Path, "unlink", _holding_unlink)

    async def run() -> None:
        await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    async def wait_for_hold() -> None:
        while not hold.is_set():
            await asyncio.sleep(0)

    task = asyncio.ensure_future(run())
    try:
        await asyncio.wait_for(wait_for_hold(), timeout=5)
        assert not task.done()  # I5: task must still be pending while the worker is held
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()  # a second cancellation while still held must also drain, not corrupt state
        await asyncio.sleep(0)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)

    assert worker_finished.is_set()
    assert io_thread_ids and io_thread_ids[0] != loop_thread_id
    # The commit itself had already succeeded before cleanup was cancelled — a cancellation
    # discovered only in cleanup must still win over an already-successful primary result.
    assert ref.snapshot_key("pfx", "snap-1") in client.objects


async def test_persist_cleanup_unlink_failure_after_success_raises(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """I2/I3: a successful persist whose offloaded staging cleanup then fails must raise
    that cleanup error — never silently return the receipt as though cleanup succeeded."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")

    real_unlink = Path.unlink

    def _failing_unlink(self, missing_ok=False):  # noqa: ANN001
        if self.name.startswith("ach-storage-"):
            raise OSError("simulated unlink failure")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    with pytest.raises(OSError, match="simulated unlink failure"):
        await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    # The commit itself still applied even though cleanup's own error is what's surfaced.
    assert ref.snapshot_key("pfx", "snap-1") in client.objects


async def test_persist_preserves_primary_failure_over_cleanup_unlink_failure(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """I3: a pre-existing primary failure (SnapshotConflict) must win over an unrelated
    cleanup unlink failure — cleanup still runs, but its own error never replaces the
    already-established terminal outcome."""
    monkeypatch.setattr(s3_module.tempfile, "gettempdir", lambda: str(tmp_path))
    client = FakeS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    real_unlink = Path.unlink

    def _failing_unlink(self, missing_ok=False):  # noqa: ANN001
        if self.name.startswith("ach-storage-"):
            raise OSError("simulated unlink failure")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _failing_unlink)

    with pytest.raises(SnapshotConflict):
        await storage.persist(ref, "snap-1", _bytes(b"different-bytes"), lease, None)


@pytest.mark.parametrize("mode", ["raise_before_apply", "upload_ack_lost"])
async def test_same_id_retry_after_failed_registry_put_commits_own_orphan(mode: str) -> None:
    """A transient failure between the immutable upload and the registry CAS leaves a
    receipt-less blob. The lifecycle retries with the same snapshot ID and bytes, so that
    retry must commit the blob it provably uploaded itself (same epoch/pod, registry
    generation unchanged), not report an unrecoverable ambiguous collision."""
    client = _FaultyS3Client()
    storage = _storage(client)
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    if mode == "raise_before_apply":
        client.arm(ref.registry_key("pfx"), mode, TimeoutError("registry put timed out"))
    else:
        client.arm(ref.snapshot_key("pfx", "snap-A"), "apply_then_raise", TimeoutError("lost ack"))

    with pytest.raises(SnapshotTransientFailure):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    assert await storage.restore(ref) is None

    receipt = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)

    assert receipt.snapshot_id == "snap-A"
    restored = await storage.restore(ref)
    assert restored is not None and restored.snapshot_id == "snap-A"
    assert await storage._read_commit_record(ref, "snap-A") == receipt


async def test_orphan_from_replaced_writer_epoch_stays_ambiguous() -> None:
    """An orphan uploaded under an older writer epoch is not this lease's own attempt."""
    client = _FaultyS3Client()
    storage = _storage(client)
    ref = _ref()
    old = await storage.acquire_writer(ref, "pod-1")
    client.arm(ref.registry_key("pfx"), "raise_before_apply", TimeoutError("registry put timed out"))
    with pytest.raises(SnapshotTransientFailure):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), old, None)
    new = await storage.acquire_writer(ref, "pod-2")

    with pytest.raises(ConcurrentModification):
        await storage.persist(ref, "snap-A", _bytes(b"a-data"), new, None)
    assert await storage.restore(ref) is None

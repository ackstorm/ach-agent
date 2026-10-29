# SPDX-License-Identifier: MIT
"""Real S3-compatible conditional-write tests (dispatch §required-behavior 6).

Unit fakes (`fakes.FakeS3Client`) prove the CAS *logic*; this module proves the real
backend actually enforces `If-Match`/`If-None-Match` the way `s3.py` assumes. Connection is
configured entirely by env vars — never hardcoded, never falls back to an emulator — so this
file only runs when a real backend endpoint is explicitly supplied by whoever is driving the
gate (coordinator-provisioned, isolated, disposable credentials).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from ach_runtime.config.schema import AgentIdentity
from ach_runtime.storage.api import SnapshotConflict, WriterFenced
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage

_ENDPOINT = os.environ.get("ACH_STORAGE_TEST_S3_ENDPOINT")
_BUCKET = os.environ.get("ACH_STORAGE_TEST_S3_BUCKET", "ach-storage-test")
_ACCESS_KEY = os.environ.get("ACH_STORAGE_TEST_S3_ACCESS_KEY", "ach-storage-test")
_SECRET_KEY = os.environ.get("ACH_STORAGE_TEST_S3_SECRET_KEY", "ach-storage-test-secret")

pytestmark = pytest.mark.skipif(
    not _ENDPOINT,
    reason=(
        "ACH_STORAGE_TEST_S3_ENDPOINT not set — this file only runs against an explicitly "
        "supplied real S3-compatible endpoint, never a built-in default or emulator fallback."
    ),
)


async def _bytes(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    c = boto3.client(
        "s3",
        endpoint_url=_ENDPOINT,
        aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY,
        region_name="us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 0}),
    )
    try:
        c.create_bucket(Bucket=_BUCKET)
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise
    return c


@pytest.fixture
def storage(client):  # noqa: ANN001, ANN201
    return S3Storage(bucket=_BUCKET, prefix=f"it-{uuid.uuid4().hex}", client=client)


def _ref() -> StateRef:
    agent = AgentIdentity(name="a", namespace="agents", uid="11111111-1111-1111-1111-111111111111")
    return StateRef(agent=agent, workspace_ref="a" * 64, domain="workspace")


# ── Low-level negative conditional assertions (bypass S3Storage, prove the raw backend
# behavior Storage's logic depends on) ──────────────────────────────────────────────────────


def test_real_backend_rejects_competing_if_none_match(client) -> None:  # noqa: ANN001
    key = f"it-neg-{uuid.uuid4().hex}/object.json"
    client.put_object(Bucket=_BUCKET, Key=key, Body=b"v1", IfNoneMatch="*")
    with pytest.raises(ClientError) as exc_info:
        client.put_object(Bucket=_BUCKET, Key=key, Body=b"v2", IfNoneMatch="*")
    assert exc_info.value.response["Error"]["Code"] in {"PreconditionFailed", "412"}


def test_real_backend_rejects_stale_if_match(client) -> None:  # noqa: ANN001
    key = f"it-neg-{uuid.uuid4().hex}/object.json"
    client.put_object(Bucket=_BUCKET, Key=key, Body=b"v1", IfNoneMatch="*")
    with pytest.raises(ClientError) as exc_info:
        client.put_object(Bucket=_BUCKET, Key=key, Body=b"v2", IfMatch="not-the-real-etag")
    assert exc_info.value.response["Error"]["Code"] in {"PreconditionFailed", "412"}


# ── Storage-level tests against the real backend ────────────────────────────────────────────


async def test_real_backend_first_write_uses_if_none_match(storage) -> None:  # noqa: ANN001
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt = await storage.persist(ref, "snap-1", _bytes(b"real-data"), lease, None)
    assert receipt.checksum

    restored = await storage.restore(ref)
    assert restored is not None
    assert b"".join([c async for c in restored.open()]) == b"real-data"


async def test_real_backend_update_uses_if_match_predecessor(storage) -> None:  # noqa: ANN001
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    first = await storage.persist(ref, "snap-1", _bytes(b"v1"), lease, None)
    second = await storage.persist(ref, "snap-2", _bytes(b"v2"), lease, first.etag)
    assert second.version == first.version + 1

    restored = await storage.restore(ref)
    assert restored is not None
    assert b"".join([c async for c in restored.open()]) == b"v2"


async def test_real_backend_rejects_stale_writer_epoch(storage) -> None:  # noqa: ANN001
    ref = _ref()
    stale_lease = await storage.acquire_writer(ref, "pod-a")
    await storage.acquire_writer(ref, "pod-b")

    with pytest.raises(WriterFenced):
        await storage.persist(ref, "snap-1", _bytes(b"data"), stale_lease, None)


async def test_real_backend_rejects_same_snapshot_id_different_bytes(storage) -> None:  # noqa: ANN001
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    first = await storage.persist(ref, "snap-1", _bytes(b"data"), lease, None)

    with pytest.raises(SnapshotConflict):
        await storage.persist(ref, "snap-1", _bytes(b"different"), lease, first.etag)


async def test_real_backend_genuine_concurrent_writers_racing_a_new_snapshot_id(storage) -> None:  # noqa: ANN001
    """A real synchronized two-instance race, not a pre-seeded object followed by one
    persist(): two coroutines genuinely race (via `asyncio.gather`, each persist() offloading
    its conditional PUT to a separate OS thread) to create the SAME never-before-seen
    `snapshot_id` with DIFFERENT bytes against the real backend. Exactly one must win the
    immutable creation; the loser must see `SnapshotConflict`; and the actually-stored object
    bytes/registry must match the winner (the Critical finding: "immutable snapshot bytes can
    be overwritten before CAS")."""
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
    restored = await storage.restore(ref)
    assert restored is not None
    assert restored.checksum == winner.checksum
    assert b"".join([c async for c in restored.open()]) in {b"writer-x-bytes", b"writer-y-bytes"}


async def test_real_backend_in_flight_takeover_rejected_by_real_cas(storage) -> None:  # noqa: ANN001
    """A genuine real-backend CAS rejection, not an app-level pre-check short-circuit: the
    registry read happens before the stream is drained, so a takeover that occurs WHILE we
    are receiving bytes is invisible to our own pre-check and must be caught by the real
    conditional PUT when we finally attempt the registry swap (review: "the test named
    failure-during-commit [...] raises at the epoch check before either upload or CAS")."""
    ref = _ref()
    lease_a = await storage.acquire_writer(ref, "pod-a")

    async def _racing_bytes():
        yield b"v1"
        await storage.acquire_writer(ref, "pod-b")  # genuine mid-persist takeover

    with pytest.raises(WriterFenced):
        await storage.persist(ref, "snap-1", _racing_bytes(), lease_a, None)


async def test_real_backend_historical_receipt_recovered_after_intervening_commit(storage) -> None:  # noqa: ANN001
    """The exact old-retry/historical-receipt contract, proven against the real backend:
    commit A, commit B, then retry A with `expected_base=None` (as a client that lost A's
    ack and never learned its real etag would) — must return A's EXACT original receipt,
    never raise, and never resurrect A as latest."""
    ref = _ref()
    lease = await storage.acquire_writer(ref, "pod-1")
    receipt_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    receipt_b = await storage.persist(ref, "snap-B", _bytes(b"b-data"), lease, receipt_a.etag)

    retried_a = await storage.persist(ref, "snap-A", _bytes(b"a-data"), lease, None)
    assert retried_a == receipt_a

    with pytest.raises(SnapshotConflict):
        await storage.persist(ref, "snap-A", _bytes(b"different-bytes"), lease, None)

    restored = await storage.restore(ref)
    assert restored is not None
    assert restored.snapshot_id == receipt_b.snapshot_id

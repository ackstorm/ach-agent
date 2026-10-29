# SPDX-License-Identifier: MIT
"""S3-compatible `Storage`: conditional CAS registry + immutable snapshot blobs
(contract §7/§11, dispatch §behavior 2/3/4).

Reuses the old `SessionStore` lazy-client/not-found-classification shape
(`old-ach-agent/src/ach_agent/sandbox/store.py`), replaced where the contract requires it:
no local-cache-is-durability, no background upload, no SandboxClaim identity — persist
durability is the S3 round trip itself, gated by writer-epoch fencing and conditional PUT.

Review round 1/2 fixes (`task-6-storage-review.md`, `task-6-storage-fix-1-review.md`):
immutable per-snapshot creation, strict predecessor-ETag enforcement, lease ref+pod binding,
a durable per-snapshot commit record for exact historical receipt recovery after `latest`
moves on, strict registry-document validation (including the explicit-null-vs-omitted
`latest` distinction), and offloaded/bounded receive+restore I/O with deterministic cleanup
regardless of cancellation. Each owned blocking step (staging creation, hash, download,
read, upload, cache I/O) runs its own `asyncio.to_thread` worker; a cancellation is shielded
and drained to that worker's real completion (`archive._run_owned`) before any cleanup
decision is made, so a cancelled await never abandons an open body/file mid-use.

Task 2 (current-latest/receipt-repair review): an applied-but-unacknowledged registry write
is reconciled with one post-failure read rather than blindly retried or rejected; a missing
commit record for the already-current `latest` is repaired without a registry write; commit
records and their conditional-write collisions are fully validated, never assumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ach_runtime.config.schema import ArtifactsBlock
from ach_runtime.storage.api import (
    ArtifactProducer,
    ArtifactReadReceipt,
    ArtifactReceipt,
    ConcurrentModification,
    PersistReceipt,
    RestoreFailed,
    RestoreReceipt,
    SnapshotConflict,
    SnapshotExpired,
    SnapshotTransientFailure,
    Storage,
    StorageUnavailable,
    WriterFenced,
    WriterLease,
)
from ach_runtime.storage.archive import ArchiveTooLarge, _run_owned, write_capped
from ach_runtime.storage.cache import BoundedCache
from ach_runtime.storage.fencing import LatestEntry, RegistryDoc, assert_lease_current
from ach_runtime.storage.refs import StateRef

_CAS_RETRY_LIMIT = 20
_MAX_REGISTRY_BYTES = 64 * 1024
_DEFAULT_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}\Z")
_TRANSIENT_CODES = {
    "408", "429", "500", "502", "503", "504", "RequestTimeout",
    "RequestTimeoutException", "SlowDown", "InternalError", "ServiceUnavailable",
}
_TRANSIENT_SDK_ERRORS = {
    "EndpointConnectionError", "ConnectionClosedError", "ConnectTimeoutError",
    "ReadTimeoutError", "HTTPClientError",
}


class S3Client(Protocol):
    """The slice of boto3's S3 client surface Storage needs (injectable; fake in tests)."""

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: Any,
        IfMatch: str | None = None,
        IfNoneMatch: str | None = None,
        Metadata: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]: ...


class PreconditionFailed(Exception):
    """The conditional PUT's `If-Match`/`If-None-Match` lost a race."""


def _error_code(exc: BaseException) -> str:
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def _is_not_found(exc: BaseException) -> bool:
    return _error_code(exc) in {"404", "NoSuchKey", "NotFound"}


def _is_precondition_failed(exc: BaseException) -> bool:
    return _error_code(exc) in {"PreconditionFailed", "412", "ConditionalRequestConflict"}


def _is_snapshot_transient(exc: BaseException) -> bool:
    return (
        isinstance(exc, (TimeoutError, ConnectionError))
        or type(exc).__name__ in _TRANSIENT_SDK_ERRORS
        or _error_code(exc) in _TRANSIENT_CODES
    )


def _hash_file(path: Path, *, block_size: int = _DOWNLOAD_CHUNK_BYTES) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(block_size):
            digest.update(chunk)
    return digest.hexdigest()


def _new_staging_path() -> Path:
    """A securely-created (mkstemp: 0600, exclusive) private staging file — not an
    `open("wb")` on a predictable shared-temp pathname (review: "Use secure private
    staging (mkstemp/0600), rather than ordinary open('wb') on a random shared-temp
    pathname")."""
    fd, raw = tempfile.mkstemp(prefix="ach-storage-", dir=tempfile.gettempdir())
    os.close(fd)
    return Path(raw)


async def _receive_bounded(stream: AsyncIterator[bytes], *, max_bytes: int) -> tuple[Path, str, int]:
    """Stream into private staging, bounded; `write_capped` removes the partial file on
    overflow/cancellation. Checksum is computed from disk, never held fully in memory twice,
    off-loop (review: "receive file writes/hash [...] still run synchronously on the loop").

    Staging creation and hashing are each their own owned worker (`_run_owned`): a
    cancellation during either is drained to that worker's real completion before the
    created file is cleaned up, so a cancelled caller never races a worker still using the
    path it is about to unlink."""
    dest, created_cancelled, created_exc = await _run_owned(_new_staging_path)
    if created_exc is not None:
        if created_cancelled:
            raise asyncio.CancelledError()
        raise created_exc
    if created_cancelled:
        await _run_owned(dest.unlink, missing_ok=True)
        raise asyncio.CancelledError()

    size = await write_capped(stream, dest, max_bytes=max_bytes)

    checksum, hash_cancelled, hash_exc = await _run_owned(_hash_file, dest)
    if hash_cancelled or hash_exc is not None:
        await _run_owned(dest.unlink, missing_ok=True)
        if hash_cancelled:
            raise asyncio.CancelledError()
        assert hash_exc is not None
        raise hash_exc
    return dest, checksum, size


def _fetch_with_etag(client: S3Client, bucket: str, key: str, max_bytes: int) -> tuple[bytes, str]:
    """Get + bounded-read + close, all inside ONE synchronous call. Running the whole
    lifecycle in a single `asyncio.to_thread` worker means a cancelled `await` on the outer
    coroutine can never abandon an open body before its `finally` runs — the thread isn't
    interrupted by `asyncio.CancelledError`, only the awaiting coroutine is (review: "A
    cancelled await of threaded get_object can abandon its subsequently returned body")."""
    obj = client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"]
    try:
        raw = body.read(max_bytes + 1) if hasattr(body, "read") else body
    finally:
        if hasattr(body, "close"):
            body.close()
    if len(raw) > max_bytes:
        raise RestoreFailed(f"object {key!r} exceeds the {max_bytes}-byte cap")
    return raw, obj["ETag"]


def _download_capped(
    client: S3Client, bucket: str, key: str, dest: Path, *, max_bytes: int
) -> int:
    """Get + bounded streaming-to-disk + close, all inside ONE synchronous call (same
    cancellation-safety rationale as `_fetch_with_etag`, but writes to private staging
    instead of holding the whole payload in memory)."""
    obj = client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"]
    try:
        total = 0
        with dest.open("wb") as fh:
            if not hasattr(body, "read"):
                fh.write(body)
                return len(body)
            while True:
                remaining = max_bytes - total + 1
                chunk = body.read(min(_DOWNLOAD_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise ArchiveTooLarge(f"object {key!r} exceeds the {max_bytes}-byte cap")
                fh.write(chunk)
        return total
    finally:
        if hasattr(body, "close"):
            body.close()


def _validate_latest_entry(data: object) -> LatestEntry:
    if not isinstance(data, dict):
        raise RestoreFailed("registry 'latest' must be an object or null")
    missing = {"snapshot_id", "checksum", "size", "version"} - data.keys()
    if missing:
        raise RestoreFailed(f"registry 'latest' is missing fields: {sorted(missing)}")
    snapshot_id, checksum, size, version = (
        data["snapshot_id"],
        data["checksum"],
        data["size"],
        data["version"],
    )
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise RestoreFailed("registry 'latest.snapshot_id' must be a non-empty string")
    if not isinstance(checksum, str) or not _SHA256_HEX.match(checksum):
        raise RestoreFailed("registry 'latest.checksum' must be a sha256 hex digest")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RestoreFailed("registry 'latest.size' must be a non-negative integer")
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise RestoreFailed("registry 'latest.version' must be a positive integer")
    committed_at, expires_at = _validate_retention_metadata(data, "registry 'latest'")
    return LatestEntry(snapshot_id=snapshot_id, checksum=checksum, size=size, version=version,
                       committed_at=committed_at, expires_at=expires_at)


def _parse_retention_date(value: object, label: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value:
        raise RestoreFailed(f"{label} must be an aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise RestoreFailed(f"{label} must be an aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RestoreFailed(f"{label} must be timezone-aware")
    return _format_retention_date(parsed)


def _format_retention_date(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("snapshot retention clock must return an aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_retention_metadata(data: dict[str, Any], label: str) -> tuple[str | None, str | None]:
    has_committed = "committed_at" in data
    has_expires = "expires_at" in data
    if not has_committed and not has_expires:
        return None, None  # legacy record; policy is enforced by the caller
    if not has_committed or not has_expires:
        raise RestoreFailed(f"{label} retention metadata must contain both fields")
    committed_at = _parse_retention_date(data["committed_at"], f"{label}.committed_at")
    expires_at = _parse_retention_date(data["expires_at"], f"{label}.expires_at", nullable=True)
    if expires_at is not None and datetime.fromisoformat(expires_at[:-1] + "+00:00") < datetime.fromisoformat(
        committed_at[:-1] + "+00:00"
    ):
        raise RestoreFailed(f"{label}.expires_at precedes committed_at")
    return committed_at, expires_at


def _validate_retention_days(retention_days: int) -> None:
    if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days < 0:
        raise ValueError("retention_days must be a non-negative integer")


def _validate_registry_doc(data: object) -> RegistryDoc:
    """Strict manifest validation — a corrupt registry fails explicitly (`RestoreFailed`),
    never silently reads as never-persisted. `latest` must be a PRESENT key (an explicit
    JSON `null` is legitimate "no commit yet"; an OMITTED key is corrupt — review: "An
    omitted latest still becomes never-persisted")."""
    if not isinstance(data, dict):
        raise RestoreFailed("registry object is not a JSON object")
    required = {"writer_epoch", "writer_instance_id", "generation", "latest"}
    missing = required - data.keys()
    if missing:
        raise RestoreFailed(f"registry object is missing fields: {sorted(missing)}")
    writer_epoch, writer_instance_id, generation = (
        data["writer_epoch"],
        data["writer_instance_id"],
        data["generation"],
    )
    if not isinstance(writer_epoch, int) or isinstance(writer_epoch, bool) or writer_epoch <= 0:
        raise RestoreFailed("registry 'writer_epoch' must be a positive integer")
    if not isinstance(writer_instance_id, str) or not writer_instance_id:
        raise RestoreFailed("registry 'writer_instance_id' must be a non-empty string")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation <= 0:
        raise RestoreFailed("registry 'generation' must be a positive integer")
    latest_raw = data["latest"]
    latest = _validate_latest_entry(latest_raw) if latest_raw is not None else None
    return RegistryDoc(
        writer_epoch=writer_epoch, writer_instance_id=writer_instance_id, generation=generation, latest=latest
    )


def _validate_commit_record(data: object) -> dict[str, Any]:
    """Same strictness as the registry doc — a corrupt commit record must not silently
    disappear and be treated as "never committed", which would re-open the overwrite hole
    for that snapshot_id. Every field is validated using the same conventions as the
    registry document (I6): positive integer epoch/generation/version, nonnegative integer
    size (bool is never accepted as an int here, even though it IS one in Python), nonempty
    string snapshot_id/etag, and an exact lowercase 64-hex checksum digest. Requested-ID
    equality against the caller's `snapshot_id` is checked by `_read_commit_record`, which
    alone knows what was requested — this function only validates the record's own shape."""
    if not isinstance(data, dict):
        raise RestoreFailed("commit record is not a JSON object")
    required = {"snapshot_id", "writer_epoch", "generation", "etag", "checksum", "size", "version"}
    missing = required - data.keys()
    if missing:
        raise RestoreFailed(f"commit record is missing fields: {sorted(missing)}")
    snapshot_id = data["snapshot_id"]
    if not isinstance(snapshot_id, str) or not snapshot_id:
        raise RestoreFailed("commit record 'snapshot_id' must be a non-empty string")
    for field in ("writer_epoch", "generation", "version"):
        value = data[field]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise RestoreFailed(f"commit record {field!r} must be a positive integer")
    size = data["size"]
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RestoreFailed("commit record 'size' must be a non-negative integer")
    etag = data["etag"]
    if not isinstance(etag, str) or not etag:
        raise RestoreFailed("commit record 'etag' must be a non-empty string")
    checksum = data["checksum"]
    if not isinstance(checksum, str) or not _SHA256_HEX.match(checksum):
        raise RestoreFailed("commit record 'checksum' must be a sha256 hex digest")
    _validate_retention_metadata(data, "commit record")
    return data  # validated shape; caller builds the typed PersistReceipt


class S3Storage(Storage):
    """`bucket is None` means no backend was configured: every call fails closed with
    `StorageUnavailable`, never a volatile fallback (contract §7)."""

    def __init__(
        self,
        *,
        bucket: str | None,
        prefix: str,
        client: S3Client | None = None,
        cache: BoundedCache | None = None,
        max_snapshot_bytes: int = _DEFAULT_MAX_SNAPSHOT_BYTES,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client = client
        self._cache = cache
        self._max_snapshot_bytes = max_snapshot_bytes
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _require_client(self) -> tuple[S3Client, str]:
        if self._bucket is None or self._client is None:
            raise StorageUnavailable("storage backend is not configured")
        return self._client, self._bucket

    async def _read_registry(self, ref: StateRef) -> tuple[RegistryDoc | None, str | None]:
        client, bucket = self._require_client()
        key = ref.registry_key(self._prefix)
        try:
            raw, etag = await asyncio.to_thread(_fetch_with_etag, client, bucket, key, _MAX_REGISTRY_BYTES)
        except Exception as exc:
            if _is_not_found(exc):
                return None, None
            raise
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RestoreFailed(f"registry object is not valid JSON: {exc}") from exc
        return _validate_registry_doc(parsed), etag

    async def _read_commit_record(
        self, ref: StateRef, snapshot_id: str, *, retention_days: int = 0
    ) -> PersistReceipt | None:
        client, bucket = self._require_client()
        key = ref.commit_record_key(self._prefix, snapshot_id)
        try:
            raw, _ = await asyncio.to_thread(
                _fetch_with_etag, client, bucket, key, _MAX_REGISTRY_BYTES
            )
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise RestoreFailed(f"commit record is not valid JSON: {exc}") from exc
        data = _validate_commit_record(parsed)
        if data["snapshot_id"] != snapshot_id:
            raise RestoreFailed(
                f"commit record at {key!r} is for snapshot {data['snapshot_id']!r}, "
                f"not the requested {snapshot_id!r}"
            )
        committed_at, expires_at = _validate_retention_metadata(data, "commit record")
        if retention_days > 0 and committed_at is None:
            raise RestoreFailed("legacy commit record has no snapshot retention metadata")
        return PersistReceipt(
            ref=ref,
            snapshot_id=data["snapshot_id"],
            writer_epoch=data["writer_epoch"],
            generation=data["generation"],
            etag=data["etag"],
            checksum=data["checksum"],
            size=data["size"],
            version=data["version"],
            committed_at=committed_at,
            expires_at=expires_at,
        )

    async def _write_commit_record(self, ref: StateRef, receipt: PersistReceipt) -> None:
        client, bucket = self._require_client()
        key = ref.commit_record_key(self._prefix, receipt.snapshot_id)
        document = {
                "snapshot_id": receipt.snapshot_id,
                "writer_epoch": receipt.writer_epoch,
                "generation": receipt.generation,
                "etag": receipt.etag,
                "checksum": receipt.checksum,
                "size": receipt.size,
                "version": receipt.version,
            }
        if receipt.committed_at is not None:
            document["committed_at"] = receipt.committed_at
            document["expires_at"] = receipt.expires_at
        payload = json.dumps(document).encode("utf-8")
        try:
            await asyncio.to_thread(
                client.put_object, Bucket=bucket, Key=key, Body=payload, IfNoneMatch="*"
            )
        except Exception as exc:
            if not _is_precondition_failed(exc):
                raise
            # Someone (this writer's own earlier attempt, or a concurrent one) already wrote
            # a record at this immutable key. Never assume it is ours — read and validate it
            # before treating this write as redundant (review/I6: "verify conditional record
            # collisions rather than assuming their contents"); a missing/corrupt/different
            # collision must fail loudly, never silently "succeed" over someone else's record.
            existing = await self._read_commit_record(ref, receipt.snapshot_id)
            if existing != receipt:
                raise RestoreFailed(
                    f"commit record collision for snapshot {receipt.snapshot_id!r} at {key!r} "
                    "does not match this writer's own receipt"
                ) from exc

    async def _cas_write_registry(
        self, ref: StateRef, doc: RegistryDoc, *, expected_etag: str | None
    ) -> str:
        client, bucket = self._require_client()
        key = ref.registry_key(self._prefix)
        document = asdict(doc)
        if doc.latest is not None and doc.latest.committed_at is None:
            document["latest"].pop("committed_at", None)
            document["latest"].pop("expires_at", None)
        payload = json.dumps(document).encode("utf-8")
        kwargs: dict[str, Any] = (
            {"IfNoneMatch": "*"} if expected_etag is None else {"IfMatch": expected_etag}
        )
        try:
            result = await asyncio.to_thread(
                client.put_object, Bucket=bucket, Key=key, Body=payload, **kwargs
            )
        except Exception as exc:
            if _is_precondition_failed(exc):
                raise PreconditionFailed(str(exc)) from exc
            raise
        return str(result["ETag"])

    async def _upload_snapshot_immutable(
        self, ref: StateRef, snapshot_id: str, dest: Path, checksum: str, size: int,
        owner: dict[str, str],
    ) -> bool:
        """Conditional, content-verified immutable creation. A collision on `snapshot_id`
        with different bytes is rejected here, before the registry is ever touched. The PUT
        (which opens and reads `dest`) runs as its own owned worker: a cancellation is
        drained to the worker's real completion — closing its read handle on `dest` — before
        the caller is allowed to unlink that same staging path (I2/I3: "A staging cleanup
        must not race a worker that reopens the path").

        `owner` tags the object with the uploading lease epoch/pod and the registry
        generation it was uploaded against. Returns `True` when this call created the object
        or the matching existing object carries the same `owner` tag: a commit always bumps
        the generation, so an unchanged generation under the same lease proves that object is
        an earlier, never-committed upload of this same attempt (a CAS that failed after the
        upload). Returns `False` for any other matching collision (I1): the caller must treat
        that as ambiguous — never as license to publish a fresh registry generation over it."""
        client, bucket = self._require_client()
        key = ref.snapshot_key(self._prefix, snapshot_id)

        def _put() -> dict[str, Any]:
            with dest.open("rb") as fh:
                return client.put_object(
                    Bucket=bucket, Key=key, Body=fh, IfNoneMatch="*", Metadata={"sha256": checksum, **owner}
                )

        _, put_cancelled, put_exc = await _run_owned(_put)
        if put_cancelled:
            raise asyncio.CancelledError()
        if put_exc is None:
            return True
        if not _is_precondition_failed(put_exc):
            raise put_exc

        def _head() -> dict[str, Any]:
            return client.head_object(Bucket=bucket, Key=key)

        existing, head_cancelled, head_exc = await _run_owned(_head)
        if head_cancelled:
            raise asyncio.CancelledError()
        if head_exc is not None:
            raise head_exc
        metadata = existing.get("Metadata") or {}
        if metadata.get("sha256") != checksum or int(existing.get("ContentLength", -1)) != size:
            raise SnapshotConflict(f"snapshot {snapshot_id!r} already exists with different bytes")
        return all(metadata.get(key) == value for key, value in owner.items())

    async def _reconciled_receipt_if_applied(
        self, ref: StateRef, lease: WriterLease, snapshot_id: str, checksum: str, size: int,
        *, retention_days: int = 0,
    ) -> tuple[PersistReceipt | None, RegistryDoc]:
        """One reconciliation read after an ambiguous or rejected registry CAS (I1): if
        `latest` already reflects exactly what we tried to commit, the write landed even
        though we never saw a clean ack — repair its durable record (if missing) and return
        it, without writing the registry again. `assert_lease_current` still enforces that a
        superseded epoch/pod sees `WriterFenced` even on this read-only reconciliation path."""
        refreshed, refreshed_etag = await self._read_registry(ref)
        assert_lease_current(refreshed, lease)
        assert refreshed is not None
        if (
            refreshed.latest is not None
            and refreshed.latest.snapshot_id == snapshot_id
            and refreshed.latest.checksum == checksum
            and refreshed.latest.size == size
        ):
            receipt = _receipt_from(ref, refreshed, refreshed_etag or "", retention_days=retention_days)
            await self._write_commit_record(ref, receipt)
            return receipt, refreshed
        return None, refreshed

    async def current_writer(self, ref: StateRef) -> str | None:
        """Instance ID the registry records as the writer, or None if the domain never persisted."""
        self._require_client()
        doc, _ = await self._read_registry(ref)
        return doc.writer_instance_id if doc is not None else None

    async def acquire_writer(self, ref: StateRef, writer_instance_id: str) -> WriterLease:
        self._require_client()
        for _ in range(_CAS_RETRY_LIMIT):
            try:
                current, etag = await self._read_registry(ref)
                new_doc = (
                    current.with_new_writer(writer_instance_id)
                    if current is not None
                    else RegistryDoc.empty(writer_instance_id)
                )
                new_etag = await self._cas_write_registry(ref, new_doc, expected_etag=etag)
            except PreconditionFailed:
                continue
            except Exception as exc:
                if _is_snapshot_transient(exc):
                    raise SnapshotTransientFailure("transient S3 writer acquisition failure") from exc
                raise
            base_etag = new_etag if new_doc.latest is not None else None
            return WriterLease(
                ref=ref, writer_instance_id=writer_instance_id, epoch=new_doc.writer_epoch, base_etag=base_etag
            )
        raise RuntimeError("acquire_writer: exhausted CAS retries under contention")

    async def persist(
        self,
        ref: StateRef,
        snapshot_id: str,
        stream: AsyncIterator[bytes],
        lease: WriterLease,
        expected_base: str | None,
        *,
        retention_days: int = 0,
    ) -> PersistReceipt:
        _validate_retention_days(retention_days)
        if lease.ref != ref:
            raise WriterFenced("lease was issued for a different StateRef")

        try:
            current, current_etag = await self._read_registry(ref)
        except Exception as exc:
            if _is_snapshot_transient(exc):
                raise SnapshotTransientFailure("transient S3 registry read failure") from exc
            raise
        assert_lease_current(current, lease)
        assert current is not None  # assert_lease_current guarantees this

        dest, checksum, size = await _receive_bounded(stream, max_bytes=self._max_snapshot_bytes)

        async def _commit() -> PersistReceipt:
            # Durable per-snapshot correlation FIRST — works whether or not `snapshot_id` is
            # still `current.latest`, recovering the EXACT original receipt for a historical
            # retry (review: "Historical idempotent receipt recovery is still absent").
            historical = await self._read_commit_record(ref, snapshot_id, retention_days=retention_days)
            if historical is not None:
                if historical.checksum == checksum and historical.size == size:
                    return historical
                raise SnapshotConflict(
                    f"snapshot {snapshot_id!r} already committed historically with a different checksum/size"
                )

            if (
                current.latest is not None
                and current.latest.snapshot_id == snapshot_id
                and current.latest.checksum == checksum
                and current.latest.size == size
            ):
                # The registry already reflects this exact commit as `latest`, but its
                # durable per-snapshot record is missing (a prior write died, or was never
                # acknowledged) — repair the record and return, without touching the
                # registry or bumping its generation (I1: "repair missing records for
                # matching current latest without a generation bump"). This must run BEFORE
                # the `expected_base` predecessor check below, or an `expected_base=None`
                # retry of the already-committed current latest would be wrongly rejected.
                receipt = _receipt_from(ref, current, current_etag or "", retention_days=retention_days)
                await self._write_commit_record(ref, receipt)
                return receipt

            owner = {
                "writer-epoch": str(lease.epoch),
                "writer-instance-id": lease.writer_instance_id,
                "base-generation": str(current.generation),
            }
            uploaded = await self._upload_snapshot_immutable(
                ref, snapshot_id, dest, checksum, size, owner
            )
            if not uploaded:
                # The object already exists with matching bytes, yet it is neither a durable
                # historical receipt, the current latest, nor this lease's own uncommitted
                # upload against the unchanged registry generation — a receipt-less nonlatest
                # collision (an old snapshot whose record was lost, or an orphan from another
                # epoch or an earlier registry generation). Root ruling: this is explicit
                # ambiguity, never silently republished as a fresh registry generation (I1:
                # "report explicit conflict ... before attempting to publish a new registry
                # generation").
                raise ConcurrentModification(
                    f"snapshot {snapshot_id!r} object already exists without a historical "
                    "receipt or a matching current latest; ambiguous collision"
                )

            if expected_base is None:
                if current.latest is not None:
                    raise ConcurrentModification(
                        "expected_base=None but a snapshot is already committed for this domain"
                    )
                base = current_etag
            else:
                base = expected_base

            new_version = (current.latest.version if current.latest else 0) + 1
            committed_at = _format_retention_date(self._clock())
            committed_dt = datetime.fromisoformat(committed_at[:-1] + "+00:00")
            expires_at = (
                _format_retention_date(committed_dt + timedelta(days=retention_days))
                if retention_days > 0 else None
            )
            new_doc = current.with_committed_snapshot(
                lease.epoch, lease.writer_instance_id,
                LatestEntry(snapshot_id, checksum, size, new_version, committed_at, expires_at),
            )
            try:
                new_etag = await self._cas_write_registry(ref, new_doc, expected_etag=base)
            except PreconditionFailed:
                # Deterministic: the conditional check itself rejected this write, so it
                # certainly did not apply. A matching current latest means someone else
                # committed the exact same (snapshot_id, checksum, size) concurrently —
                # reconcile to that. Anything else is a genuine conflict.
                receipt, _ = await self._reconciled_receipt_if_applied(
                    ref, lease, snapshot_id, checksum, size, retention_days=retention_days
                )
                if receipt is not None:
                    return receipt
                raise ConcurrentModification(
                    f"registry changed concurrently while committing snapshot {snapshot_id!r}"
                ) from None
            except Exception as exc:
                # Ambiguous: the backend raised something other than a precondition failure
                # (a lost ack, a transient network error, ...) — we genuinely do not know
                # whether the write applied server-side. Exactly one reconciliation read
                # decides (I1): a matching latest means it DID apply (reconcile/repair, no
                # second registry write); a DIFFERENT committed latest means a concurrent
                # writer's commit landed instead (`ConcurrentModification`); an UNCHANGED
                # latest means ours genuinely never applied — surface the original error,
                # never mask it with a invented conflict or a blind retry.
                receipt, refreshed = await self._reconciled_receipt_if_applied(
                    ref, lease, snapshot_id, checksum, size, retention_days=retention_days
                )
                if receipt is not None:
                    return receipt
                if refreshed.latest != current.latest:
                    raise ConcurrentModification(
                        f"registry changed concurrently while committing snapshot {snapshot_id!r}"
                    ) from exc
                raise
            receipt = PersistReceipt(
                ref=ref,
                snapshot_id=snapshot_id,
                writer_epoch=lease.epoch,
                generation=new_doc.generation,
                etag=new_etag,
                checksum=checksum,
                size=size,
                version=new_version,
                committed_at=committed_at,
                expires_at=expires_at,
            )
            await self._write_commit_record(ref, receipt)
            return receipt

        # Owned staging cleanup is offloaded and drained (I2: "persist still unlinks its
        # owned staging file on the event loop"). A pre-existing primary failure or
        # cancellation always wins over the cleanup's own outcome; with no primary error, a
        # cleanup failure or cancellation is propagated rather than hidden (I3 discipline,
        # applied equally to persist's own cleanup).
        result: PersistReceipt | None = None
        primary_cancelled = False
        primary_exc: BaseException | None = None
        try:
            result = await _commit()
        except asyncio.CancelledError:
            primary_cancelled = True
        except BaseException as exc:  # noqa: BLE001 - re-raised below, after cleanup
            primary_exc = exc

        _, unlink_cancelled, unlink_exc = await _run_owned(dest.unlink, missing_ok=True)

        if primary_cancelled:
            raise asyncio.CancelledError()
        if primary_exc is not None:
            if isinstance(primary_exc, SnapshotTransientFailure):
                raise primary_exc
            if _is_snapshot_transient(primary_exc):
                raise SnapshotTransientFailure("transient S3 snapshot persistence failure") from primary_exc
            raise primary_exc
        if unlink_cancelled:
            raise asyncio.CancelledError()
        if unlink_exc is not None:
            raise unlink_exc
        assert result is not None
        return result

    async def restore(self, ref: StateRef, *, retention_days: int = 0) -> RestoreReceipt | None:
        _validate_retention_days(retention_days)
        client, bucket = self._require_client()
        doc, etag = await self._read_registry(ref)
        if doc is None or doc.latest is None:
            return None
        latest = doc.latest
        if retention_days > 0 and latest.committed_at is None:
            raise RestoreFailed("legacy snapshot has no retention metadata")
        _check_snapshot_expiry(latest, self._clock())
        if latest.size > self._max_snapshot_bytes:
            # Never trust the registered size alone (review: "Restore trusts any
            # nonnegative registered size, never applies max_snapshot_bytes").
            raise RestoreFailed(
                f"snapshot {latest.snapshot_id!r} registered size {latest.size} exceeds "
                f"the {self._max_snapshot_bytes}-byte cap"
            )
        key = ref.snapshot_key(self._prefix, latest.snapshot_id)
        registry_key = ref.registry_key(self._prefix)
        cache = self._cache
        max_bytes = self._max_snapshot_bytes

        cached: bytes | None = None
        if cache is not None:
            cached, get_cancelled, get_exc = await _run_owned(
                cache.get, registry_key, latest.version, expected_checksum=latest.checksum
            )
            if get_cancelled:
                raise asyncio.CancelledError()
            if get_exc is not None:
                raise get_exc

        if cached is None:
            # Only touch the backend when the bounded cache has no already-verified copy for
            # this exact committed version — a cache hit must serve without a backend round
            # trip (dispatch §4: "cannot hide a changed latest pointer" is handled by keying
            # on `latest.version`, not by always re-checking the backend).
            try:
                obj = await asyncio.to_thread(client.head_object, Bucket=bucket, Key=key)
            except Exception as exc:
                if _is_not_found(exc):
                    raise RestoreFailed(
                        f"registry references snapshot {latest.snapshot_id!r} but its bytes are missing"
                    ) from exc
                raise
            if int(obj.get("ContentLength", -1)) != latest.size:
                raise RestoreFailed(
                    f"snapshot {latest.snapshot_id!r} size mismatch: registry={latest.size} object={obj.get('ContentLength')}"
                )

        async def _open() -> AsyncIterator[bytes]:
            _check_snapshot_expiry(latest, self._clock())
            if cached is not None:
                yield cached
                return

            staging, created_cancelled, created_exc = await _run_owned(_new_staging_path)
            if created_exc is not None:
                if created_cancelled:
                    raise asyncio.CancelledError()
                raise created_exc
            if created_cancelled:
                await _run_owned(staging.unlink, missing_ok=True)
                raise asyncio.CancelledError()

            payload: bytes | None = None
            primary_cancelled = False
            primary_exc: BaseException | None = None
            try:
                size, dl_cancelled, dl_exc = await _run_owned(
                    _download_capped, client, bucket, key, staging, max_bytes=max_bytes
                )
                if dl_cancelled:
                    raise asyncio.CancelledError()
                if dl_exc is not None:
                    raise dl_exc
                if size != latest.size:
                    raise RestoreFailed(
                        f"snapshot {latest.snapshot_id!r} size mismatch while streaming content"
                    )
                checksum, hash_cancelled, hash_exc = await _run_owned(_hash_file, staging)
                if hash_cancelled:
                    raise asyncio.CancelledError()
                if hash_exc is not None:
                    raise hash_exc
                if checksum != latest.checksum:
                    raise RestoreFailed(
                        f"snapshot {latest.snapshot_id!r} failed checksum verification"
                    )
                payload, read_cancelled, read_exc = await _run_owned(staging.read_bytes)
                if read_cancelled:
                    raise asyncio.CancelledError()
                if read_exc is not None:
                    raise read_exc
            except asyncio.CancelledError:
                primary_cancelled = True
            except BaseException as exc:  # noqa: BLE001 - re-raised below, after cleanup
                primary_exc = exc

            # Off-loaded, drained unlink — by the time this runs, every worker above has
            # already been shielded to real completion (or never started), so cleanup never
            # races a worker still holding `staging` open. A pre-existing primary
            # failure/cancellation always wins over the cleanup's own outcome; with no
            # primary error, a cleanup failure or cancellation must still be propagated
            # before any cache write or yield — never silently discarded while serving bytes
            # whose cleanup never actually completed (I3).
            _, unlink_cancelled, unlink_exc = await _run_owned(staging.unlink, missing_ok=True)

            if primary_cancelled:
                raise asyncio.CancelledError()
            if primary_exc is not None:
                raise primary_exc
            if unlink_cancelled:
                raise asyncio.CancelledError()
            if unlink_exc is not None:
                raise unlink_exc
            assert payload is not None

            _check_snapshot_expiry(latest, self._clock())

            if cache is not None:
                _, put_cancelled, put_exc = await _run_owned(
                    cache.put, registry_key, latest.version, payload
                )
                if put_cancelled:
                    raise asyncio.CancelledError()
                if put_exc is not None:
                    raise put_exc
            _check_snapshot_expiry(latest, self._clock())
            yield payload

        return RestoreReceipt(
            ref=ref,
            snapshot_id=latest.snapshot_id,
            writer_epoch=doc.writer_epoch,
            generation=doc.generation,
            etag=etag or "",
            checksum=latest.checksum,
            size=latest.size,
            version=latest.version,
            open=_open,
            committed_at=latest.committed_at,
            expires_at=latest.expires_at,
        )

    async def create_artifact(
        self, owner: StateRef, producer: ArtifactProducer, idempotency_key: str,
        stream: AsyncIterator[bytes], *, mime: str, name: str, policy: ArtifactsBlock,
    ) -> ArtifactReceipt:
        client, bucket = self._require_client()
        from ach_runtime.storage.artifacts import create_artifact_s3

        return await create_artifact_s3(
            client, bucket, self._prefix, owner, producer, idempotency_key, stream,
            mime=mime, name=name, policy=policy,
        )

    async def read_artifact(self, owner: StateRef, artifact_id: str) -> ArtifactReadReceipt:
        client, bucket = self._require_client()
        from ach_runtime.storage.artifacts import read_artifact_s3

        return await read_artifact_s3(client, bucket, self._prefix, owner, artifact_id)


def _check_snapshot_expiry(latest: LatestEntry, now: datetime) -> None:
    if latest.expires_at is None:
        return
    now_utc = _format_retention_date(now)
    if datetime.fromisoformat(now_utc[:-1] + "+00:00") >= datetime.fromisoformat(
        latest.expires_at[:-1] + "+00:00"
    ):
        raise SnapshotExpired(f"snapshot {latest.snapshot_id!r} expired at {latest.expires_at}")


def _receipt_from(
    ref: StateRef, doc: RegistryDoc, etag: str, *, retention_days: int = 0
) -> PersistReceipt:
    assert doc.latest is not None
    if retention_days > 0 and doc.latest.committed_at is None:
        raise RestoreFailed("legacy snapshot has no retention metadata")
    return PersistReceipt(
        ref=ref,
        snapshot_id=doc.latest.snapshot_id,
        writer_epoch=doc.writer_epoch,
        generation=doc.generation,
        etag=etag,
        checksum=doc.latest.checksum,
        size=doc.latest.size,
        version=doc.latest.version,
        committed_at=doc.latest.committed_at,
        expires_at=doc.latest.expires_at,
    )

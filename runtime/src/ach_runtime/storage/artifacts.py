# SPDX-License-Identifier: MIT
"""Immutable artifact publication over the existing S3 storage boundary."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from ach_runtime.config.schema import ArtifactsBlock
from ach_runtime.storage.api import (
    ArtifactConflict,
    ArtifactDisabled,
    ArtifactExpired,
    ArtifactMetadata,
    ArtifactNotFound,
    ArtifactProducer,
    ArtifactReadReceipt,
    ArtifactReceipt,
    ArtifactTooLarge,
    RestoreFailed,
)
from ach_runtime.storage.archive import ArchiveTooLarge, _run_owned
from ach_runtime.storage.refs import StateRef

if TYPE_CHECKING:
    from ach_runtime.storage.s3 import S3Client

_ID = re.compile(r"^[0-9a-f]{64}$")
_SHA256 = _ID
_MAX_MANIFEST_BYTES = 64 * 1024
_CHUNK = 1024 * 1024


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _artifact_id(owner: StateRef, idempotency_key: str) -> str:
    raw = json.dumps(
        ["artifact-v1", owner.agent.namespace, owner.agent.uid, owner.workspace_ref, idempotency_key],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _descriptor(owner: StateRef, producer: ArtifactProducer, mime: str, name: str) -> bytes:
    raw = json.dumps(
        [owner.agent.namespace, owner.agent.uid, owner.workspace_ref,
         producer.session_ref, producer.invocation_id, mime, name],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return raw


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("artifact dates must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise RestoreFailed(f"artifact manifest {field} must be an ISO-8601 string")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RestoreFailed(f"artifact manifest {field} is invalid") from exc
    if date.tzinfo is None or date.utcoffset() is None:
        raise RestoreFailed(f"artifact manifest {field} must be timezone-aware")
    return date.astimezone(timezone.utc)


def _metadata_json(meta: ArtifactMetadata) -> dict[str, Any]:
    return {
        "artifact_id": meta.artifact_id,
        "owner": {"namespace": meta.owner.agent.namespace, "uid": meta.owner.agent.uid,
                  "workspace_ref": meta.owner.workspace_ref},
        "producer": {"session_ref": meta.producer.session_ref,
                     "invocation_id": meta.producer.invocation_id},
        "mime": meta.mime, "name": meta.name, "size": meta.size,
        "checksum": meta.checksum, "created_at": _utc(meta.created_at),
        "expires_at": _utc(meta.expires_at) if meta.expires_at else None,
    }


def _parse_manifest(raw: bytes) -> ArtifactMetadata:
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise RestoreFailed("artifact manifest exceeds the 64 KiB cap")
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RestoreFailed("artifact manifest is not valid JSON") from exc
    required = {"artifact_id", "owner", "producer", "mime", "name", "size",
                "checksum", "created_at", "expires_at"}
    if not isinstance(doc, dict) or set(doc) != required:
        raise RestoreFailed("artifact manifest has an invalid shape")
    owner, producer = doc["owner"], doc["producer"]
    if not isinstance(owner, dict) or set(owner) != {"namespace", "uid", "workspace_ref"}:
        raise RestoreFailed("artifact manifest owner is invalid")
    if not isinstance(producer, dict) or set(producer) != {"session_ref", "invocation_id"}:
        raise RestoreFailed("artifact manifest producer is invalid")
    artifact_id, checksum, size = doc["artifact_id"], doc["checksum"], doc["size"]
    if not isinstance(artifact_id, str) or not _ID.fullmatch(artifact_id):
        raise RestoreFailed("artifact manifest ID is invalid")
    if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
        raise RestoreFailed("artifact manifest checksum is invalid")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise RestoreFailed("artifact manifest size is invalid")
    if not all(isinstance(v, str) and v for v in owner.values()):
        raise RestoreFailed("artifact manifest owner values are invalid")
    if not all(isinstance(v, str) and v for v in producer.values()):
        raise RestoreFailed("artifact manifest producer values are invalid")
    if not isinstance(doc["mime"], str) or not doc["mime"] or not isinstance(doc["name"], str) or not doc["name"]:
        raise RestoreFailed("artifact manifest MIME or name is invalid")
    try:
        parsed_owner = StateRef(
            agent={"name": "artifact", "namespace": owner["namespace"], "uid": owner["uid"]},
            workspace_ref=owner["workspace_ref"], domain="workspace",
        )
    except ValidationError as exc:
        raise RestoreFailed("artifact manifest owner is invalid") from exc
    return ArtifactMetadata(
        artifact_id=artifact_id,
        owner=parsed_owner,
        producer=ArtifactProducer(**producer), mime=doc["mime"], name=doc["name"],
        size=size, checksum=checksum, created_at=_parse_date(doc["created_at"], "created_at"),
        expires_at=None if doc["expires_at"] is None else _parse_date(doc["expires_at"], "expires_at"),
    )


def _object_keys(owner: StateRef, prefix: str, artifact_id: str) -> tuple[str, str]:
    base = f"{owner._base_key(prefix)}/artifacts/{artifact_id}"
    return f"{base}/blob", f"{base}/metadata.json"


def _read_body(client: S3Client, bucket: str, key: str, cap: int) -> bytes:
    obj = client.get_object(Bucket=bucket, Key=key)
    body = obj["Body"]
    try:
        data = body.read(cap + 1) if hasattr(body, "read") else body
    finally:
        if hasattr(body, "close"):
            body.close()
    if len(data) > cap:
        raise RestoreFailed(f"artifact object {key!r} exceeds the {cap}-byte cap")
    return data


def _head(client: S3Client, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        return client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        from ach_runtime.storage.s3 import _is_not_found

        if _is_not_found(exc):
            return None
        raise


def _body_metadata(meta: ArtifactMetadata, descriptor: bytes) -> dict[str, str]:
    packed = base64.urlsafe_b64encode(descriptor).decode("ascii")
    return {
        "ach-artifact-id": meta.artifact_id,
        "ach-descriptor": packed,
        "ach-checksum": meta.checksum,
        "ach-size": str(meta.size),
        "ach-created-at": _utc(meta.created_at),
        "ach-expires-at": _utc(meta.expires_at) if meta.expires_at else "none",
    }


def _recover_body_dates(head: dict[str, Any], expected: dict[str, str]) -> tuple[datetime, datetime | None] | None:
    metadata = head.get("Metadata", {})
    stable_fields = ("ach-artifact-id", "ach-descriptor", "ach-checksum", "ach-size")
    if (any(metadata.get(field) != expected[field] for field in stable_fields)
            or int(head.get("ContentLength", -1)) != int(expected["ach-size"])):
        return None
    created = _parse_date(metadata.get("ach-created-at"), "body created_at")
    expires_raw = metadata.get("ach-expires-at")
    expires = None if expires_raw == "none" else _parse_date(expires_raw, "body expires_at")
    return created, expires


def _receipt(meta: ArtifactMetadata) -> ArtifactReceipt:
    return ArtifactReceipt(meta.artifact_id, f"artifact:{meta.artifact_id}", meta)


async def _cleanup_path(path: Path) -> None:
    _, cancelled, exc = await _run_owned(path.unlink, missing_ok=True)
    if cancelled:
        raise asyncio.CancelledError()
    if exc is not None:
        raise exc


def _check_manifest_match(existing: ArtifactMetadata, expected: ArtifactMetadata) -> ArtifactReceipt:
    if (existing.artifact_id != expected.artifact_id
            or existing.owner.agent.namespace != expected.owner.agent.namespace
            or existing.owner.agent.uid != expected.owner.agent.uid
            or existing.owner.workspace_ref != expected.owner.workspace_ref
            or existing.producer != expected.producer or existing.mime != expected.mime
            or existing.name != expected.name or existing.size != expected.size
            or existing.checksum != expected.checksum):
        raise ArtifactConflict("artifact id is already registered with a different publication descriptor")
    # The manifest is authoritative for immutable timestamps; use the validated values while
    # retaining the current trusted StateRef's identity name (name is not an ownership key).
    recovered = ArtifactMetadata(
        existing.artifact_id, expected.owner, existing.producer, existing.mime, existing.name,
        existing.size, existing.checksum, existing.created_at, existing.expires_at,
    )
    return _receipt(recovered)


async def create_artifact_s3(
    client: S3Client, bucket: str, prefix: str, owner: StateRef, producer: ArtifactProducer,
    idempotency_key: str, stream: AsyncIterator[bytes], *, mime: str, name: str,
    policy: ArtifactsBlock,
) -> ArtifactReceipt:
    if not policy.enabled:
        raise ArtifactDisabled("artifacts are disabled")
    if owner.domain != "workspace":
        raise ValueError("artifact owner must be a Workspace StateRef")
    StateRef(agent=owner.agent, workspace_ref=owner.workspace_ref, domain="session", session_ref=producer.session_ref)
    if not idempotency_key or not mime or not name or not producer.invocation_id:
        raise ValueError("artifact key, MIME, name and invocation ID must be non-empty")
    artifact_id = _artifact_id(owner, idempotency_key)
    body_key, manifest_key = _object_keys(owner, prefix, artifact_id)
    from ach_runtime.storage.s3 import _receive_bounded

    try:
        path, checksum, size = await _receive_bounded(stream, max_bytes=policy.max_artifact_bytes)
    except ArchiveTooLarge as exc:
        raise ArtifactTooLarge(str(exc)) from exc

    try:
        descriptor = _descriptor(owner, producer, mime, name)
        expected_hash = hashlib.sha256(descriptor).hexdigest()
        head = await asyncio.to_thread(_head, client, bucket, body_key)
        if head is not None:
            body_meta = head.get("Metadata", {})
            if (body_meta.get("ach-artifact-id") != artifact_id
                    or body_meta.get("ach-descriptor") != base64.urlsafe_b64encode(descriptor).decode("ascii")
                    or body_meta.get("ach-checksum") != checksum or body_meta.get("ach-size") != str(size)
                    or int(head.get("ContentLength", -1)) != size):
                raise ArtifactConflict("idempotency key already has different immutable content")
            created = _parse_date(body_meta.get("ach-created-at"), "body created_at")
            expires_raw = body_meta.get("ach-expires-at")
            expires = None if expires_raw == "none" else _parse_date(expires_raw, "body expires_at")
            expected = ArtifactMetadata(artifact_id, owner, producer, mime, name, size, checksum, created, expires)
        else:
            created = _utc_now().astimezone(timezone.utc)
            expires = created + timedelta(days=policy.retention_days) if policy.retention_days else None
            expected = ArtifactMetadata(artifact_id, owner, producer, mime, name, size, checksum, created, expires)
            upload_meta = _body_metadata(expected, descriptor)

            def put_body() -> None:
                with path.open("rb") as body:
                    client.put_object(Bucket=bucket, Key=body_key, Body=body,
                                      IfNoneMatch="*", Metadata=upload_meta)

            _, was_cancelled, upload_error = await _run_owned(put_body)
            if was_cancelled:
                raise asyncio.CancelledError()
            if upload_error is not None:
                original = upload_error
                head = await asyncio.to_thread(_head, client, bucket, body_key)
                if head is None:
                    raise original
                dates = _recover_body_dates(head, upload_meta)
                if dates is None:
                    raise ArtifactConflict("idempotency key raced with different immutable content") from original
                expected = ArtifactMetadata(
                    artifact_id, owner, producer, mime, name, size, checksum, dates[0], dates[1],
                )

        manifest = _metadata_json(expected)
        try:
            await asyncio.to_thread(
                client.put_object, Bucket=bucket, Key=manifest_key, Body=_json_bytes(manifest),
                IfNoneMatch="*", Metadata={"ach-artifact-id": artifact_id,
                                             "ach-checksum": checksum,
                                             "ach-descriptor": expected_hash},
            )
            return _receipt(expected)
        except Exception as original:
            try:
                raw = await asyncio.to_thread(_read_body, client, bucket, manifest_key, _MAX_MANIFEST_BYTES)
            except Exception as read_exc:
                from ach_runtime.storage.s3 import _is_not_found, _is_precondition_failed

                if _is_not_found(read_exc):
                    raise original
                raise read_exc
            existing = _parse_manifest(raw)
            return _check_manifest_match(existing, expected)
    finally:
        await _cleanup_path(path)


async def read_artifact_s3(
    client: S3Client, bucket: str, prefix: str, owner: StateRef, artifact_id: str,
) -> ArtifactReadReceipt:
    if owner.domain != "workspace":
        raise ValueError("artifact owner must be a Workspace StateRef")
    if not _ID.fullmatch(artifact_id):
        raise ArtifactNotFound("artifact not found")
    body_key, manifest_key = _object_keys(owner, prefix, artifact_id)
    from ach_runtime.storage.s3 import _is_not_found

    try:
        raw = await asyncio.to_thread(_read_body, client, bucket, manifest_key, _MAX_MANIFEST_BYTES)
    except Exception as exc:
        if _is_not_found(exc):
            raise ArtifactNotFound("artifact not found") from exc
        raise
    meta = _parse_manifest(raw)
    if (meta.artifact_id != artifact_id
            or meta.owner.agent.namespace != owner.agent.namespace
            or meta.owner.agent.uid != owner.agent.uid
            or meta.owner.workspace_ref != owner.workspace_ref):
        raise ArtifactNotFound("artifact not found")
    # The durable ownership contract is namespace/UID/Workspace; preserve the caller's
    # already-bound identity display name in the in-process receipt.
    meta = ArtifactMetadata(
        meta.artifact_id, owner, meta.producer, meta.mime, meta.name, meta.size,
        meta.checksum, meta.created_at, meta.expires_at,
    )
    if meta.expires_at is not None and _utc_now() >= meta.expires_at:
        raise ArtifactExpired("artifact has expired")

    async def _open() -> AsyncIterator[bytes]:
        if meta.expires_at is not None and _utc_now() >= meta.expires_at:
            raise ArtifactExpired("artifact has expired")
        from ach_runtime.storage.s3 import _download_capped, _hash_file, _new_staging_path

        path, cancelled, exc = await _run_owned(_new_staging_path)
        if cancelled:
            raise asyncio.CancelledError()
        if exc is not None:
            raise exc
        assert isinstance(path, Path)
        try:
            size, cancelled, exc = await _run_owned(
                _download_capped, client, bucket, body_key, path, max_bytes=meta.size,
            )
            if cancelled:
                raise asyncio.CancelledError()
            if exc is not None:
                if isinstance(exc, ArchiveTooLarge):
                    raise RestoreFailed("registered artifact body exceeds its size") from exc
                if _is_not_found(exc):
                    raise RestoreFailed("registered artifact body is missing") from exc
                raise exc
            checksum, cancelled, exc = await _run_owned(_hash_file, path)
            if cancelled:
                raise asyncio.CancelledError()
            if exc is not None:
                raise exc
            if size != meta.size or checksum != meta.checksum:
                raise RestoreFailed("registered artifact body failed size or checksum verification")
            offset = 0
            while offset < size:
                chunk, cancelled, exc = await _run_owned(_read_chunk, path, offset, min(_CHUNK, size-offset))
                if cancelled:
                    raise asyncio.CancelledError()
                if exc is not None:
                    raise exc
                if not chunk:
                    raise RestoreFailed("artifact body ended before its registered size")
                offset += len(chunk)
                yield chunk
        finally:
            await _cleanup_path(path)

    return ArtifactReadReceipt(meta, _open)


def _read_chunk(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as stream:
        stream.seek(offset)
        return stream.read(length)

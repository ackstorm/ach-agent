# SPDX-License-Identifier: MIT
"""`Storage` protocol, receipts and its error taxonomy (contract §7/§11, dispatch
§interfaces). The Harness calls this surface in-process — there is no separate Storage
service or wire authentication of its own; whichever backend is configured (e.g. `s3.py`'s
`S3Storage`, assembled by `storage/config.py`) sits behind it. The mini-harness's own
authenticated channel is what gates an execution pod's access to these operations, not this
module. Harness depends only on this surface — never on `s3.py`'s client or bytes directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ach_runtime.config.schema import ArtifactsBlock
from ach_runtime.storage.refs import StateRef


class StorageUnavailable(Exception):
    """No valid backend configured. Fails closed — never a volatile/in-memory fallback."""


class RestoreFailed(Exception):
    """A registry entry exists but its referenced bytes are missing or corrupt."""


class SnapshotExpired(RestoreFailed):
    """A registered snapshot has reached its immutable retention deadline."""


class SnapshotTransientFailure(Exception):
    """A supported transient backend failure may be retried within one close attempt."""


class WriterFenced(Exception):
    """A stale writer epoch tried to persist after a newer epoch was acquired."""


class SnapshotConflict(Exception):
    """Same `snapshot_id` committed twice with different bytes."""


class ConcurrentModification(Exception):
    """The caller's `expected_base` no longer matches the registry, and the attempted write
    is not a recognized idempotent retry of the currently-committed snapshot. Never silently
    re-read a fresh base and succeed anyway — that is the old retry/latest regression."""


class ArtifactDisabled(Exception):
    """Artifact operations are disabled by policy."""


class ArtifactTooLarge(Exception):
    """An artifact stream exceeded its byte cap."""


class ArtifactConflict(Exception):
    """An idempotency key was reused with different publication content."""


class ArtifactNotFound(Exception):
    """No durable manifest registers this artifact."""


class ArtifactExpired(Exception):
    """The artifact has passed its registered expiry time."""


@dataclass(frozen=True, slots=True)
class ArtifactProducer:
    session_ref: str
    invocation_id: str


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    artifact_id: str
    owner: StateRef
    producer: ArtifactProducer
    mime: str
    name: str
    size: int
    checksum: str
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    artifact_id: str
    reference: str
    metadata: ArtifactMetadata


@dataclass(frozen=True, slots=True)
class ArtifactReadReceipt:
    metadata: ArtifactMetadata
    open: "ContentOpener"


@dataclass(frozen=True, slots=True)
class WriterLease:
    ref: StateRef
    writer_instance_id: str
    epoch: int
    base_etag: str | None = None


@dataclass(frozen=True, slots=True)
class PersistReceipt:
    ref: StateRef
    snapshot_id: str
    writer_epoch: int
    generation: int
    etag: str
    checksum: str
    size: int
    version: int
    committed_at: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    """Verified metadata plus a bounded content accessor — never a raw S3 URL."""

    ref: StateRef
    snapshot_id: str
    writer_epoch: int
    generation: int
    etag: str
    checksum: str
    size: int
    version: int
    open: "ContentOpener"
    committed_at: str | None = None
    expires_at: str | None = None


class ContentOpener(Protocol):
    def __call__(self) -> AsyncIterator[bytes]: ...


class Storage(Protocol):
    async def restore(self, ref: StateRef, *, retention_days: int = 0) -> RestoreReceipt | None: ...

    async def current_writer(self, ref: StateRef) -> str | None: ...

    async def acquire_writer(self, ref: StateRef, writer_instance_id: str) -> WriterLease: ...

    async def persist(
        self,
        ref: StateRef,
        snapshot_id: str,
        stream: AsyncIterator[bytes],
        lease: WriterLease,
        expected_base: str | None,
        *,
        retention_days: int = 0,
    ) -> PersistReceipt: ...

    async def create_artifact(
        self, owner: StateRef, producer: ArtifactProducer, idempotency_key: str,
        stream: AsyncIterator[bytes], *, mime: str, name: str, policy: ArtifactsBlock,
    ) -> ArtifactReceipt: ...

    async def read_artifact(self, owner: StateRef, artifact_id: str) -> ArtifactReadReceipt: ...

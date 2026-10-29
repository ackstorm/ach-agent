# SPDX-License-Identifier: MIT
"""Pure CAS-registry document shape and epoch/generation bump rules (dispatch §behavior 2/3).

No I/O here — `s3.py` owns reading/writing the registry object; this module owns the
decision of what the *next* document should look like and when a write must be refused.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ach_runtime.storage.api import WriterLease


@dataclass(frozen=True, slots=True)
class LatestEntry:
    snapshot_id: str
    checksum: str
    size: int
    version: int
    committed_at: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class RegistryDoc:
    writer_epoch: int
    writer_instance_id: str
    generation: int
    latest: LatestEntry | None = None

    @classmethod
    def empty(cls, writer_instance_id: str) -> RegistryDoc:
        return cls(writer_epoch=1, writer_instance_id=writer_instance_id, generation=1, latest=None)

    def with_new_writer(self, writer_instance_id: str) -> RegistryDoc:
        """Acquiring a new epoch preserves the prior committed snapshot (dispatch §2)."""
        return replace(
            self,
            writer_epoch=self.writer_epoch + 1,
            writer_instance_id=writer_instance_id,
            generation=self.generation + 1,
        )

    def with_committed_snapshot(self, epoch: int, writer_instance_id: str, entry: LatestEntry) -> RegistryDoc:
        return replace(
            self,
            writer_epoch=epoch,
            writer_instance_id=writer_instance_id,
            generation=self.generation + 1,
            latest=entry,
        )


def assert_lease_current(doc: RegistryDoc | None, lease: WriterLease) -> None:
    """Raise unless `doc` exists and was written by exactly this lease's epoch AND instance.

    A caller-constructed lease for a different instance, or a lease presented against a domain
    that was never acquired (no registry row), is rejected the same way a superseded epoch
    is — never adopted silently (review: "lease is not bound to its ref and instance").
    """
    from ach_runtime.storage.api import WriterFenced

    if doc is None:
        raise WriterFenced("no writer has ever been acquired for this domain")
    if doc.writer_epoch != lease.epoch:
        raise WriterFenced(
            f"writer epoch {lease.epoch} superseded by current epoch {doc.writer_epoch}"
        )
    if doc.writer_instance_id != lease.writer_instance_id:
        raise WriterFenced(
            f"lease writer {lease.writer_instance_id!r} does not match the registry's "
            f"current writer {doc.writer_instance_id!r}"
        )

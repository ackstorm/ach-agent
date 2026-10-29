# SPDX-License-Identifier: MIT
"""`StateRef`: the strict, immutable domain descriptor Storage operates on (dispatch §interfaces).

`workspace_ref`/`session_ref` are opaque, already-validated full SHA-256 hex digests minted
by `harness/refs.py` (Task3, not imported here — this is a bounded parallel split). Storage
never derives or splits them; it only confines every operation to the server-validated
agent identity, domain and ref carried on this object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from ach_runtime.config.schema import AgentIdentity

_HEX64 = re.compile(r"^[0-9a-f]{64}$")

DOMAIN_WORKSPACE = "workspace"
DOMAIN_SESSION = "session"


@dataclass(frozen=True, slots=True)
class FrozenAgentIdentity:
    """A storage-owned, genuinely immutable snapshot of `config.schema.AgentIdentity`.

    `AgentIdentity` is a plain (non-frozen) pydantic model — `ref.agent.uid = "x"` would
    silently mutate it even though the outer `StateRef` is frozen, because pydantic's
    `frozen=True` only blocks reassigning fields ON that model, not fields of a nested
    mutable object it holds (review: "the frozen outer StateRef contains a mutable
    AgentIdentity"). Storage never holds onto the shared mutable instance past validation.
    """

    name: str
    namespace: str
    uid: str


class StateRef(BaseModel):
    """Agent identity + domain + opaque refs. No arbitrary bucket/key/prefix ever crosses
    this boundary — `Storage` derives every domain key from these fields alone."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: FrozenAgentIdentity
    workspace_ref: str
    domain: str
    session_ref: str | None = None

    @field_validator("agent", mode="before")
    @classmethod
    def _freeze_identity(cls, v: object) -> FrozenAgentIdentity:
        """Every accepted input shape — a live `AgentIdentity`, a `FrozenAgentIdentity`
        already produced by a prior `StateRef`, or a raw dict — is revalidated through
        `AgentIdentity`'s own canonical-UUID constraint HERE, fresh, reading current field
        values. A bare `FrozenAgentIdentity` has no validation of its own (it's a plain
        dataclass), and a shared `AgentIdentity` instance can have its `.uid` mutated after
        the fact without pydantic re-validating (no `validate_assignment`) — so trusting
        either input's fields directly would let a malformed/mutated identity through
        (review: "New frozen identity input bypasses canonical identity validation")."""
        if isinstance(v, FrozenAgentIdentity):
            identity = AgentIdentity(name=v.name, namespace=v.namespace, uid=v.uid)
        elif isinstance(v, AgentIdentity):
            identity = AgentIdentity(name=v.name, namespace=v.namespace, uid=v.uid)
        else:
            identity = AgentIdentity.model_validate(v)
        return FrozenAgentIdentity(name=identity.name, namespace=identity.namespace, uid=identity.uid)

    @field_validator("workspace_ref", "session_ref")
    @classmethod
    def _hex64(cls, v: str | None) -> str | None:
        if v is not None and not _HEX64.match(v):
            raise ValueError(f"ref must be a 64-char lowercase SHA-256 hex digest: {v!r}")
        return v

    @field_validator("domain")
    @classmethod
    def _known_domain(cls, v: str) -> str:
        if v not in (DOMAIN_WORKSPACE, DOMAIN_SESSION):
            raise ValueError(f"domain must be {DOMAIN_WORKSPACE!r} or {DOMAIN_SESSION!r}: {v!r}")
        return v

    @model_validator(mode="after")
    def _session_ref_matches_domain(self) -> Self:
        if self.domain == DOMAIN_SESSION and self.session_ref is None:
            raise ValueError("session_ref is required for domain='session'")
        if self.domain == DOMAIN_WORKSPACE and self.session_ref is not None:
            raise ValueError("session_ref is forbidden for domain='workspace'")
        return self

    def _base_key(self, prefix: str) -> str:
        # namespace + uid: two namespaces must never collide on key even if a UID were ever
        # reused (review: "key derivation omits namespace").
        base = f"{prefix.strip('/')}/{self.agent.namespace}/{self.agent.uid}/{self.workspace_ref}"
        if self.domain == DOMAIN_SESSION:
            base = f"{base}/sessions/{self.session_ref}"
        return base

    def registry_key(self, prefix: str) -> str:
        """Key of the CAS registry pointer object for this domain (dispatch §behavior 3)."""
        return f"{self._base_key(prefix)}/registry.json"

    def snapshot_key(self, prefix: str, snapshot_id: str) -> str:
        """Key of the immutable snapshot blob — uploaded before the registry swap."""
        return f"{self._base_key(prefix)}/snapshots/{snapshot_id}.tar.gz"

    def commit_record_key(self, prefix: str, snapshot_id: str) -> str:
        """Key of the durable, immutable per-snapshot commit receipt — the minimum durable
        correlation needed to recover a historical snapshot's exact original receipt after
        `latest` has moved on (review: "Historical idempotent receipt recovery")."""
        return f"{self._base_key(prefix)}/commits/{snapshot_id}.json"

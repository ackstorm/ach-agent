# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ach_runtime.config.schema import AgentIdentity, ArtifactsBlock
from ach_runtime.facades.artifacts import ArtifactOperations
from ach_runtime.harness.tokens import (
    WorkspaceAuthenticationFailed,
    facade_token,
)
from ach_runtime.storage.api import (
    ArtifactDisabled,
    ArtifactProducer,
    ArtifactReceipt,
    StorageUnavailable,
)
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage
from tests.storage.fakes import FakeS3Client

KEY = b"artifact-facade-test-key"
WORKSPACE = "workspace-one"


def _owner(workspace_ref: str = "a" * 64) -> StateRef:
    return StateRef(
        agent=AgentIdentity(
            name="agent", namespace="agents", uid="11111111-1111-1111-1111-111111111111"
        ),
        workspace_ref=workspace_ref,
        domain="workspace",
    )


def _producer() -> ArtifactProducer:
    return ArtifactProducer(session_ref="b" * 64, invocation_id="c" * 64)


def _policy(enabled: bool = True) -> ArtifactsBlock:
    return ArtifactsBlock(enabled=enabled, maxArtifactBytes=4, retentionDays=90)


async def _owned(claim: str) -> bool:
    return claim == WORKSPACE


async def _chunks(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


class _RecordingStorage:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def create_artifact(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls.append(("create", *args, kwargs))
        return ArtifactReceipt("d" * 64, f"artifact:{'d' * 64}", None)

    async def read_artifact(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        self.calls.append(("read", *args, kwargs))
        return object()


@pytest.mark.asyncio
async def test_auth_before_storage_rejects_forged_other_workspace_and_unowned() -> None:
    storage = _RecordingStorage()
    ops = ArtifactOperations(
        storage=storage, owner=_owner(), producer=_producer(), policy=_policy(),
        workspace_name=WORKSPACE, key=KEY, owned_workspace=_owned,
    )
    with pytest.raises(WorkspaceAuthenticationFailed):
        await ops.create_artifact("forged.token", "key", _chunks(b"data"), mime="text/plain", name="r.txt")
    with pytest.raises(WorkspaceAuthenticationFailed):
        await ops.create_artifact(facade_token(KEY, "other-workspace"), "key", _chunks(b"data"), mime="text/plain", name="r.txt")

    async def rejected(_: str) -> bool:
        return False

    denied = ArtifactOperations(
        storage=storage, owner=_owner(), producer=_producer(), policy=_policy(),
        workspace_name=WORKSPACE, key=KEY, owned_workspace=rejected,
    )
    with pytest.raises(WorkspaceAuthenticationFailed):
        await denied.read_artifact(facade_token(KEY, WORKSPACE), "d" * 64)
    assert storage.calls == []


@pytest.mark.asyncio
async def test_failed_authenticated_publication_emits_safe_anomaly(caplog) -> None:
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL

    class FailedStorage(_RecordingStorage):
        async def create_artifact(self, *args, **kwargs):
            raise RuntimeError("publication-secret")

    storage = FailedStorage()
    ops = ArtifactOperations(
        storage=storage, owner=_owner(), producer=_producer(), policy=_policy(),
        workspace_name=WORKSPACE, key=KEY, owned_workspace=_owned,
    )
    before = ANOMALIES_TOTAL.labels(kind="artifact_publication_failed")._value.get()
    with pytest.raises(RuntimeError, match="publication-secret"):
        await ops.create_artifact(facade_token(KEY, WORKSPACE), "key", _chunks(b"data"),
                                  mime="text/plain", name="r.txt")
    after = ANOMALIES_TOTAL.labels(kind="artifact_publication_failed")._value.get()
    assert after == before + 1
    assert "publication-secret" not in caplog.text


@pytest.mark.asyncio
async def test_enablement_and_missing_backend_fail_before_storage_calls() -> None:
    storage = _RecordingStorage()
    disabled = ArtifactOperations(
        storage=storage, owner=_owner(), producer=_producer(), policy=_policy(False),
        workspace_name=WORKSPACE, key=KEY, owned_workspace=_owned,
    )
    token = facade_token(KEY, WORKSPACE)
    with pytest.raises(ArtifactDisabled):
        await disabled.create_artifact(token, "key", _chunks(b"data"), mime="text/plain", name="r.txt")
    with pytest.raises(ArtifactDisabled):
        await disabled.read_artifact(token, "d" * 64)
    missing = ArtifactOperations(
        storage=None, owner=_owner(), producer=_producer(), policy=_policy(),
        workspace_name=WORKSPACE, key=KEY, owned_workspace=_owned,
    )
    with pytest.raises(StorageUnavailable):
        await missing.read_artifact(token, "d" * 64)
    assert storage.calls == []


@pytest.mark.asyncio
async def test_bound_context_is_snapshotted_and_arguments_cannot_override_it() -> None:
    storage = _RecordingStorage()
    owner = _owner()
    producer = _producer()
    policy = _policy()
    ops = ArtifactOperations(
        storage=storage, owner=owner, producer=producer, policy=policy,
        workspace_name=WORKSPACE, key=KEY, owned_workspace=_owned,
    )
    policy.enabled = False
    token = facade_token(KEY, WORKSPACE)
    await ops.create_artifact(token, "stable-key", _chunks(b"data"), mime="text/plain", name="r.txt")
    call = storage.calls[0]
    assert call[1:4] == (owner, producer, "stable-key")
    assert call[5]["policy"].enabled is True
    with pytest.raises(TypeError):
        await ops.create_artifact(token, "key", _chunks(b"data"), mime="text/plain", name="r.txt", owner=_owner("e" * 64))
    with pytest.raises(TypeError):
        await ops.read_artifact(token, "d" * 64, bucket="attacker")


@pytest.mark.asyncio
async def test_bound_component_creates_and_reads_logical_artifact() -> None:
    storage = S3Storage(bucket="bucket", prefix="ach", client=FakeS3Client())
    create = ArtifactOperations(
        storage=storage, owner=_owner(), producer=_producer(), policy=_policy(),
        workspace_name=WORKSPACE, key=KEY, owned_workspace=_owned,
    )
    receipt = await create.create_artifact(
        facade_token(KEY, WORKSPACE), "stable-key", _chunks(b"data"),
        mime="text/plain", name="report.txt",
    )
    assert receipt.reference == f"artifact:{receipt.artifact_id}"
    reopened = ArtifactOperations(
        storage=storage, owner=_owner(), producer=_producer(), policy=_policy(),
        workspace_name=WORKSPACE, key=KEY, owned_workspace=_owned,
    )
    read = await reopened.read_artifact(facade_token(KEY, WORKSPACE), receipt.artifact_id)
    assert read.metadata == receipt.metadata
    assert b"".join([part async for part in read.open()]) == b"data"

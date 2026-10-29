# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError

from ach_runtime.config.schema import AgentIdentity, ArtifactsBlock
from ach_runtime.storage.api import ArtifactConflict, ArtifactNotFound
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage

_ENDPOINT = os.environ.get("ACH_STORAGE_TEST_S3_ENDPOINT")
_BUCKET = os.environ.get("ACH_STORAGE_TEST_S3_BUCKET", "ach-storage-test")
_ACCESS_KEY = os.environ.get("ACH_STORAGE_TEST_S3_ACCESS_KEY", "ach-storage-test")
_SECRET_KEY = os.environ.get("ACH_STORAGE_TEST_S3_SECRET_KEY", "ach-storage-test-secret")

pytestmark = pytest.mark.skipif(not _ENDPOINT, reason="real S3 endpoint was not supplied")


async def _bytes(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


def _owner() -> StateRef:
    return StateRef(
        agent=AgentIdentity(name="a", namespace="agents", uid="11111111-1111-1111-1111-111111111111"),
        workspace_ref="a" * 64,
        domain="workspace",
    )


def _producer():
    from ach_runtime.storage.api import ArtifactProducer

    return ArtifactProducer(session_ref="b" * 64, invocation_id="c" * 64)


def _policy() -> ArtifactsBlock:
    return ArtifactsBlock(enabled=True, maxArtifactBytes=16, retentionDays=90)


@pytest.fixture(scope="module")
def client():  # noqa: ANN201
    return boto3.client(
        "s3", endpoint_url=_ENDPOINT, aws_access_key_id=_ACCESS_KEY,
        aws_secret_access_key=_SECRET_KEY, region_name="us-east-1",
        config=Config(signature_version="s3v4", retries={"max_attempts": 0}),
    )


def _storage(client, *, prefix: str | None = None):  # noqa: ANN001, ANN201
    return S3Storage(bucket=_BUCKET, prefix=prefix or f"it-artifacts-{uuid.uuid4().hex}", client=client)


def test_real_seaweed_conditional_put_and_durable_retry(client) -> None:  # noqa: ANN001
    key = f"it-artifacts-negative-{uuid.uuid4().hex}/object"
    client.put_object(Bucket=_BUCKET, Key=key, Body=b"v1", IfNoneMatch="*")
    with pytest.raises(ClientError) as error:
        client.put_object(Bucket=_BUCKET, Key=key, Body=b"v2", IfNoneMatch="*")
    assert error.value.response["Error"]["Code"] in {"PreconditionFailed", "412"}


@pytest.mark.asyncio
async def test_real_seaweed_restart_and_different_byte_race(client) -> None:  # noqa: ANN001
    prefix = f"it-artifacts-{uuid.uuid4().hex}"
    first = _storage(client, prefix=prefix)
    owner, producer = _owner(), _producer()
    receipt = await first.create_artifact(
        owner, producer, "durable", _bytes(b"real"),
        mime="text/plain", name="real.txt", policy=_policy(),
    )
    retry = await _storage(client, prefix=prefix).create_artifact(
        owner, producer, "durable", _bytes(b"real"),
        mime="text/plain", name="real.txt", policy=_policy(),
    )
    assert retry == receipt
    reopened = await _storage(client, prefix=prefix).read_artifact(owner, receipt.artifact_id)
    assert b"".join([part async for part in reopened.open()]) == b"real"

    race_prefix = f"it-artifacts-race-{uuid.uuid4().hex}"
    storage_a, storage_b = _storage(client, prefix=race_prefix), _storage(client, prefix=race_prefix)
    results = await asyncio.gather(
        storage_a.create_artifact(owner, producer, "race", _bytes(b"writer-a"),
                                  mime="text/plain", name="race.txt", policy=_policy()),
        storage_b.create_artifact(owner, producer, "race", _bytes(b"writer-b"),
                                  mime="text/plain", name="race.txt", policy=_policy()),
        return_exceptions=True,
    )
    receipts = [item for item in results if not isinstance(item, BaseException)]
    failures = [item for item in results if isinstance(item, BaseException)]
    assert len(receipts) == 1
    assert len(failures) == 1 and isinstance(failures[0], ArtifactConflict)
    read = await storage_a.read_artifact(owner, receipts[0].artifact_id)
    assert b"".join([part async for part in read.open()]) in {b"writer-a", b"writer-b"}


@pytest.mark.asyncio
async def test_real_seaweed_body_only_orphan_is_invisible_then_repaired(client) -> None:  # noqa: ANN001
    base = _storage(client)

    class FailManifest:
        def __getattr__(self, name):  # noqa: ANN204
            return getattr(client, name)

        def put_object(self, **kwargs):  # noqa: ANN003, ANN201
            if kwargs["Key"].endswith("/metadata.json"):
                raise RuntimeError("injected manifest failure")
            return client.put_object(**kwargs)

    owner, producer = _owner(), _producer()
    failed = S3Storage(bucket=_BUCKET, prefix=base._prefix, client=FailManifest())
    with pytest.raises(RuntimeError, match="injected"):
        await failed.create_artifact(
            owner, producer, "orphan", _bytes(b"real"),
            mime="text/plain", name="orphan.txt", policy=_policy(),
        )
    artifact_id = __import__("hashlib").sha256(
        __import__("json").dumps(["artifact-v1", "agents", owner.agent.uid,
                                  owner.workspace_ref, "orphan"], separators=(",", ":")).encode()
    ).hexdigest()
    with pytest.raises(ArtifactNotFound):
        await base.read_artifact(owner, artifact_id)
    repaired = await base.create_artifact(
        owner, producer, "orphan", _bytes(b"real"),
        mime="text/plain", name="orphan.txt", policy=_policy(),
    )
    assert repaired.artifact_id == artifact_id
    assert b"".join([part async for part in (await base.read_artifact(owner, artifact_id)).open()]) == b"real"

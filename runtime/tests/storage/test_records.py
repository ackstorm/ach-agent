from __future__ import annotations

import boto3
import pytest
from botocore.stub import Stubber
from fakes import FakeS3Client

from ach_runtime.storage.api import StorageUnavailable
from ach_runtime.storage.s3 import S3Storage


async def test_records_round_trip_list_and_delete() -> None:
    client = FakeS3Client()
    client.page_size = 1
    storage = S3Storage(bucket="b", prefix="p", client=client)
    await storage.put_record("a2a/x/1", b"one")
    await storage.put_record("a2a/x/2", b"two")
    await storage.put_record("a2a/y/3", b"three")
    assert await storage.get_record("a2a/x/1") == b"one"
    assert await storage.get_record("missing") is None
    assert sorted(name for name, _ in await storage.list_records("a2a/x/")) == ["a2a/x/1", "a2a/x/2"]
    await storage.delete_record("a2a/x/1")
    assert await storage.get_record("a2a/x/1") is None
    assert [name for name, _ in await storage.list_records("a2a/x/")] == ["a2a/x/2"]


async def test_records_fail_closed_without_a_backend() -> None:
    storage = S3Storage(bucket=None, prefix="p")
    for call in (
        storage.put_record("n", b"x"), storage.get_record("n"),
        storage.list_records("n"), storage.delete_record("n"),
    ):
        with pytest.raises(StorageUnavailable):
            await call


async def test_list_records_sends_requests_s3_accepts() -> None:
    client = boto3.client("s3", region_name="eu-west-1")
    with Stubber(client) as stub:  # validates every parameter as botocore does before sending
        stub.add_response(
            "list_objects_v2",
            {"Contents": [], "IsTruncated": True, "NextContinuationToken": "t2"},
            {"Bucket": "b", "Prefix": "p/records/a2a/"},
        )
        stub.add_response(
            "list_objects_v2",
            {"Contents": [], "IsTruncated": False},
            {"Bucket": "b", "Prefix": "p/records/a2a/", "ContinuationToken": "t2"},
        )
        assert await S3Storage(bucket="b", prefix="p", client=client).list_records("a2a/") == []
        stub.assert_no_pending_responses()

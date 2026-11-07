from __future__ import annotations

import pytest
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

# SPDX-License-Identifier: MIT
"""In-memory S3-compatible fake: conditional PUT semantics only, no network.

Mirrors `old-ach-agent/tests/sandbox/test_store.py`'s `FakeS3` shape; extended with
`IfMatch`/`IfNoneMatch` so the CAS registry logic in `s3.py` can be exercised without a
real backend. The real-backend conditions are proven separately in
`test_s3_integration.py` against MinIO.
"""

from __future__ import annotations

import hashlib
import io
import threading
from dataclasses import dataclass


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


@dataclass
class _Object:
    body: bytes
    etag: str
    metadata: dict[str, str]


def _etag(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _read_body(body: object) -> bytes:
    return body.read() if hasattr(body, "read") else body  # type: ignore[union-attr,return-value]


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, _Object] = {}
        # `asyncio.to_thread` genuinely runs on separate OS threads, so two concurrent
        # `persist()` calls DO race on this dict — a real S3-compatible backend makes the
        # check-and-set atomic server-side; this lock makes the fake faithfully atomic too,
        # rather than letting a Python-level race double-accept a conditional write that a
        # real backend would correctly split into one winner and one PreconditionFailed.
        self._lock = threading.Lock()

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: object,
        IfMatch: str | None = None,
        IfNoneMatch: str | None = None,
        Metadata: dict[str, str] | None = None,
    ) -> dict[str, str]:
        payload = _read_body(Body)
        etag = _etag(payload)
        with self._lock:
            current = self.objects.get(Key)
            if IfNoneMatch == "*" and current is not None:
                raise _ClientError("PreconditionFailed")
            if IfMatch is not None and (current is None or current.etag != IfMatch):
                raise _ClientError("PreconditionFailed")
            self.objects[Key] = _Object(body=payload, etag=etag, metadata=dict(Metadata or {}))
        return {"ETag": etag}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        obj = self.objects.get(Key)
        if obj is None:
            raise _ClientError("NoSuchKey")
        return {"Body": io.BytesIO(obj.body), "ETag": obj.etag, "Metadata": dict(obj.metadata)}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        obj = self.objects.get(Key)
        if obj is None:
            raise _ClientError("404")
        return {"ETag": obj.etag, "ContentLength": len(obj.body), "Metadata": dict(obj.metadata)}

    def delete_object_for_test(self, key: str) -> None:
        """Test-only helper to simulate a registry pointing at missing bytes."""
        self.objects.pop(key, None)

    def corrupt_for_test(self, key: str, body: bytes) -> None:
        """Test-only helper: overwrite bytes without updating the recorded ETag semantics."""
        existing = self.objects[key]
        self.objects[key] = _Object(body=body, etag=existing.etag, metadata=existing.metadata)

    def put_raw_for_test(self, key: str, body: bytes) -> None:
        """Test-only helper: inject an object bypassing conditional-write checks entirely,
        used to simulate a corrupt registry document."""
        self.objects[key] = _Object(body=body, etag=_etag(body), metadata={})

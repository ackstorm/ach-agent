# SPDX-License-Identifier: Apache-2.0
"""Bot-session HOME archives: harness PVC cache in front of S3.

PVC first, S3 second: a pushed archive is safe on the PVC before the claim is deleted, and a
harness restart mid-upload finds it still marked pending and uploads it on the next sweep.
Keyed by the session digest (sandbox.tokens.digest). Only the harness holds S3 access (Pod
Identity); the sandbox never sees this module.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_PENDING = ".pending"


def _not_found(exc: BaseException) -> bool:
    code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
    return code in {"404", "NoSuchKey", "NotFound"}


class SessionStore:
    def __init__(
        self,
        cache_dir: Path,
        *,
        bucket: str,
        prefix: str,
        cache_ttl_seconds: float,
        s3: Any | None = None,
    ) -> None:
        self._dir = cache_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._ttl = cache_ttl_seconds
        self._s3 = s3
        self._uploads: set[asyncio.Task[None]] = set()

    @property
    def cache_dir(self) -> Path:
        return self._dir

    def _client(self) -> Any:
        if self._s3 is None:
            import boto3  # Pod Identity via the default credential chain

            self._s3 = boto3.client("s3")
        return self._s3

    def object_key(self, digest: str) -> str:
        return f"{self._prefix}/{digest}.tar.gz"

    def cache_path(self, digest: str) -> Path:
        return self._dir / f"{digest}.tar.gz"

    async def fetch(self, digest: str) -> Path | None:
        """Cached archive, else S3 download into the cache, else None (new session)."""
        path = self.cache_path(digest)
        if path.exists():
            path.touch()
            return path
        part = path.with_suffix(".part")
        try:
            await asyncio.to_thread(
                self._client().download_file, self._bucket, self.object_key(digest), str(part)
            )
        except Exception as exc:
            part.unlink(missing_ok=True)
            if _not_found(exc):
                return None
            raise
        part.replace(path)
        return path

    async def commit(self, digest: str, archive: Path) -> None:
        """Move ``archive`` into the cache and upload it to S3 in the background."""
        path = self.cache_path(digest)
        archive.replace(path)
        Path(f"{path}{_PENDING}").touch()
        task = asyncio.create_task(self._upload(path, self.object_key(digest)))
        self._uploads.add(task)
        task.add_done_callback(self._uploads.discard)

    async def _upload(self, path: Path, key: str) -> None:
        try:
            await asyncio.to_thread(self._client().upload_file, str(path), self._bucket, key)
        except Exception as exc:  # noqa: BLE001 — retried by sweep(); the PVC copy is safe
            log.warning("session store: upload failed", key=key, error=str(exc))
            return
        Path(f"{path}{_PENDING}").unlink(missing_ok=True)

    async def wait_uploads(self) -> None:
        await asyncio.gather(*self._uploads, return_exceptions=True)

    async def sweep(self) -> None:
        """Retry pending uploads, then evict uploaded archives older than the TTL."""
        for marker in self._dir.glob(f"*.tar.gz{_PENDING}"):
            archive = Path(str(marker)[: -len(_PENDING)])
            if archive.exists():
                await self._upload(archive, f"{self._prefix}/{archive.name}")
            else:
                marker.unlink(missing_ok=True)
        cutoff = time.time() - self._ttl
        for archive in self._dir.glob("*.tar.gz"):
            if not Path(f"{archive}{_PENDING}").exists() and archive.stat().st_mtime < cutoff:
                archive.unlink(missing_ok=True)

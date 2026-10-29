# SPDX-License-Identifier: MIT
"""Authenticated, bounded archive transport to an observed mini-harness pod."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx

from ach_runtime.harness.workspaces import InstanceMismatch, WorkspaceEndpoint, instance_changed

_MAX_ARCHIVE_BYTES = 2 * 1024**3
_TRANSFER_TIMEOUT = 180.0
_CHUNK_SIZE = 64 * 1024


def _url(endpoint: WorkspaceEndpoint, path: str) -> str:
    if not path.startswith("/"):
        raise ValueError("archive path must be absolute")
    return endpoint.base_url.rstrip("/") + path


async def get_archive(
    client: httpx.AsyncClient, endpoint: WorkspaceEndpoint,
    headers: dict[str, str], path: str, *,
    on_mismatch: Callable[[WorkspaceEndpoint], None] | None = None,
) -> AsyncIterator[bytes]:
    """Yield a bounded archive only from the endpoint's observed instance. The header is
    checked at response start: an interrupted body is a transport error, not a mismatch."""
    async with asyncio.timeout(_TRANSFER_TIMEOUT):
        async with client.stream("GET", _url(endpoint, path), headers=headers) as response:
            if instance_changed(response, endpoint):
                if on_mismatch is not None:
                    on_mismatch(endpoint)
                raise InstanceMismatch()
            if response.headers.get("X-ACH-Instance-Id") is None:
                raise RuntimeError("mini-harness response carried no instance ID")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                try:
                    await response.aread()
                    body = response.json()
                    if (
                        isinstance(body, dict)
                        and isinstance(body.get("type"), str)
                        and isinstance(body.get("message"), str)
                    ):
                        error.args = (
                            f"archive export HTTP {response.status_code}: "
                            f"{body['type']}: {body['message']}",
                        )
                except Exception:
                    # Diagnostics are best-effort; keep the original status exception.
                    pass
                raise
            length = response.headers.get("Content-Length")
            if length is not None and int(length) > _MAX_ARCHIVE_BYTES:
                raise RuntimeError("archive exceeds transfer byte limit")
            total = 0
            async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                total += len(chunk)
                if total > _MAX_ARCHIVE_BYTES:
                    raise RuntimeError("archive exceeds transfer byte limit")
                if chunk:
                    yield chunk


async def put_archive(
    client: httpx.AsyncClient, endpoint: WorkspaceEndpoint,
    headers: dict[str, str], path: str, chunks: AsyncIterator[bytes], *,
    params: dict[str, str] | None = None,
    on_mismatch: Callable[[WorkspaceEndpoint], None] | None = None,
) -> dict[str, Any]:
    """Stream a bounded archive and require status plus observed instance confirmation."""
    total = 0

    async def bounded() -> AsyncIterator[bytes]:
        nonlocal total
        async for chunk in chunks:
            total += len(chunk)
            if total > _MAX_ARCHIVE_BYTES:
                raise RuntimeError("archive exceeds transfer byte limit")
            if chunk:
                yield chunk

    async with asyncio.timeout(_TRANSFER_TIMEOUT):
        response = await client.put(
            _url(endpoint, path), headers=headers, params=params, content=bounded()
        )
    if instance_changed(response, endpoint):
        if on_mismatch is not None:
            on_mismatch(endpoint)
        raise InstanceMismatch()
    if response.headers.get("X-ACH-Instance-Id") is None:
        raise RuntimeError("mini-harness response carried no instance ID")
    response.raise_for_status()
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("mini-harness returned invalid archive confirmation") from exc
    if not isinstance(body, dict):
        raise RuntimeError("mini-harness returned invalid archive confirmation")
    return body

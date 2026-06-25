from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx

from ach_agent.engine.hydrate import Context

_KINDS = ("skills", "prompts", "artifacts")


async def _get_bytes(url: str, ek: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as c:
        # ACH auth is the `x-ach-key` header, NOT `Authorization: Bearer` (the latter
        # returns 400 "malformed bearer key" — confirmed vs real ACH content endpoint).
        r = await c.get(url, headers={"x-ach-key": ek})
        r.raise_for_status()
        return r.content


def _safe_extract(members: list[tarfile.TarInfo], dest: Path) -> None:
    dest_resolved = dest.resolve()
    for member in members:
        target = (dest / member.name).resolve()
        if not target.is_relative_to(dest_resolved):
            raise ValueError(f"unsafe tar member escapes destination: {member.name!r}")


async def fetch_context(ctx: Context, ek: str, root: Path) -> None:
    for kind in _KINDS:
        items = getattr(ctx, kind)
        for item in items:
            data = await _get_bytes(item.download_url, ek)
            target_dir = root / kind / item.name
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                _safe_extract(tar.getmembers(), target_dir)
                target_dir.mkdir(parents=True, exist_ok=True)
                tar.extractall(target_dir, filter="data")

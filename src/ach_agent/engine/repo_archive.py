# SPDX-License-Identifier: Apache-2.0
"""GitLab repo-archive MCP resource client + local extraction (Task 3/4).

Reads the gitlab-mcp `gitlab://{project}/archive/{ref}[/{subpath}]` resource, authenticating
harness-side with the ek_ as `x-ach-key` (never seen by the agent), and returns the raw gzip
tar bytes. Extraction (Task 4) writes those bytes into a per-checkout dir under a tmp base.

The session (and the SDK's no-`headers=` quirk) lives in engine/mcp_session.py.
"""

from __future__ import annotations

import base64
import io
import shutil
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import quote

import structlog

from ach_agent.engine.mcp_session import mcp_session

log = structlog.get_logger(__name__)


def build_archive_uri(project: str, ref: str, subpath: str | None) -> str:
    """Build the gitlab-mcp archive resource URI.

    Numeric project + SHA ref need no encoding. subpath keeps its slashes (path separators)
    but other specials are percent-encoded (FastMCP URL-decodes captured params).
    """
    uri = f"gitlab://{project}/archive/{ref}"
    if subpath:
        uri = f"{uri}/{quote(subpath, safe='/')}"
    return uri


async def read_repo_archive(
    endpoint: str, ek: str, project: str, ref: str, subpath: str | None = None
) -> bytes:
    """Read the archive resource → decoded gzip tar bytes.

    RAISES on read error (over-cap / auth / GitLab 403/404) — no silent truncation.
    """
    uri = build_archive_uri(project, ref, subpath)
    async with mcp_session(endpoint, {"x-ach-key": ek}) as session:
        result = await session.read_resource(uri)
    blob = result.contents[0].blob  # BlobResourceContents.blob is base64 (application/gzip)
    return base64.b64decode(blob)


def extract_archive(data: bytes, tmp_base: str, project: str, ref: str) -> str:
    """Extract gzip tar `data` into a fresh mkdtemp dir under tmp_base; return the repo root.

    GitLab archives nest everything under one top dir; when that holds, the repo root is that
    single child (so callers land inside the tree). Uses filter="data" to block path traversal.
    """
    Path(tmp_base).mkdir(parents=True, exist_ok=True)
    dest = tempfile.mkdtemp(prefix=f"{project}-{ref[:12]}-", dir=tmp_base)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(dest, filter="data")
    children = [c for c in Path(dest).iterdir()]
    if len(children) == 1 and children[0].is_dir():
        return str(children[0])
    return dest


def sweep_stale(tmp_base: str, ttl_seconds: float, now: float) -> int:
    """rmtree every direct child of tmp_base older than ttl_seconds. Returns count removed."""
    base = Path(tmp_base)
    if not base.is_dir():
        return 0
    removed = 0
    for child in base.iterdir():
        try:
            if now - child.stat().st_mtime > ttl_seconds:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:  # noqa: PERF203 — child vanished mid-sweep; ignore
            continue
    if removed:
        log.info("repo checkout sweep", base=tmp_base, removed=removed)
    return removed

# SPDX-License-Identifier: Apache-2.0
"""GitLab repo-archive MCP resource client + local extraction (Task 3/4).

Reads the gitlab-mcp `gitlab://{project}/archive/{ref}[/{subpath}]` resource, authenticating
harness-side with the ek_ as `x-ach-key` (never seen by the agent), and returns the raw gzip
tar bytes. Extraction (Task 4) writes those bytes into a per-checkout dir under a tmp base.

SDK note: the installed `streamable_http_client` takes NO `headers=` kwarg — auth is injected
by pre-building an httpx client via `create_mcp_http_client(headers=...)` (mirrors
memory/hindsight.py:_hindsight_session).
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


def build_archive_uri(project: str, ref: str, subpath: str | None) -> str:
    """Build the gitlab-mcp archive resource URI.

    Numeric project + SHA ref need no encoding. subpath keeps its slashes (path separators)
    but other specials are percent-encoded (FastMCP URL-decodes captured params).
    """
    uri = f"gitlab://{project}/archive/{ref}"
    if subpath:
        uri = f"{uri}/{quote(subpath, safe='/')}"
    return uri


@asynccontextmanager
async def _archive_session(endpoint: str, ek: str):  # type: ignore[no-untyped-def]
    """Open a ClientSession to the gitlab-mcp endpoint with the ek as x-ach-key (harness-side)."""
    async with create_mcp_http_client(headers={"x-ach-key": ek}) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def read_repo_archive(
    endpoint: str, ek: str, project: str, ref: str, subpath: str | None = None
) -> bytes:
    """Read the archive resource → decoded gzip tar bytes. RAISES on read error (over-cap/auth/404)."""
    uri = build_archive_uri(project, ref, subpath)
    async with _archive_session(endpoint, ek) as session:
        result = await session.read_resource(uri)
    blob = result.contents[0].blob  # BlobResourceContents.blob is base64 (application/gzip)
    return base64.b64decode(blob)

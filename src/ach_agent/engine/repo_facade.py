# SPDX-License-Identifier: Apache-2.0
"""Harness-hosted repo-checkout MCP facade.

Exposes ONE agent-facing tool, `checkout_repo`, on 127.0.0.1. The handler reads the gitlab-mcp
archive resource (ek injected harness-side, never seen by the agent), extracts it under a tmp
base, and returns the local path. The archive blob never enters the model context — only the
path string does. Mirrors memory/facade.py's FastMCP + uvicorn lifecycle.

Cleanup (Option A): a TTL sweep runs before each checkout; stop() rmtrees the whole tmp base at
harness shutdown. The shared facade cannot attribute a call to a session_key, so there is no
exact session-close deletion — /tmp is ephemeral, wiped on pod restart.
"""

from __future__ import annotations

import shutil
import time
from typing import Annotated

import structlog
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ach_agent.engine.mcp_host import LocalMcpHost
from ach_agent.engine.repo_archive import extract_archive, read_repo_archive, sweep_stale

log = structlog.get_logger(__name__)


class RepoCheckoutFacade:
    """FastMCP server exposing `checkout_repo`; reads the gitlab archive resource → local path."""

    def __init__(
        self, endpoint: str, ek: str, tmp_base: str = "/tmp/gitlab", ttl_seconds: float = 3600.0
    ) -> None:
        self._endpoint = endpoint
        self._ek = ek  # closure-only, never logged; injected as x-ach-key upstream
        self._tmp_base = tmp_base
        self._ttl = ttl_seconds
        self._mcp = FastMCP("ach-repo")
        self._host = LocalMcpHost(self._mcp, "repo facade", "repo checkout facade started")
        self._register_tools()

    async def _checkout(self, project: str, ref: str, subpath: str | None) -> str:
        """Read + extract the archive; fail-soft to a short note (never raises)."""
        try:
            sweep_stale(self._tmp_base, self._ttl, time.time())
            data = await read_repo_archive(self._endpoint, self._ek, project, ref, subpath)
            path = extract_archive(data, self._tmp_base, project, ref)
            return (
                f"Checked out to {path} (read-only snapshot, no .git — no blame/log/history). "
                "Use rg/tests/build there."
            )
        except Exception as exc:  # noqa: BLE001 — observability never breaks a turn
            log.warning("checkout_repo failed", project=project, ref=ref, error=str(exc))
            return (
                f"Checkout failed: {exc}. Narrow with a subpath if the repo is large, "
                "or use the per-file gitlab read tools instead."
            )

    def _register_tools(self) -> None:
        @self._mcp.tool(
            name="checkout_repo",
            description=(
                "Copy a GitLab repo (or subtree) to a local directory so you can run "
                "ripgrep/tests/build over the whole tree instead of reading one file at a time. "
                "Returns the local path. Read-only SNAPSHOT: no .git, so no blame/log/history and "
                "no `git describe`. For big repos pass `subpath` to fetch only what you need."
            ),
        )
        async def checkout_repo(
            project: Annotated[str, Field(description="Numeric GitLab project id (e.g. '1234').")],
            ref: Annotated[str, Field(description="Commit SHA to check out (the MR head SHA).")],
            subpath: Annotated[
                str | None,
                Field(description="Optional subtree, e.g. 'src/app', to stay small."),
            ] = None,
        ) -> str:
            return await self._checkout(project, ref, subpath)

    async def start(self) -> str:
        """Bind the facade on an ephemeral localhost port; return its MCP URL."""
        return await self._host.start(tmp_base=self._tmp_base)

    async def stop(self) -> None:
        """Signal uvicorn to exit, await the task, and rmtree the tmp base (shutdown sweep)."""
        await self._host.stop()
        shutil.rmtree(self._tmp_base, ignore_errors=True)

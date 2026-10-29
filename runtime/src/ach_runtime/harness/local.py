# SPDX-License-Identifier: MIT
"""In-process Workspace source for the `agent` role (spec 8).

Each Workspace runs the real execution app in this process, behind a routing transport keyed
by host name. Single-unit topology only: not for production (contract v2 D5).
"""

from __future__ import annotations

import asyncio
import itertools
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.execution.boot import build_execution_app
from ach_runtime.harness.launch import bootstrap_json, launch_env
from ach_runtime.harness.workspaces import Placement, WorkspaceAssemblyUnavailable, WorkspaceConflict, WorkspaceRef


class LocalRoutingTransport(httpx.AsyncBaseTransport):
    """Dispatches by `request.url.host` to that Workspace's ASGI app."""

    def __init__(self) -> None:
        self._apps: dict[str, httpx.ASGITransport] = {}

    def register(self, host: str, app: Any) -> None:
        self._apps[host] = httpx.ASGITransport(app=app)

    def unregister(self, host: str) -> None:
        self._apps.pop(host, None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        transport = self._apps.get(request.url.host)
        if transport is None:
            raise httpx.ConnectError(f"no local execution unit for {request.url.host}", request=request)
        return await transport.handle_async_request(request)


class LocalSource:
    """`WorkspaceSource` whose units are execution apps living in this process."""

    def __init__(
        self, bootstrap: ExecutionBootstrap, *, verify_key: str, data_dir: Path, transport: LocalRoutingTransport
    ) -> None:
        self._bootstrap = bootstrap
        self._bootstrap_json = bootstrap_json(bootstrap)
        self._verify_key = verify_key
        self._root = data_dir / "workspaces"
        self._transport = transport
        self._units: dict[str, Any] = {}  # name -> lifespan context of the running app
        self._registrations = itertools.count(1)

    @staticmethod
    def _host(name: str) -> str:
        return f"{name}.local"

    async def _remove(self, path: Path) -> None:
        await asyncio.to_thread(shutil.rmtree, path, True)

    async def place(self, ref: WorkspaceRef, name: str, *, deadline: float) -> Placement:
        # ponytail: the unit starts in-process and immediately, so `deadline` cannot expire here.
        if name in self._units:
            raise WorkspaceConflict(f"{name}: the Workspace is already hosted")
        unit_dir = self._root / name
        await self._remove(unit_dir)  # local files are never trusted across instances
        try:
            workspace, sessions = unit_dir / "workspace", unit_dir / "sessions"
            workspace.mkdir(parents=True)
            sessions.mkdir()
            env = launch_env(self._bootstrap_json, self._verify_key, ref, name)
            app = build_execution_app(self._bootstrap, environ=env, workspace_dir=workspace, sessions_root=sessions)
            lifespan = app.router.lifespan_context(app)
            await lifespan.__aenter__()
        except BaseException as exc:
            await self._remove(unit_dir)
            if not isinstance(exc, Exception) or isinstance(exc, WorkspaceAssemblyUnavailable):
                raise  # cancellation and interpreter exits pass through
            # Spec 8: a startup failure is a probe failure; the manager frees the slot only for these.
            raise WorkspaceAssemblyUnavailable(f"{name}: execution unit failed to start: {exc}") from exc
        self._units[name] = lifespan
        self._transport.register(self._host(name), app)
        return Placement(name, f"http://{self._host(name)}", next(self._registrations))

    async def retire(self, name: str) -> None:
        lifespan = self._units.pop(name, None)
        self._transport.unregister(self._host(name))
        if lifespan is not None:
            try:
                await lifespan.__aexit__(None, None, None)  # stops engine processes
            finally:
                await self._remove(self._root / name)
        else:
            await self._remove(self._root / name)

    async def adopt(self) -> list[tuple[WorkspaceRef, Placement]]:
        # A restart never adopts local state; the next demand restores from storage.
        if self._root.is_dir():
            for child in self._root.iterdir():
                await self._remove(child)
        return []

    def observed(self, placement: Placement, instance_id: str) -> bool:
        return True

    def serving(self, placement: Placement, instance_id: str) -> bool:
        return True

    def on_lost(self, callback: Callable[[str, str | None], Awaitable[None]]) -> None:
        pass  # an in-process app does not disappear on its own

    def close(self) -> None:
        pass  # `Control.close()` calls this on every source; units are stopped by `aclose()`

    async def aclose(self) -> None:
        for name in list(self._units):
            await self.retire(name)

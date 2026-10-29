# SPDX-License-Identifier: MIT
"""Hosted Workspace source and the `/host/v1` API (contract v2 section 5).

The runtime publishes desired state; the host reconciles it and registers what it started.
Host messages are hints: the unit's own instance ID (learned by probing) is the truth.
"""

from __future__ import annotations

import asyncio
import hmac
import itertools
import json
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ach_runtime import __version__
from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.config.schema import ENDPOINT_PATTERN
from ach_runtime.harness.launch import bootstrap_json as _bootstrap_json_of, launch_env
from ach_runtime.harness.refs import workspace_name
from ach_runtime.harness.workspaces import (
    Placement,
    WorkspaceConflict,
    WorkspaceRef,
    WorkspaceUnhosted,
)
from ach_runtime.observability import alarm

_MAX_BODY = 1024 * 1024
_MAX_WAIT_SECONDS = 60
_LAUNCH_PORT = 8080
_LAUNCH_COMMAND = ["python", "-m", "ach_runtime", "execution"]


@dataclass
class Hosted:
    ref: WorkspaceRef
    name: str
    desired: Literal["running", "released"]
    launch: dict[str, Any]
    placement: Placement | None = None  # current registration; None is listed as "instance": null
    observed_instance: str | None = None  # set by observed()
    state: Literal["probing", "serving"] | None = None  # None until a registration


class HostedSource:
    """`WorkspaceSource` whose units are started by a host that long-polls `/host/v1`."""

    def __init__(self, bootstrap: ExecutionBootstrap, *, verify_key: str) -> None:
        self._agent = bootstrap.agent
        self._verify_key = verify_key
        self._bootstrap_json = _bootstrap_json_of(bootstrap)
        self.epoch = uuid.uuid4().hex
        self.revision = 0
        self.adopting = True
        self.stopping = False
        self._entries: dict[str, Hosted] = {}
        self._inventory: list[tuple[WorkspaceRef, Placement]] = []
        self._registrations = itertools.count(1)
        self._changed = asyncio.Event()
        self._lost: Callable[[str, str | None], Awaitable[None]] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- change notification (every mutation wakes every waiter) ------------------------------
    def _touch(self) -> None:
        self.revision += 1
        woken, self._changed = self._changed, asyncio.Event()
        woken.set()

    async def _wait(self, timeout: float | None) -> None:
        try:
            async with asyncio.timeout(timeout):
                await self._changed.wait()
        except TimeoutError:
            pass

    @property
    def cursor(self) -> str:
        return f"{self.epoch}.{self.revision}"

    # -- launch spec (contract 5.5): no secret, single-line, 8 KiB per value --------------------
    def _launch(self, ref: WorkspaceRef, name: str) -> dict[str, Any]:
        return {
            "runtimeRelease": __version__,
            "port": _LAUNCH_PORT,
            "command": list(_LAUNCH_COMMAND),
            "env": launch_env(self._bootstrap_json, self._verify_key, ref, name),
        }

    # -- WorkspaceSource ---------------------------------------------------------------------
    async def place(self, ref: WorkspaceRef, name: str, *, deadline: float) -> Placement:
        if name in self._entries:
            raise WorkspaceConflict(f"{name}: the Workspace is already hosted")
        entry = Hosted(ref, name, "running", self._launch(ref, name))
        self._entries[name] = entry
        self._touch()
        try:
            while entry.placement is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                await self._wait(remaining)
        except BaseException as exc:
            if entry.placement is None and self._entries.get(name) is entry:
                del self._entries[name]
                self._touch()
            if isinstance(exc, TimeoutError):
                alarm("workspace_unhosted", workspace_ref=ref.workspace_ref)
                raise WorkspaceUnhosted(f"{name}: no execution unit was registered in time") from None
            raise
        return entry.placement

    async def retire(self, name: str) -> None:
        entry = self._entries.get(name)
        if entry is None:
            return
        entry.desired = "released"
        self._touch()
        while self._entries.get(name) is entry:
            await self._wait(None)

    async def adopt(self) -> list[tuple[WorkspaceRef, Placement]]:
        while self.adopting:
            await self._wait(None)
        return list(self._inventory)

    def _current(self, placement: Placement) -> Hosted | None:
        entry = self._entries.get(placement.name)
        if entry is None or entry.desired != "running" or entry.placement != placement:
            return None
        return entry

    def observed(self, placement: Placement, instance_id: str) -> bool:
        entry = self._current(placement)
        if entry is None:
            return False
        entry.observed_instance = instance_id
        self._touch()
        return True

    def serving(self, placement: Placement, instance_id: str) -> bool:
        entry = self._current(placement)
        if entry is None:
            return False
        entry.state = "serving"
        self._touch()
        return True

    def on_lost(self, callback: Callable[[str, str | None], Awaitable[None]]) -> None:
        self._lost = callback

    def close(self) -> None:
        """Shutdown: answer every pending long-poll with the current state."""
        self.stopping = True
        self._touch()

    # -- host API operations (the routes below are thin wrappers) ------------------------------
    def snapshot(self) -> dict[str, Any]:
        workspaces = [] if self.adopting else [
            {
                "name": e.name,
                "workspaceRef": e.ref.workspace_ref,
                "desired": e.desired,
                "launch": e.launch,
                "instance": None if e.placement is None else {
                    "instanceId": e.observed_instance,
                    "baseUrl": e.placement.base_url,
                    "state": e.state,
                },
            }
            for e in self._entries.values()
        ]
        return {
            "cursor": self.cursor, "runtimeEpoch": self.epoch,
            "adopting": self.adopting, "workspaces": workspaces,
        }

    async def listing(self, after: str | None, wait_seconds: int) -> dict[str, Any] | None:
        if after == self.cursor and wait_seconds > 0 and not self.stopping:
            await self._wait(wait_seconds)
        return None if self.stopping else self.snapshot()  # None: stopping, the route answers 503

    def register(self, name: str, base_url: str) -> tuple[int, dict[str, str] | None]:
        entry = self._entries.get(name)
        if entry is None or entry.desired != "running":
            return 409, {"error": "not_desired"}
        if entry.placement is not None:
            if entry.placement.base_url == base_url:
                return 200, None
            return 409, {"error": "registered"}
        entry.placement = Placement(name, base_url, next(self._registrations))
        entry.state = "probing"
        self._touch()
        return 202, None

    def unregister(self, name: str, instance_id: str | None) -> None:
        entry = self._entries.get(name)
        if entry is None or entry.placement is None:
            return
        if entry.observed_instance is not None and instance_id != entry.observed_instance:
            return
        del self._entries[name]
        self._touch()
        if entry.desired == "running" and self._lost is not None:
            task = asyncio.ensure_future(self._lost(name, entry.observed_instance))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def accept_inventory(self, epoch: str, units: list[dict[str, str]]) -> int:
        if epoch != self.epoch:
            return 409
        if not self.adopting:
            return 200
        parsed: list[tuple[WorkspaceRef, Placement]] = []
        try:
            for unit in units:
                ref = WorkspaceRef(self._agent.namespace, self._agent.uid, unit["workspaceRef"])
                if unit["name"] != workspace_name(self._agent.name, ref.workspace_ref):
                    return 422
                parsed.append((ref, Placement(unit["name"], unit["baseUrl"], next(self._registrations))))
        except (ValueError, KeyError):
            return 422
        if len({p.name for _, p in parsed}) != len(parsed):
            return 422
        for ref, placement in parsed:
            self._entries[placement.name] = Hosted(
                ref, placement.name, "running", self._launch(ref, placement.name),
                placement=placement, state="probing",
            )
        self._inventory = parsed
        self.adopting = False
        self._touch()
        return 202


def create_host_app(source: HostedSource, token: str) -> Starlette:
    """The `/host/v1` routes (mounted at that prefix on the 8081 app). Messages and status
    codes are normative in contract v2 section 5.3."""
    expected = f"Bearer {token}".encode()

    def guarded(handler: Callable[[Request], Awaitable[Response]]) -> Callable[[Request], Awaitable[Response]]:
        async def run(request: Request) -> Response:
            given = request.headers.get("authorization", "").encode()
            if not hmac.compare_digest(given, expected):
                return Response(status_code=401)
            if source.stopping:
                return Response(status_code=503)
            return await handler(request)

        return run

    async def body(request: Request) -> dict[str, Any] | None:
        raw = await request.body()
        try:
            value = json.loads(raw) if len(raw) <= _MAX_BODY else None
        except ValueError:
            return None
        return value if isinstance(value, dict) else None

    def valid_url(value: Any) -> bool:
        return isinstance(value, str) and re.match(ENDPOINT_PATTERN, value) is not None

    async def list_workspaces(request: Request) -> Response:
        try:
            wait = min(_MAX_WAIT_SECONDS, max(0, int(request.query_params.get("waitSeconds", "0"))))
        except ValueError:
            return Response(status_code=422)
        snapshot = await source.listing(request.query_params.get("after"), wait)
        return Response(status_code=503) if snapshot is None else JSONResponse(snapshot)

    async def put_instance(request: Request) -> Response:
        payload = await body(request)
        if payload is None or not valid_url(payload.get("baseUrl")):
            return Response(status_code=422)
        status, error = source.register(request.path_params["name"], payload["baseUrl"])
        return JSONResponse(error, status_code=status) if error else Response(status_code=status)

    async def delete_instance(request: Request) -> Response:
        source.unregister(request.path_params["name"], request.query_params.get("instanceId"))
        return Response(status_code=204)

    async def put_inventory(request: Request) -> Response:
        payload = await body(request)
        units = None if payload is None else payload.get("units")
        if (
            payload is None or not isinstance(payload.get("runtimeEpoch"), str)
            or not isinstance(units, list)
            or not all(
                isinstance(u, dict) and isinstance(u.get("name"), str)
                and isinstance(u.get("workspaceRef"), str) and valid_url(u.get("baseUrl"))
                for u in units
            )
        ):
            return Response(status_code=422)
        return Response(status_code=source.accept_inventory(payload["runtimeEpoch"], units))

    return Starlette(routes=[
        Route("/workspaces", guarded(list_workspaces), methods=["GET"]),
        Route("/workspaces/{name}/instance", guarded(put_instance), methods=["PUT"]),
        Route("/workspaces/{name}/instance", guarded(delete_instance), methods=["DELETE"]),
        Route("/inventory", guarded(put_inventory), methods=["PUT"]),
    ])

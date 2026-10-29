"""A host written the way an operator would write one: it drives the real `/host/v1` routes
over `httpx.ASGITransport` with the real host token (contract v2 section 5.4).

One sequential loop (spec 11.4): send the inventory for the current epoch, then long-poll
`GET /workspaces` and reconcile. The test controls below default off.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from ach_runtime.harness.hosted import HostedSource, create_host_app

StartUnit = Callable[[str, dict[str, Any]], Awaitable[str]]
StopUnit = Callable[[str], Awaitable[None]]
HideUnit = Callable[[str], str]


@dataclass
class StartGate:
    """`hold_start()` result: `reached` is set when the host is about to start a unit,
    the host then waits for `release`."""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class FakeHost:
    def __init__(
        self,
        source: HostedSource,
        token: str,
        start_unit: StartUnit,
        stop_unit: StopUnit,
        *,
        hide_unit: HideUnit | None = None,
        dishonest: bool = False,
        units: dict[str, dict[str, str]] | None = None,
    ) -> None:
        self.source = source
        self.token = token
        self._start_unit = start_unit
        self._stop_unit = stop_unit
        self._hide_unit = hide_unit
        self.dishonest = dishonest
        # name -> {"workspaceRef", "baseUrl"}: the units this host runs. Shared with the next
        # host when a test restarts the control unit.
        self.units: dict[str, dict[str, str]] = units if units is not None else {}
        self.started_count = 0
        self.deleted: list[tuple[str, str | None]] = []
        self.zombies: dict[str, str] = {}
        self.listed: dict[str, dict[str, Any]] = {}
        self.first_listing: dict[str, Any] | None = None
        self.launches: dict[str, dict[str, Any]] = {}
        self._withheld_registration: set[str] = set()
        self._withheld_delete: set[str] = set()
        self._stopped: dict[str, str | None] = {}  # stopped, DELETE still owed
        self._gate: StartGate | None = None
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_host_app(source, token)),
            base_url="http://control", headers={"authorization": f"Bearer {token}"}, timeout=None,
        )

    # -- test controls ---------------------------------------------------------------------
    def withhold_registration(self, name: str) -> None:
        self._withheld_registration.add(name)

    def withhold_delete(self, name: str) -> None:
        self._withheld_delete.add(name)

    async def release_delete(self, name: str) -> None:
        self._withheld_delete.discard(name)
        if name in self._stopped:
            await self._delete(name, self._stopped.pop(name))

    def hold_start(self) -> StartGate:
        self._gate = StartGate()
        return self._gate

    async def restart_in_place(self, name: str) -> None:
        """Rebuild the unit behind the same base URL; the host is not told."""
        await self._start_unit(name, self.launches[name])

    async def kill(self, name: str, *, report: bool = True) -> None:
        """The unit dies. With `report`, the host tells the runtime (an unexpected loss)."""
        listing = await self.list_workspaces()
        instance = next((i["instance"] for i in listing["workspaces"] if i["name"] == name), None) or {}
        await self._stop_unit(name)
        self.units.pop(name, None)
        if report:
            await self._delete(name, instance.get("instanceId"))

    def zombie(self, name: str) -> str:
        """Host name under which a dishonest host kept the unit alive."""
        return self.zombies[name]

    # -- the loop --------------------------------------------------------------------------
    async def list_workspaces(self, after: str | None = None, wait: int = 0) -> dict[str, Any]:
        params: dict[str, Any] = {"waitSeconds": wait}
        if after is not None:
            params["after"] = after
        response = await self.client.get("/workspaces", params=params)
        response.raise_for_status()
        return response.json()

    async def run(self) -> None:
        snapshot = await self.list_workspaces()
        self.first_listing = snapshot
        if snapshot["adopting"]:
            await self.send_inventory(snapshot["runtimeEpoch"])
            snapshot = await self.list_workspaces()
        while True:
            if not snapshot["adopting"]:
                await self._reconcile(snapshot)
            snapshot = await self.list_workspaces(snapshot["cursor"], 1)

    async def send_inventory(self, epoch: str) -> httpx.Response:
        units = [{"name": name, **unit} for name, unit in self.units.items()]
        response = await self.client.put("/inventory", json={"runtimeEpoch": epoch, "units": units})
        assert response.status_code in {200, 202}, response.status_code
        return response

    async def _reconcile(self, snapshot: dict[str, Any]) -> None:
        listed = {item["name"]: item for item in snapshot["workspaces"]}
        self.listed = listed
        for name, item in listed.items():
            self.launches[name] = item["launch"]
            instance = item["instance"]
            if item["desired"] == "running":
                if name in self._withheld_registration or "*" in self._withheld_registration:
                    continue
                if name not in self.units:
                    if instance is not None:
                        continue  # killed without a report: the runtime recovers by itself
                    await self._start(name, item)
                unit = self.units[name]
                if instance is None or instance["baseUrl"] != unit["baseUrl"]:
                    response = await self.client.put(
                        f"/workspaces/{name}/instance", json={"baseUrl": unit["baseUrl"]}
                    )
                    if response.status_code == 409 and response.json() == {"error": "not_desired"}:
                        await self._stop(name)
            else:
                await self._release(name, instance)
        for name in list(self.units):
            if name not in listed:
                await self._stop(name)

    async def _start(self, name: str, item: dict[str, Any]) -> None:
        gate = self._gate
        if gate is not None:
            gate.reached.set()
            await gate.release.wait()
        base_url = await self._start_unit(name, item["launch"])
        self.started_count += 1
        self.units[name] = {"workspaceRef": item["workspaceRef"], "baseUrl": base_url}

    async def _stop(self, name: str) -> None:
        if self.dishonest and self._hide_unit is not None:
            self.zombies[name] = self._hide_unit(name)
        else:
            await self._stop_unit(name)
        self.units.pop(name, None)

    async def _release(self, name: str, instance: dict[str, Any] | None) -> None:
        instance_id = None if instance is None else instance["instanceId"]
        if name in self.units:
            await self._stop(name)
            self._stopped[name] = instance_id
        elif name not in self._stopped:
            self._stopped[name] = instance_id
        if name not in self._withheld_delete and name in self._stopped:
            await self._delete(name, self._stopped.pop(name))

    async def _delete(self, name: str, instance_id: str | None) -> httpx.Response:
        params = {} if instance_id is None else {"instanceId": instance_id}
        self.deleted.append((name, instance_id))
        response = await self.client.delete(f"/workspaces/{name}/instance", params=params)
        assert response.status_code == 204, response.status_code
        return response

    async def aclose(self) -> None:
        await self.client.aclose()

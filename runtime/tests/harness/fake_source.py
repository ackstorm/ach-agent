# SPDX-License-Identifier: MIT
"""In-memory `WorkspaceSource` for manager and delivery tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from ach_runtime.harness.workspaces import Placement, WorkspaceRef


class FakeSource:
    """`place` and `retire` run straight through unless the test holds `place_gate` /
    `retire_gate` (an `asyncio.Event` the test sets) or sets `place_error`."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.names: set[str] = set()  # units the source currently hosts
        self.released: set[str] = set()  # retire was requested (the entry is dropped)
        self.place_calls: list[str] = []
        self.retire_calls: list[str] = []
        self.place_gate: asyncio.Event | None = None
        self.retire_gate: asyncio.Event | None = None
        self.place_error: BaseException | None = None
        self.observed_ok = True
        self.serving_ok = True
        self.adopted: list[tuple[WorkspaceRef, Placement]] = []
        self.adopt_gate: asyncio.Event | None = None
        self.lost: Callable[[str, str | None], Awaitable[None]] | None = None
        self._registration = 0
        self.gateway = True

    def placement(self, name: str) -> Placement:
        self._registration += 1
        return Placement(name, f"http://{name}", self._registration)

    async def place(self, ref: WorkspaceRef, name: str, *, deadline: float) -> Placement:
        self.place_calls.append(name)
        self.events.append("place")
        if self.place_gate is not None:
            await self.place_gate.wait()
        if self.place_error is not None:
            raise self.place_error
        self.names.add(name)
        self.released.discard(name)
        return self.placement(name)

    async def retire(self, name: str) -> None:
        self.retire_calls.append(name)
        self.released.add(name)
        if self.retire_gate is not None:
            await self.retire_gate.wait()
        self.names.discard(name)

    async def adopt(self) -> list[tuple[WorkspaceRef, Placement]]:
        if self.adopt_gate is not None:
            await self.adopt_gate.wait()
        self.names.update(p.name for _, p in self.adopted)
        return list(self.adopted)

    def observed(self, placement: Placement, instance_id: str) -> bool:
        self.events.append("observed")
        return self.observed_ok

    def serving(self, placement: Placement, instance_id: str) -> bool:
        self.events.append("serving")
        return self.serving_ok

    def gateway_available(self) -> bool:
        return self.gateway

    def on_lost(self, callback: Callable[[str, str | None], Awaitable[None]]) -> None:
        self.lost = callback

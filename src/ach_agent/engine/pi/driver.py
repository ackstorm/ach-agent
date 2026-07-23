# SPDX-License-Identifier: Apache-2.0
"""PiDriver stub — satisfies EngineDriver so engine.type='pi' selection type-checks.
The real launch/run_turn/etc. land in SP1 Phase 8."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ach_agent.engine.base.driver import EngineConfig, TurnResult

if TYPE_CHECKING:
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer

_TODO = "Pi engine lands in SP1 Phase 8"


class PiDriver:
    engine_type = "pi"

    def skills_dir(self, home: Path) -> Path:
        return home / "pi" / "skills"

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer:
        raise NotImplementedError(_TODO)

    async def health(self, server: ManagedServer) -> bool:
        raise NotImplementedError(_TODO)

    async def run_turn(
        self,
        server: ManagedServer,
        *,
        conv_key: str,
        prompt: str,
        reuse: bool,
        sessions: MutableMapping[str, str],
        session_ref: str | None = None,
        on_text: Callable[[str], None] | None,
        on_tool: Callable[[OpenCodeToolUpdate], None] | None,
        max_tool_calls: int,
        stats: dict[str, Any],
    ) -> TurnResult:
        raise NotImplementedError(_TODO)

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None:
        raise NotImplementedError(_TODO)

    async def compact_session(self, server: ManagedServer, session_ref: str) -> None:
        raise NotImplementedError(_TODO)

    async def stop(self, server: ManagedServer) -> None:
        raise NotImplementedError(_TODO)

# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: EnginePool + session maps moved to engine/base/pool.py (SP1)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.base.pool import (  # noqa: F401
    EnginePool,
    _LRUSessionMap,
    _NamespacedSessionMap,
    _SqliteSessionMap,
)

if TYPE_CHECKING:
    from ach_agent.engine.lifecycle import ManagedServer

__all__ = [
    "EnginePool",
    "_LRUSessionMap",
    "_NamespacedSessionMap",
    "_SqliteSessionMap",
    "_default_start_server",
]


async def _default_start_server(config: EngineConfig, session_key: str) -> ManagedServer:
    """Back-compat wrapper for the former opencode-only launch helper."""
    from ach_agent.engine.opencode.driver import OpencodeDriver

    return await OpencodeDriver().launch(config, session_key)

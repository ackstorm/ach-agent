# SPDX-License-Identifier: Apache-2.0
"""Serialize native work that selects the same engine conversation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass


@dataclass
class _ConversationEntry:
    lock: asyncio.Lock
    references: int = 0


class ConversationLocks:
    """Reference-counted locks keyed by engine type and native conversation."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], _ConversationEntry] = {}

    @asynccontextmanager
    async def hold(self, engine_type: str, conversation_key: str | None) -> AsyncIterator[None]:
        """Hold a conversation lock, or bypass it for stateless work."""
        if conversation_key is None:
            yield
            return

        key = (engine_type, conversation_key)
        entry = self._entries.get(key)
        if entry is None:
            entry = _ConversationEntry(asyncio.Lock())
            self._entries[key] = entry
        entry.references += 1
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.references -= 1
            if entry.references == 0:
                self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)


__all__ = ["ConversationLocks"]

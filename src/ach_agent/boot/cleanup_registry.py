# SPDX-License-Identifier: Apache-2.0
"""Correlate workspace stop notifications with cleanup hooks and acknowledgements."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock
from ach_agent.execution.wire import WorkspaceStoppedEvent

log = structlog.get_logger(__name__)


class CleanupError(RuntimeError):
    """Raised when cleanup context registration or lifecycle operations fail."""


@dataclass(frozen=True, slots=True)
class _CleanupContext:
    invocation_id: str
    event: MessageEvent
    workspace: Path
    cfg: PrepareBlock


class CleanupRegistry:
    """Bounded cleanup contexts correlated with engine stop events.

    Contexts contain only workspace and hook data needed for ACK handling.
    """

    def __init__(self, *, max_contexts: int = 64) -> None:
        if max_contexts <= 0:
            raise ValueError("cleanup registry bounds must be positive")
        self._max_contexts = max_contexts
        self._contexts: dict[str, _CleanupContext] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._task_by_invocation: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    async def register(
        self,
        invocation_id: str,
        event: MessageEvent,
        workspace: Path,
        cfg: PrepareBlock,
    ) -> None:
        """Store hook context before the corresponding workspace reservation."""
        if self._closed:
            raise CleanupError("cleanup registry is closed")
        if invocation_id in self._contexts or invocation_id in self._task_by_invocation:
            raise CleanupError("cleanup context is already registered")
        if len(self._contexts) + len(self._tasks) >= self._max_contexts:
            raise CleanupError("cleanup context limit reached")
        self._contexts[invocation_id] = _CleanupContext(invocation_id, event, workspace, cfg)

    def commit(self, invocation_id: str) -> None:
        """Commit a successful prepare and retire superseded same-lane contexts."""
        context = self._contexts.get(invocation_id)
        if context is None:
            raise CleanupError("cleanup context is not pending")
        for previous, candidate in tuple(self._contexts.items()):
            if (
                previous != invocation_id
                and candidate.event.session_key == context.event.session_key
            ):
                self._contexts.pop(previous, None)

    def retire(self, invocation_id: str) -> None:
        """Remove an unused context after cancellation or failed admission."""
        self._contexts.pop(invocation_id, None)

    @staticmethod
    def _observe_task(task: asyncio.Task[None]) -> None:
        try:
            task.result()
        except BaseException as exc:
            log.warning("cleanup: callback failed", error=str(exc))

    async def handle_event(
        self,
        event: WorkspaceStoppedEvent,
        acknowledge: Callable[[WorkspaceStoppedEvent], Awaitable[None]],
    ) -> bool:
        """Run one matching cleanup and ACK it; return false for stale events."""
        if self._closed:
            return False
        context = self._contexts.get(event.invocation_id)
        if (
            context is None
            or context.event.idempotency_key != event.event_id
            or context.event.session_key != event.session_key
        ):
            return False
        self._contexts.pop(event.invocation_id, None)
        task = asyncio.create_task(self._cleanup_and_ack(context, event, acknowledge))
        self._tasks.add(task)
        self._task_by_invocation[event.invocation_id] = task
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda _: self._task_by_invocation.pop(event.invocation_id, None))
        task.add_done_callback(self._observe_task)
        return True

    async def _cleanup_and_ack(
        self,
        context: _CleanupContext,
        event: WorkspaceStoppedEvent,
        acknowledge: Callable[[WorkspaceStoppedEvent], Awaitable[None]],
    ) -> None:
        try:
            from ach_agent.boot.prepare import run_cleanup

            await run_cleanup(context.cfg, context.event, context.workspace)
        except Exception as exc:  # noqa: BLE001
            log.warning("cleanup: hook failed", invocation_id=event.invocation_id, error=str(exc))
        finally:
            await acknowledge(event)

    async def close(self) -> None:
        """Retire pending contexts and cancel owned cleanup tasks."""
        self._closed = True
        self._contexts.clear()
        tasks = tuple(self._tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._task_by_invocation.clear()

# SPDX-License-Identifier: Apache-2.0
"""Minimal shared-server pool for engine.shared.enabled mode (ENG-08).

Single server, ref-count, one TTL task. See D-07 for scope boundary.


Lifecycle modes:
  - spawn-per-invocation (shared.enabled=false): acquire starts a new server,
    release(ttl_seconds=0) stops it immediately after each invocation.
  - shared (shared.enabled=true): acquire reuses the alive server; release with
    ttlSeconds schedules the stop after idle TTL elapses; a re-acquire cancels
    the pending TTL task.

Constraint: No router or Hermes imports (D-08, RTR-06).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ach_agent.engine.lifecycle import EngineConfig, ManagedServer

log = structlog.get_logger(__name__)


class EnginePool:
    """Minimal shared-server pool.

    For engine.shared.enabled=false: create ManagedServer per invocation, stop after (TTL=0).
    For engine.shared.enabled=true: reuse alive server across invocations with TTL idle expiry.

    Thread-safety: all operations are protected by an asyncio.Lock.
    D-07 scope: one server only. Multi-server features are out of scope for Phase 0.
    """

    def __init__(self) -> None:
        self._server: ManagedServer | None = None
        self._ref_count: int = 0
        self._ttl_task: asyncio.Task[None] | None = None
        self._lock: asyncio.Lock = asyncio.Lock()

        # A′ proven-start latch (DUR-02, D-08-latch).
        # Set True the first time acquire() completes _start_server successfully.
        # Never reset — gate is first-warmup-only (D-07).
        self.engine_has_been_ready_once: bool = False

        # _start_server is injectable for testing (replaced by tests with a fake)
        # In production it points to the lifecycle launch helper.
        self._start_server: Callable[..., Awaitable[ManagedServer]] = _default_start_server

    async def acquire(self, config: EngineConfig) -> ManagedServer:
        """Acquire a ManagedServer, reusing alive server or starting a new one.

        Under the lock:
          1. Cancel any pending TTL expiry task.
          2. If server exists and is alive, increment ref_count and return it (reuse).
          3. Otherwise start a new server and set ref_count=1.
        """
        async with self._lock:
            # Cancel pending TTL task (if any) — a new acquire voids the expiry
            if self._ttl_task is not None and not self._ttl_task.done():
                self._ttl_task.cancel()
                self._ttl_task = None

            if self._server is not None and self._server.is_alive():
                # Reuse existing server
                self._ref_count += 1
                log.debug(
                    "EnginePool.acquire: reusing alive server",
                    ref_count=self._ref_count,
                )
                return self._server

            # Start a new server (or replace a dead one)
            log.info("EnginePool.acquire: starting new server")
            self._server = await self._start_server(config)
            # A′ latch: _start_server only returns on success (poll_ready calls sys.exit(1)
            # on failure), so reaching here proves the engine was ready at least once.
            self.engine_has_been_ready_once = True
            self._ref_count = 1
            return self._server

    async def release(self, ttl_seconds: float) -> None:
        """Release a server reference; stop immediately if ttl_seconds==0 or schedule expiry.

        Under the lock:
          1. Decrement ref_count (floor 0).
          2. If ref_count > 0 — other callers still hold the server; return.
          3. If ttl_seconds == 0 — stop immediately (spawn-per-invocation).
          4. Else — schedule _expire(ttl_seconds) task.
        """
        async with self._lock:
            self._ref_count = max(0, self._ref_count - 1)
            if self._ref_count > 0:
                log.debug(
                    "EnginePool.release: still in use",
                    ref_count=self._ref_count,
                )
                return

        # ref_count == 0 below (lock released to allow stop to take it)
        if ttl_seconds == 0:
            await self._stop()
        else:
            log.debug("EnginePool.release: scheduling TTL expiry", ttl_seconds=ttl_seconds)
            self._ttl_task = asyncio.create_task(self._expire(ttl_seconds))

    async def _expire(self, ttl: float) -> None:
        """Sleep for ttl seconds then stop the server."""
        try:
            await asyncio.sleep(ttl)
        except asyncio.CancelledError:
            log.debug("EnginePool._expire: TTL task cancelled")
            return
        log.info("EnginePool._expire: TTL elapsed — stopping server", ttl=ttl)
        await self._stop()

    async def _stop(self) -> None:
        """Stop the held server under lock and clear pool state."""
        async with self._lock:
            server = self._server
            if server is None:
                return
            self._server = None
            self._ref_count = 0

        # Stop outside the lock so it doesn't block acquire
        try:
            await server.stop()
            log.info("EnginePool._stop: server stopped")
        except Exception:  # noqa: BLE001
            log.warning("EnginePool._stop: error stopping server", exc_info=True)


async def _default_start_server(config: EngineConfig) -> ManagedServer:
    """Default start-server implementation: full lifecycle launch + poll_ready.

    HOME is the stable, shared ``config.home`` (created if absent) — opencode's config,
    hydrated skills, sessions, and node_modules live there and persist across servers.
    Used in production. Tests replace pool._start_server with a fake.
    """
    from pathlib import Path

    from ach_agent.engine.client import find_free_port
    from ach_agent.engine.lifecycle import launch, poll_ready

    home = Path(config.home)
    home.mkdir(parents=True, exist_ok=True)
    # opencode serve binds loopback on a free ephemeral port the harness picks (so it knows
    # the port for its client + `opencode attach`); never published off-host.
    port = find_free_port()
    server = await launch(port, home, config)
    await poll_ready(server, config.startup_timeout_seconds)
    return server

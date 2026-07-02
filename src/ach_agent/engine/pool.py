# SPDX-License-Identifier: Apache-2.0
"""Keyed multi-server pool for EnginePool (ENG-08).

Keyed by session_key: each session identity (cron name, gitlab server+repo,
tui-console) gets its own ManagedServer. See D-07 for scope boundary.

All servers share ONE home (``engine.home``). Per-key isolation is the
``opencode_<key>.json`` config file (written via ``OPENCODE_CONFIG`` by
``launch``), so concurrent servers for distinct keys never race the same
config file (I-1). The session store and node_modules cache under the shared
home are intentionally shared across concurrent keyed processes — low risk at
v1; isolate ``XDG_DATA_HOME`` per key later if needed.

Lifecycle modes:
  - spawn-per-invocation (shared.enabled=false): acquire starts a new server for
    the session_key, release(key, ttl_seconds=0) stops it immediately after each
    invocation.
  - shared (shared.enabled=true): acquire reuses the alive server for that key;
    release with ttlSeconds schedules the stop after idle TTL elapses; a
    re-acquire for the same key cancels the pending TTL task.

Constraint: No router or Hermes imports (D-08, RTR-06).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from ach_agent.engine.lifecycle import EngineConfig, ManagedServer

log = structlog.get_logger(__name__)


class EnginePool:
    """Pool of opencode servers keyed by session_key.

    Each session_key (cron name, gitlab server+repo, tui-console) maps to its
    own ManagedServer with its own port, all sharing ONE home (``config.home``).
    Per-key isolation is the ``opencode_<key>.json`` config file selected via
    ``OPENCODE_CONFIG`` — so concurrent servers for distinct keys never race the
    same config file (I-1). The session store and node_modules cache are shared
    across keyed processes (v1 tradeoff; isolate ``XDG_DATA_HOME`` per key later
    if needed).

    For ttl_seconds == 0 (spawn-per-invocation, v1 default): the key's server is
    stopped as soon as its last holder releases. For ttl_seconds > 0: the server
    is kept warm for that key until the idle TTL elapses; a re-acquire cancels
    the pending expiry. Reference counting per key ensures a key's TTL only
    starts once no invocation holds it.

    Constraint: No router or Hermes imports (D-08, RTR-06).
    """

    def __init__(self) -> None:
        self._servers: dict[str, ManagedServer] = {}
        self._ref_counts: dict[str, int] = {}
        self._ttl_tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

        # _start_server is injectable for testing (replaced by tests with a fake).
        # In production it points to the lifecycle launch helper. It receives the
        # EngineConfig and session_key.
        self._start_server: Callable[[EngineConfig, str], Awaitable[ManagedServer]] = (
            _default_start_server
        )

    def _get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the per-key lock, creating it on first use.

        Safe under asyncio: no await between the membership check and the
        insertion, so the create-and-store is atomic for the single-thread loop.
        """
        lock = self._locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_key] = lock
        return lock

    async def acquire(self, session_key: str, config: EngineConfig) -> ManagedServer:
        """Acquire the server for session_key, reusing an alive one or starting new.

        Under the key's lock:
          1. Cancel any pending TTL expiry for this key.
          2. If a live server exists for this key, increment its ref-count and reuse.
          3. Otherwise (missing or dead) start a new server and set ref-count = 1.
        """
        lock = self._get_lock(session_key)
        async with lock:
            ttl_task = self._ttl_tasks.pop(session_key, None)
            if ttl_task is not None and not ttl_task.done():
                ttl_task.cancel()

            existing = self._servers.get(session_key)
            if existing is not None and existing.is_alive():
                self._ref_counts[session_key] = self._ref_counts.get(session_key, 0) + 1
                log.debug(
                    "EnginePool.acquire: reusing alive server",
                    session_key=session_key,
                    ref_count=self._ref_counts[session_key],
                )
                return existing

            if existing is not None:
                # Dead server — stop and replace.
                log.warning("EnginePool.acquire: server dead, replacing", session_key=session_key)
                try:
                    await existing.stop()
                except Exception:  # noqa: BLE001
                    log.debug("EnginePool.acquire: dead-server stop failed", exc_info=True)
                self._servers.pop(session_key, None)
                self._ref_counts.pop(session_key, None)

            log.info("EnginePool.acquire: starting new server", session_key=session_key)
            server = await self._start_server(config, session_key)
            self._servers[session_key] = server
            self._ref_counts[session_key] = 1
            return server

    async def release(self, session_key: str, ttl_seconds: float) -> None:
        """Release one reference to session_key's server.

        Under the key's lock:
          1. Decrement the key's ref-count (floor 0).
          2. If still referenced — return (another holder is active).
        Then, with no holders:
          3. ttl_seconds == 0 — stop the key's server immediately.
          4. ttl_seconds  > 0 — schedule _expire(session_key, ttl_seconds).
        """
        lock = self._get_lock(session_key)
        async with lock:
            count = self._ref_counts.get(session_key, 0)
            if count == 0:
                # Spurious release (double-release or release of a never-acquired
                # key). No server is tracked for this key — nothing to stop.
                log.warning(
                    "EnginePool.release: no active ref for key (spurious release)",
                    session_key=session_key,
                )
                return
            if count > 1:
                self._ref_counts[session_key] = count - 1
                log.debug(
                    "EnginePool.release: still in use",
                    session_key=session_key,
                    ref_count=count - 1,
                )
                return
            self._ref_counts.pop(session_key, None)
            # Schedule the TTL task under the lock so _ttl_tasks mutations are
            # always lock-protected (consistent with acquire()). ttl==0 stops the
            # server via _stop() below — which takes the same lock, so it must run
            # AFTER this block exits to avoid re-entrant deadlock.
            if ttl_seconds > 0:
                log.debug(
                    "EnginePool.release: scheduling TTL expiry",
                    session_key=session_key,
                    ttl_seconds=ttl_seconds,
                )
                old = self._ttl_tasks.pop(session_key, None)
                if old is not None and not old.done():
                    old.cancel()
                self._ttl_tasks[session_key] = asyncio.create_task(
                    self._expire(session_key, ttl_seconds)
                )

        if ttl_seconds == 0:
            await self._stop(session_key)

    async def _expire(self, session_key: str, ttl: float) -> None:
        """Sleep ttl seconds, then stop the key's server (unless re-acquired / superseded).

        B7: the stop decision is re-checked UNDER the key lock. A re-acquire during the
        sleep bumps the ref-count and cancels/replaces this task; if the cancellation lost
        the race (this task already passed its sleep), the recheck still refuses to stop a
        server that was handed back out. The recheck + pop are one atomic critical section
        (inlined _stop) so nothing can slip between them.
        """
        try:
            await asyncio.sleep(ttl)
        except asyncio.CancelledError:
            log.debug("EnginePool._expire: cancelled", session_key=session_key)
            return
        async with self._get_lock(session_key):
            if self._ref_counts.get(session_key, 0) > 0:
                # Re-acquired during the sleep — server is in use, do NOT stop.
                return
            if self._ttl_tasks.get(session_key) not in (None, asyncio.current_task()):
                # A newer release scheduled a different expiry task — let it own the stop.
                return
            self._ttl_tasks.pop(session_key, None)
            server = self._servers.pop(session_key, None)
            self._ref_counts.pop(session_key, None)
        if server is None:
            return
        log.info("EnginePool._expire: TTL elapsed — stopping", session_key=session_key, ttl=ttl)
        try:
            await server.stop()
        except Exception:  # noqa: BLE001
            log.warning("EnginePool._expire: stop error", session_key=session_key, exc_info=True)

    async def _stop(self, session_key: str) -> None:
        """Stop and drop the server for one key (idempotent)."""
        lock = self._get_lock(session_key)
        async with lock:
            server = self._servers.pop(session_key, None)
            self._ref_counts.pop(session_key, None)
        if server is None:
            return
        try:
            await server.stop()
            log.info("EnginePool._stop: server stopped", session_key=session_key)
        except Exception:  # noqa: BLE001
            log.warning(
                "EnginePool._stop: error stopping server",
                session_key=session_key,
                exc_info=True,
            )

    async def stop_all(self) -> None:
        """Stop every live server and clear the pool (shutdown / tui exit)."""
        for task in list(self._ttl_tasks.values()):
            if not task.done():
                task.cancel()
        self._ttl_tasks.clear()
        for session_key in list(self._servers.keys()):
            await self._stop(session_key)


async def _default_start_server(config: EngineConfig, session_key: str) -> ManagedServer:
    """Default start-server: full lifecycle launch + poll_ready into the SHARED home.

    HOME is ``config.home`` (shared across all keys, created if absent). Per-key
    isolation is the opencode CONFIG FILE (``opencode_<key>.json`` via
    ``OPENCODE_CONFIG``), written by ``launch`` from ``session_key`` — so distinct
    keys never race the same config file (I-1) while sharing skills/.ach-state/
    node_modules/session store under the one home. Tests replace
    ``pool._start_server`` with a fake ``(config, session_key)``.
    """
    from ach_agent.engine.client import find_free_port
    from ach_agent.engine.lifecycle import launch, poll_ready

    home = Path(config.home)
    home.mkdir(parents=True, exist_ok=True)
    # opencode serve binds loopback on a free ephemeral port the harness picks (so it knows
    # the port for its client + `opencode attach`); never published off-host.
    port = find_free_port()
    server = await launch(port, home, config, session_key)
    await poll_ready(server, config.startup_timeout_seconds)
    return server

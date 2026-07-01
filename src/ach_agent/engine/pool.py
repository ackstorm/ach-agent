# SPDX-License-Identifier: Apache-2.0
"""Keyed multi-server pool for EnginePool (ENG-08).

Keyed by session_key: each session identity (cron name, gitlab server+repo,
tui-console) gets its own ManagedServer. See D-07 for scope boundary.

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
import dataclasses
import hashlib
import re
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
    own ManagedServer with its own port and its own HOME (opencode.json, the
    opencode session store, and node_modules) under
    ``<config.home>/servers/oc-<key>`` — so concurrent servers for distinct keys
    never race the same opencode.json (I-1). The home is stable per key: the same
    session_key reuses its home across invocations (node_modules cache reuse).
    The working directory (``config.work_dir``, cwd) is NOT per-key — it remains
    the shared checkout root, as it was before keying. Reference counting per key
    ensures a key's TTL only starts once no invocation holds it.

    Disk tradeoff (explicit v1 decision): per-key homes are NOT reaped on stop —
    stopping a server leaves ``<base>/servers/oc-<key>`` (incl. its node_modules)
    on disk so the next invocation of that key reuses the cache. Live-server disk
    is bounded by ``maxConcurrentInvocations``; on-disk home count grows with the
    number of DISTINCT keys ever seen. For bounded-cardinality channels (cron/
    tui/queue/a2a: key = channel name) this is fine. For an unbounded key space
    (gitlab key = server+repo across many repos over a long-lived pod) it can
    fill disk. v1 accepts this; a reaper (GC ``servers/oc-*`` whose key is not
    live, e.g. by mtime) is a tracked follow-up, not built here.

    For ttl_seconds == 0 (spawn-per-invocation, v1 default): the key's server is
    stopped as soon as its last holder releases. For ttl_seconds > 0: the server
    is kept warm for that key until the idle TTL elapses; a re-acquire cancels
    the pending expiry.

    Constraint: No router or Hermes imports (D-08, RTR-06).
    """

    def __init__(self) -> None:
        self._servers: dict[str, ManagedServer] = {}
        self._ref_counts: dict[str, int] = {}
        self._ttl_tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

        # A′ proven-start latch (DUR-02, D-08-latch).
        # Set True the first time ANY server starts successfully; never reset.
        self.engine_has_been_ready_once: bool = False

        # _start_server is injectable for testing (replaced by tests with a fake).
        # In production it points to the lifecycle launch helper. It receives only
        # the EngineConfig — session_key is pool-internal.
        self._start_server: Callable[..., Awaitable[ManagedServer]] = _default_start_server

    @staticmethod
    def _config_for_key(session_key: str, config: EngineConfig) -> EngineConfig:
        """Return a copy of ``config`` whose HOME is isolated per session_key.

        Each key launches into ``<config.home>/servers/oc-<safe>-<hash>`` so two
        concurrent servers for distinct keys never share opencode.json, the
        session store, or node_modules (I-1: a shared HOME let concurrent
        ``write_opencode_config`` calls tear/cross-wire the same file). The path
        is deterministic in ``session_key`` — the same key reuses its home, so
        node_modules is reinstalled only when a new key first appears.

        Non-dataclass configs (test fakes / MagicMock) or an empty home pass
        through unchanged.
        """
        if not dataclasses.is_dataclass(config) or isinstance(config, type):
            return config
        base = getattr(config, "home", "") or ""
        if not base:
            return config
        digest = hashlib.sha1(session_key.encode("utf-8")).hexdigest()[:8]
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_key)[:48]
        per_key_home = str(Path(base) / "servers" / f"oc-{safe}-{digest}")
        return dataclasses.replace(config, home=per_key_home)

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
            server = await self._start_server(self._config_for_key(session_key, config))
            # A′ latch: _start_server only returns on success (poll_ready calls
            # sys.exit(1) on failure), so reaching here proves the engine was ready.
            self.engine_has_been_ready_once = True
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
        """Sleep ttl seconds, then stop the key's server (unless cancelled)."""
        try:
            await asyncio.sleep(ttl)
        except asyncio.CancelledError:
            log.debug("EnginePool._expire: cancelled", session_key=session_key)
            return
        log.info("EnginePool._expire: TTL elapsed — stopping", session_key=session_key, ttl=ttl)
        self._ttl_tasks.pop(session_key, None)
        await self._stop(session_key)

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


async def _default_start_server(config: EngineConfig) -> ManagedServer:
    """Default start-server implementation: full lifecycle launch + poll_ready.

    HOME is ``config.home`` (created if absent). The caller (EnginePool.acquire)
    passes a per-session_key home (``<base>/servers/oc-<key>``) so opencode's
    config, session store, and node_modules are isolated per key and never race
    a shared file across concurrent servers (I-1). The per-key home is stable, so
    node_modules persists across invocations of the same key.
    Used in production. Tests replace pool._start_server with a fake.
    """
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

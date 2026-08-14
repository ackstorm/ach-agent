# SPDX-License-Identifier: Apache-2.0
"""Boot-time store selection: dedup store + session map, per persistence config."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import MutableMapping
from pathlib import Path

import structlog

from ach_agent.config.schema import AgentConfig
from ach_agent.router.dedup import DedupStore

log = structlog.get_logger(__name__)


def open_dedup_store(cfg: AgentConfig) -> DedupStore:
    """Select and open the dedup store per persistence config (D-03/D-04).

    persistence.enabled=false → InMemoryDedupStore (no disk dependency).
    persistence.enabled=true  → FileBackedDedupStore on mountPath/state/state.db
      (shared harness sqlite; dedup is its first table, more may follow).
      Missing / non-writable mount → sys.exit(1) fail-closed (D-04a,
      mirrors ENG-06 poll_ready exit pattern).
      Corrupt state.db → fail-open: move aside, start fresh, WARN +
      PERSISTENCE_DEGRADED metric (D-04b, T-03-08: file preserved for forensics).

    Never logs ek_ / GITLAB_TOKEN values (T-03-07 mitigation).
    """
    from ach_agent.router.dedup import FileBackedDedupStore, InMemoryDedupStore
    from ach_agent.router.metrics import PERSISTENCE_DEGRADED

    if not cfg.persistence.enabled:
        return InMemoryDedupStore()

    mount = Path(cfg.persistence.mount_path)

    # D-04a: missing / non-writable mount → fail-closed (loud — ENG-06 pattern)
    if not mount.exists() or not os.access(mount, os.W_OK):
        log.error(
            "persistence.enabled=true but mountPath missing or not writable — exiting",
            mount_path=str(mount),
        )
        sys.exit(1)

    db_path = mount / "state" / "state.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        store = FileBackedDedupStore(db_path)
        log.info("durable dedup store opened", db_path=str(db_path))
        return store
    except Exception as exc:
        # D-04b: corrupt / unreadable DB → fail-open: move aside, start fresh.
        # Preserved for forensics (T-03-08: not deleted, only renamed).
        aside_path = db_path.with_suffix(f".corrupt.{int(time.time())}.db")
        try:
            db_path.rename(aside_path)
            log.warning(
                "state.db corrupt — moved aside, starting fresh (fail-open)",
                db_path=str(db_path),
                aside_path=str(aside_path),
                error=str(exc),
            )
        except OSError as rename_exc:
            log.warning(
                "state.db corrupt and could not be moved aside — retrying fresh store",
                db_path=str(db_path),
                error=str(exc),
                rename_error=str(rename_exc),
            )
        PERSISTENCE_DEGRADED.inc()
        # Retry with a fresh DB file after moving the corrupt one aside
        try:
            return FileBackedDedupStore(db_path)
        except Exception:
            # Final fallback: in-memory (degraded mode, DB path still unusable)
            return InMemoryDedupStore()


def open_session_store(cfg: AgentConfig) -> MutableMapping[str, str]:
    """Select the pool's session_key → opencode-session map per persistence config.

    persistence.enabled=false → in-memory _LRUSessionMap (volatile, current behavior).
    persistence.enabled=true  → _SqliteSessionMap on mountPath/state/state.db, so
      channel.session='auto' continuity survives a full harness restart; bounded to
      maxsize rows (LRU by last_used).

    Fail-OPEN (unlike open_dedup_store, which fail-CLOSES): a missing mount or a DB
    error degrades to the in-memory map + WARN + PERSISTENCE_DEGRADED, because losing
    conversational continuity is a soft degrade, not a duplicate-firing hazard.

    Call AFTER open_dedup_store: that opens/repairs state.db first, so this second WAL
    connection just adds the oc_sessions table to an already-valid file.
    """
    from ach_agent.engine.pool import _LRUSessionMap, _SqliteSessionMap
    from ach_agent.router.metrics import PERSISTENCE_DEGRADED

    if not cfg.persistence.enabled:
        return _LRUSessionMap()

    db_path = Path(cfg.persistence.mount_path) / "state" / "state.db"
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = _SqliteSessionMap(db_path)
        log.info("durable session map opened", db_path=str(db_path), rows=len(store))
        return store
    except Exception as exc:  # noqa: BLE001 — fail-open to in-memory (degraded, not fatal)
        log.warning(
            "session map open failed — using in-memory (fail-open)",
            db_path=str(db_path),
            error=str(exc),
        )
        PERSISTENCE_DEGRADED.inc()
        return _LRUSessionMap()

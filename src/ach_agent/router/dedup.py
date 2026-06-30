# SPDX-License-Identifier: Apache-2.0
"""DedupStore Protocol + InMemoryDedupStore + idempotency-key derivation (IDM-01/02/03).

Phase 1: InMemoryDedupStore (in-memory TTL dict).
Phase 3: FileBackedDedupStore swaps in without touching router — the
Protocol is the stability guarantee.

Idempotency derivation functions (pure logic, CONTRACT §6.1, spec §18.4.0):
  - derive_webhook_idempotency_key: header priority chain → ms-timestamp fallback
  - derive_cron_idempotency_key:    {channel}:{scheduled_tick_iso} (D-09)

Constraint: NEVER import from hermes_agent.* or engine.* here (RTR-06, D-08).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Protocol

# ---------------------------------------------------------------------------
# DedupStore Protocol (swappable Phase 1 → Phase 3)
# ---------------------------------------------------------------------------


class DedupStore(Protocol):
    """Protocol for idempotency key stores (swappable impl, IDM-03).

    Phase 1: InMemoryDedupStore
    Phase 3: FileBackedDedupStore (durable across restarts)
    """

    def seen(self, key: str) -> bool: ...

    def mark(self, key: str, ttl_seconds: int) -> None: ...


class InMemoryDedupStore:
    """Phase 1 in-memory TTL dedup store.

    Stores key → monotonic expiry timestamp. Lazy _prune() on each mark()
    bounds memory to the active idempotency window (Performance Trap mitigation).
    """

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}  # key -> expiry timestamp (monotonic)

    def seen(self, key: str) -> bool:
        """Return True if key is still within its TTL window."""
        expiry = self._seen.get(key)
        if expiry is None:
            return False
        if time.monotonic() > expiry:
            del self._seen[key]
            return False
        return True

    def mark(self, key: str, ttl_seconds: int) -> None:
        """Record key as seen for ttl_seconds from now."""
        self._seen[key] = time.monotonic() + ttl_seconds
        self._prune()  # lazy prune — bound memory (Performance Trap, RESEARCH.md)

    def _prune(self) -> None:
        """Remove all expired keys (called lazily on each mark)."""
        now = time.monotonic()
        expired = [k for k, exp in self._seen.items() if now > exp]
        for k in expired:
            del self._seen[k]


class FileBackedDedupStore:
    """Phase 3 SQLite-backed TTL dedup store (DUR-01, D-01/D-02).

    Stores key → wall-clock expiry in a SQLite WAL database. Survives pod
    restart because expiry is stored as time.time() (wall-clock), NOT
    time.monotonic() (which resets on restart).

    All SQL uses '?' parameterized placeholders — never string interpolation (ASVS V5, T-03-01).
    Constraint: NEVER import from hermes_agent.* or engine.* here (RTR-06, D-08).
    """

    def __init__(self, db_path: Path) -> None:
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS dedup (key TEXT PRIMARY KEY, expiry REAL NOT NULL)"
        )
        self._con.execute("CREATE INDEX IF NOT EXISTS idx_dedup_expiry ON dedup(expiry)")
        self._con.commit()

    def seen(self, key: str) -> bool:
        """Return True if key is still within its TTL window (wall-clock)."""
        row = self._con.execute(
            "SELECT 1 FROM dedup WHERE key=? AND expiry > ?",
            (key, time.time()),
        ).fetchone()
        return row is not None

    def mark(self, key: str, ttl_seconds: int) -> None:
        """Record key as seen for ttl_seconds from now (wall-clock expiry).

        Lazy prune: DELETE WHERE expiry < now() runs in same transaction (T-03-03).
        """
        # Read the clock once so the just-inserted expiry and the prune cutoff share a
        # consistent `now` — a separate time.time() for the DELETE could (under a forward
        # clock jump) prune the key we just marked.
        now = time.time()
        self._con.execute(
            "INSERT OR REPLACE INTO dedup (key, expiry) VALUES (?,?)",
            (key, now + ttl_seconds),
        )
        # Lazy prune — bound table size to active idempotency window (T-03-03, D-02)
        self._con.execute("DELETE FROM dedup WHERE expiry < ?", (now,))
        self._con.commit()

    def close(self) -> None:
        """Close the SQLite connection."""
        self._con.close()


# ---------------------------------------------------------------------------
# Idempotency key derivation — pure functions, no side effects (IDM-01)
# ---------------------------------------------------------------------------


def derive_webhook_idempotency_key(headers: dict[str, str]) -> str:
    """Webhook: header priority chain → ms-timestamp fallback (CONTRACT §6.1).

    Priority (ORDER IS NORMATIVE per spec §18.4.0):
      X-GitHub-Delivery → X-Gitlab-Event-UUID → svix-id → X-Request-ID → Idempotency-Key
    Fallback: str(int(time.time() * 1000)) — unique-per-arrival, never empty/shared.

    Pitfall 1: the fallback MUST be ms-timestamp, not "" or None or a constant.
    """
    for header in (
        "X-GitHub-Delivery",
        "X-Gitlab-Event-UUID",
        "svix-id",
        "X-Request-ID",
        "Idempotency-Key",
    ):
        if val := headers.get(header):
            return val
    # Fallback: unique-per-arrival (IDM-02, Pitfall 1: never empty/shared)
    return str(int(time.time() * 1000))


def derive_cron_idempotency_key(channel_name: str, scheduled_tick: datetime) -> str:
    """{channel}:{scheduled_tick_iso} — deterministic per scheduled tick (D-09).

    D-09: use scheduled_tick (from croniter.get_next()), NOT datetime.now().
    Using now() would create a different key each invocation, preventing
    correct dedup on restart redelivery (Pitfall 5).
    """
    tick_iso = scheduled_tick.strftime("%Y-%m-%dT%H:%M:%S")
    return f"{channel_name}:{tick_iso}"


def derive_a2a_idempotency_key(task_id: str) -> str:
    """A2A: task_id → a2a:{task_id}; empty → ms-timestamp fallback (CHN-05, D-03, IDM-01).

    Invariant: unique-per-distinct-event, degrade to unique-per-arrival, never empty/shared.
    Uses the same ms-timestamp fallback pattern as other derivers when task_id is absent.
    """
    if task_id:
        return f"a2a:{task_id}"
    return str(int(time.time() * 1000))

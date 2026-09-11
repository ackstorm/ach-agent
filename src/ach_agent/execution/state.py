"""Engine-owned native session map and bounded legacy migration."""

from __future__ import annotations

import math
import sqlite3
from collections.abc import Iterable, Mapping
from itertools import islice
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from ach_agent.engine.base.pool import _SqliteSessionMap

MAX_MIGRATION_ROWS = 1024
_MIGRATION_NAME = "oc_sessions_v1"


class LegacySessionRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    key: str
    oc_session_id: str
    last_used: float

    @field_validator("last_used")
    @classmethod
    def finite_last_used(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("last_used must be finite")
        return value


class MigrationAlreadyComplete(RuntimeError):
    """The one-time legacy import marker is already committed."""


class NativeSessionStore(_SqliteSessionMap):
    """Normal session-map access rooted at engine-home/.ach-execution/sessions.db."""

    def __init__(self, engine_home: str | Path, maxsize: int = 1024) -> None:
        self.engine_home = Path(engine_home).absolute()
        self.path = self.engine_home / ".ach-execution" / "sessions.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(self.path, maxsize=maxsize)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS execution_migrations "
            "(name TEXT PRIMARY KEY, completed_at REAL NOT NULL)"
        )
        self._con.commit()

    @property
    def migration_complete(self) -> bool:
        row = self._con.execute(
            "SELECT 1 FROM execution_migrations WHERE name=?", (_MIGRATION_NAME,)
        ).fetchone()
        return row is not None


def export_legacy_sessions(
    db_path: str | Path, *, limit: int = MAX_MIGRATION_ROWS
) -> list[LegacySessionRow]:
    """Read only the legacy ``oc_sessions`` table, bounded to the newest rows."""
    if limit < 0:
        raise ValueError("migration limit must be non-negative")
    path = Path(db_path)
    if not path.exists():
        return []
    con = sqlite3.connect(f"file:{path.absolute()}?mode=ro", uri=True)
    try:
        try:
            rows = con.execute(
                "SELECT key, oc_session_id, last_used FROM oc_sessions "
                "ORDER BY last_used DESC, key ASC LIMIT ?",
                (min(limit, MAX_MIGRATION_ROWS),),
            ).fetchall()
        except sqlite3.Error as exc:
            if "no such table" in str(exc).lower():
                return []
            raise
        return [
            LegacySessionRow(key=key, oc_session_id=session_id, last_used=last_used)
            for key, session_id, last_used in rows
        ]
    finally:
        con.close()


def _coerce_row(row: LegacySessionRow | Mapping[str, Any]) -> LegacySessionRow:
    if isinstance(row, LegacySessionRow):
        return row
    try:
        return LegacySessionRow.model_validate(row)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid legacy session row") from exc


def import_legacy_sessions(
    store: NativeSessionStore, rows: Iterable[LegacySessionRow | Mapping[str, Any]]
) -> int:
    """Atomically import rows and completion marker; reject repeat imports."""
    bounded = list(islice(rows, MAX_MIGRATION_ROWS + 1))
    if len(bounded) > MAX_MIGRATION_ROWS:
        raise ValueError(f"legacy migration exceeds {MAX_MIGRATION_ROWS} rows")
    imported = [_coerce_row(row) for row in bounded]
    try:
        store._con.execute("BEGIN IMMEDIATE")
        marker = store._con.execute(
            "SELECT 1 FROM execution_migrations WHERE name=?", (_MIGRATION_NAME,)
        ).fetchone()
        if marker is not None:
            store._con.rollback()
            raise MigrationAlreadyComplete("legacy session migration already completed")
        for row in imported:
            # Existing engine values win: a restart must not overwrite newer mappings.
            store._con.execute(
                "INSERT OR IGNORE INTO oc_sessions (key, oc_session_id, last_used) VALUES (?,?,?)",
                (row.key, row.oc_session_id, row.last_used),
            )
        store._con.execute(
            "INSERT INTO execution_migrations (name, completed_at) "
            "VALUES (?, strftime('%s','now'))",
            (_MIGRATION_NAME,),
        )
        store._con.execute(
            "DELETE FROM oc_sessions WHERE key NOT IN "
            "(SELECT key FROM oc_sessions ORDER BY last_used DESC LIMIT ?)",
            (store._maxsize,),
        )
        store._con.commit()
    except MigrationAlreadyComplete:
        raise
    except Exception:
        store._con.rollback()
        raise
    return len(imported)

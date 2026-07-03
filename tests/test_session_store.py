# SPDX-License-Identifier: Apache-2.0
"""Unit tests for main._open_session_store — persistent vs in-memory selection."""

from __future__ import annotations

from pathlib import Path

from ach_agent.config.schema import AgentConfig

_BASE: dict = {
    "schemaVersion": "1",
    "agent": {"name": "a"},
    "model": {"name": "openai.gpt-5", "type": "openai"},
    "capability": {"ach": {"baseUrl": "https://ach.example.com"}},
}


def _cfg(persistence: dict) -> AgentConfig:
    raw: dict = dict(_BASE)
    raw["persistence"] = persistence
    return AgentConfig.model_validate(raw)


def test_open_session_store_disabled_is_in_memory() -> None:
    from ach_agent.engine.pool import _LRUSessionMap, _SqliteSessionMap
    from ach_agent.main import _open_session_store

    store = _open_session_store(_cfg({"enabled": False}))
    assert isinstance(store, _LRUSessionMap)
    assert not isinstance(store, _SqliteSessionMap)


def test_open_session_store_enabled_persists_to_state_db(tmp_path: Path) -> None:
    from ach_agent.engine.pool import _SqliteSessionMap
    from ach_agent.main import _open_session_store

    store = _open_session_store(_cfg({"enabled": True, "mountPath": str(tmp_path)}))
    assert isinstance(store, _SqliteSessionMap)
    store["lane-1"] = "ses-a"
    store.close()
    assert (tmp_path / "state" / "state.db").exists()


def test_open_session_store_shares_state_db_with_dedup(tmp_path: Path) -> None:
    """Dedup (opened first) and the session store live in the same state.db file."""
    from ach_agent.main import _open_dedup_store, _open_session_store

    cfg = _cfg({"enabled": True, "mountPath": str(tmp_path)})
    dedup = _open_dedup_store(cfg)
    dedup.mark("evt-1", ttl_seconds=3600)
    sess = _open_session_store(cfg)
    sess["lane-1"] = "ses-a"
    sess.close()
    dedup.close()

    # Reopen both from the same file — each table survives independently.
    dedup2 = _open_dedup_store(cfg)
    assert dedup2.seen("evt-1")
    sess2 = _open_session_store(cfg)
    assert sess2.get("lane-1") == "ses-a"
    sess2.close()
    dedup2.close()

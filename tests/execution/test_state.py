from __future__ import annotations

import sqlite3

import pytest


def test_native_store_uses_engine_home_and_imports_bounded_rows_transactionally(tmp_path) -> None:
    from ach_agent.execution.state import (
        MigrationAlreadyComplete,
        NativeSessionStore,
        export_legacy_sessions,
        import_legacy_sessions,
    )

    legacy = tmp_path / "state.db"
    con = sqlite3.connect(legacy)
    con.execute(
        "CREATE TABLE oc_sessions (key TEXT PRIMARY KEY, "
        "oc_session_id TEXT NOT NULL, last_used REAL NOT NULL)"
    )
    con.execute("CREATE TABLE dedup (id TEXT PRIMARY KEY)")
    con.execute("INSERT INTO oc_sessions VALUES ('opencode:lane', 'ses_opaque', 1.0)")
    con.execute("INSERT INTO oc_sessions VALUES ('pi:lane', '/engine/home/sessions/pi.jsonl', 2.0)")
    con.execute("INSERT INTO dedup VALUES ('keep-me')")
    con.commit()
    con.close()

    exported = export_legacy_sessions(legacy)
    assert [(row.key, row.oc_session_id) for row in exported] == [
        ("pi:lane", "/engine/home/sessions/pi.jsonl"),
        ("opencode:lane", "ses_opaque"),
    ]

    store = NativeSessionStore(tmp_path / "engine-home")
    assert store.path == tmp_path / "engine-home" / ".ach-execution" / "sessions.db"
    imported = import_legacy_sessions(store, exported)
    assert imported == 2
    assert store.get("opencode:lane") == "ses_opaque"
    assert store.get("pi:lane") == "/engine/home/sessions/pi.jsonl"
    store.close()

    store = NativeSessionStore(tmp_path / "engine-home")
    with pytest.raises(MigrationAlreadyComplete):
        import_legacy_sessions(store, exported)
    store.close()

    check = sqlite3.connect(legacy)
    assert check.execute("SELECT id FROM dedup").fetchone() == ("keep-me",)
    check.close()


def test_failed_import_does_not_mark_completion(tmp_path) -> None:
    from ach_agent.execution.state import NativeSessionStore, import_legacy_sessions

    store = NativeSessionStore(tmp_path / "home")
    try:
        import_legacy_sessions(store, [{"bad": "row"}])
    except Exception:
        pass
    assert not store.migration_complete
    assert len(store) == 0
    store.close()

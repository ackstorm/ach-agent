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
    store["opencode:lane"] = "newer-after-migration"
    with pytest.raises(MigrationAlreadyComplete):
        import_legacy_sessions(store, exported)
    assert store.get("opencode:lane") == "newer-after-migration"
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


def test_export_punctuation_path_is_read_only_and_missing_path_is_not_created(tmp_path) -> None:
    from ach_agent.execution.state import export_legacy_sessions

    punctuation = tmp_path / "legacy #state?.db"
    con = sqlite3.connect(punctuation)
    con.execute("CREATE TABLE oc_sessions (key TEXT, oc_session_id TEXT, last_used REAL)")
    con.execute("INSERT INTO oc_sessions VALUES ('opencode:key', 'ses-id', 1.0)")
    con.commit()
    con.close()
    assert export_legacy_sessions(punctuation)[0].oc_session_id == "ses-id"
    missing = tmp_path / "missing #state?.db"
    assert export_legacy_sessions(missing) == []
    assert not missing.exists()


def test_import_bounds_generator_and_preserves_existing_mapping_and_maxsize(tmp_path) -> None:
    from ach_agent.execution.state import NativeSessionStore, import_legacy_sessions

    consumed = 0

    def rows():
        nonlocal consumed
        for index in range(2000):
            consumed += 1
            yield {
                "key": f"opencode:{index}",
                "oc_session_id": f"ses-{index}",
                "last_used": float(index),
            }

    store = NativeSessionStore(tmp_path / "home", maxsize=1)
    store["opencode:0"] = "newer-native"
    with pytest.raises(ValueError, match="exceeds"):
        import_legacy_sessions(store, rows())
    assert consumed == 1025
    assert store.get("opencode:0") == "newer-native"
    assert not store.migration_complete
    store.close()

    store = NativeSessionStore(tmp_path / "home-2", maxsize=1)
    store["opencode:0"] = "newer-native"
    import_legacy_sessions(
        store,
        [
            {"key": "opencode:0", "oc_session_id": "ses-old", "last_used": 1.0},
            {"key": "opencode:1", "oc_session_id": "ses-new", "last_used": 2.0},
        ],
    )
    assert len(store) == 1
    assert store.get("opencode:0") == "newer-native"
    assert store.get("opencode:1") is None
    store.close()


def test_import_rolls_back_rows_when_marker_write_fails(tmp_path) -> None:
    from ach_agent.execution.state import NativeSessionStore, import_legacy_sessions

    store = NativeSessionStore(tmp_path / "home")
    store._con.execute(
        "CREATE TRIGGER fail_migration_marker BEFORE INSERT ON execution_migrations "
        "BEGIN SELECT RAISE(ABORT, 'marker failure'); END"
    )
    store._con.commit()
    with pytest.raises(sqlite3.IntegrityError, match="marker failure"):
        import_legacy_sessions(
            store,
            [{"key": "pi:lane", "oc_session_id": "/engine/home/pi.jsonl", "last_used": 1.0}],
        )
    assert len(store) == 0
    assert not store.migration_complete
    store.close()


@pytest.mark.parametrize(
    "row",
    [
        {"key": "x", "oc_session_id": "y", "last_used": float("nan")},
        {"key": "x", "oc_session_id": "y", "last_used": 1.0, "extra": "reject"},
        {"key": 1, "oc_session_id": "y", "last_used": 1.0},
    ],
)
def test_import_rejects_malformed_rows(tmp_path, row) -> None:
    from ach_agent.execution.state import NativeSessionStore, import_legacy_sessions

    store = NativeSessionStore(tmp_path / "home")
    with pytest.raises(ValueError):
        import_legacy_sessions(store, [row])
    assert not store.migration_complete
    store.close()

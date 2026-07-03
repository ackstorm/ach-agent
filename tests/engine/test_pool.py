# SPDX-License-Identifier: Apache-2.0
"""Tests for the keyed EnginePool (ENG-08 + keyed migration).

Each session_key maps to its own ManagedServer. acquire/release take a
session_key; releasing one key never affects another.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from ach_agent.engine.pool import EnginePool

if TYPE_CHECKING:
    from ach_agent.engine.lifecycle import ManagedServer


def _make_fake_server(alive: bool = True):
    from unittest.mock import AsyncMock, MagicMock

    srv = MagicMock()
    srv.is_alive.return_value = alive
    srv.stop = AsyncMock()
    return srv


def _config():
    from unittest.mock import MagicMock

    return MagicMock(name="EngineConfig")


async def test_pool_reuse_same_key() -> None:
    """Second acquire with the same key reuses the alive server (no new start)."""
    pool = EnginePool()
    calls = {"n": 0}
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        calls["n"] += 1
        return fake

    pool._start_server = fake_start

    s1 = await pool.acquire("k1", _config())
    s2 = await pool.acquire("k1", _config())
    assert calls["n"] == 1, "Same key must reuse the server"
    assert s1 is fake and s2 is fake
    assert pool._ref_counts["k1"] == 2


async def test_pool_distinct_keys_get_distinct_servers() -> None:
    """Different keys start different servers and are tracked independently."""
    pool = EnginePool()
    servers = {"k1": _make_fake_server(), "k2": _make_fake_server()}
    seq = iter([servers["k1"], servers["k2"]])  # returned in acquire order

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return next(seq)

    pool._start_server = fake_start

    a = await pool.acquire("k1", _config())
    b = await pool.acquire("k2", _config())

    assert a is servers["k1"]
    assert b is servers["k2"]
    assert set(pool._servers.keys()) == {"k1", "k2"}


async def test_release_one_key_does_not_stop_another() -> None:
    """Stopping k1 (ttl=0) must leave k2's server alive and untracked-untouched."""
    pool = EnginePool()
    servers = {"k1": _make_fake_server(), "k2": _make_fake_server()}
    seq = iter([servers["k1"], servers["k2"]])  # returned in acquire order

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return next(seq)

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.acquire("k2", _config())

    await pool.release("k1", ttl_seconds=0)

    servers["k1"].stop.assert_awaited_once()
    servers["k2"].stop.assert_not_awaited()
    assert "k1" not in pool._servers
    assert "k2" in pool._servers


async def test_ttl0_stops_immediately() -> None:
    """release(ttl=0) stops that key's server and removes it from the pool."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0)
    fake.stop.assert_awaited_once()
    assert "k1" not in pool._servers


async def test_ttl_expires_after_delay() -> None:
    """release(ttl>0) keeps the server until TTL elapses, then stops it."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0.05)
    assert fake.stop.call_count == 0
    assert "k1" in pool._servers

    await asyncio.sleep(0.12)
    fake.stop.assert_awaited_once()
    assert "k1" not in pool._servers


async def test_reacquire_cancels_pending_ttl() -> None:
    """A re-acquire before TTL fires cancels that key's expiry task."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0.05)
    await pool.acquire("k1", _config())  # cancels expiry
    await asyncio.sleep(0.12)
    fake.stop.assert_not_awaited()
    assert "k1" in pool._servers


async def test_warm_reuse_within_ttl() -> None:
    """acquire → release(ttl>0) → acquire returns the SAME server, started only once (B1)."""
    pool = EnginePool()
    calls = {"n": 0}
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        calls["n"] += 1
        return fake

    pool._start_server = fake_start

    s1 = await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0.2)  # warm — expiry armed
    s2 = await pool.acquire("k1", _config())  # within TTL — reuse, cancel expiry
    assert s1 is fake and s2 is fake
    assert calls["n"] == 1, "warm reuse must not start a second server"
    fake.stop.assert_not_awaited()


async def test_expire_rechecks_before_stop() -> None:
    """A re-acquire during the TTL sleep must not be stopped by the stale _expire (B7).

    release(ttl) arms _expire; a re-acquire before it fires bumps the ref back to 1 and
    cancels the task. The race-safe recheck guarantees a server handed back out is never
    stopped by an in-flight expiry.
    """
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0.01)  # arm expiry (fires very soon)
    await pool.acquire("k1", _config())  # re-acquire: ref→1, expiry cancelled
    await asyncio.sleep(0.05)  # let any stale _expire run past its sleep

    fake.stop.assert_not_awaited()
    assert "k1" in pool._servers, "re-acquired server must not be stopped by stale _expire"
    assert pool._ref_counts.get("k1", 0) == 1


async def test_ref_count_keeps_server_until_last_release() -> None:
    """Two acquires + one release keep the server; second release (ttl=0) stops it."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0)
    fake.stop.assert_not_awaited()
    assert "k1" in pool._servers
    await pool.release("k1", ttl_seconds=0)
    fake.stop.assert_awaited_once()
    assert "k1" not in pool._servers


async def test_dead_server_replaced_on_acquire() -> None:
    """If the tracked server is dead, acquire starts a fresh one for that key."""
    pool = EnginePool()
    dead = _make_fake_server(alive=False)
    live = _make_fake_server(alive=True)
    seq = [dead, live]

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return seq.pop(0)

    pool._start_server = fake_start

    s1 = await pool.acquire("k1", _config())
    assert s1 is dead
    s2 = await pool.acquire("k1", _config())  # dead → replace
    assert s2 is live
    dead.stop.assert_awaited_once()


async def test_stop_all_stops_every_server() -> None:
    """stop_all() stops every live server and clears the pool."""
    pool = EnginePool()
    servers = {"k1": _make_fake_server(), "k2": _make_fake_server()}
    seq = iter([servers["k1"], servers["k2"]])  # returned in acquire order

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return next(seq)

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.acquire("k2", _config())

    await pool.stop_all()
    servers["k1"].stop.assert_awaited_once()
    servers["k2"].stop.assert_awaited_once()
    assert pool._servers == {}


# ---------------------------------------------------------------------------
# I-1: per-key server isolation — distinct keys get distinct ManagedServer
# objects so concurrent servers are independently ref-counted and stopped.
# All servers share the same config.home; per-key isolation is the
# opencode_<key>.json config file (via OPENCODE_CONFIG).
# ---------------------------------------------------------------------------


async def test_distinct_keys_get_distinct_servers_and_refcounts() -> None:
    """Different keys start independent ManagedServer objects with independent ref-counts.

    All servers receive the same (shared) config.home — isolation is handled by
    the per-key opencode config file, not a per-key home directory.
    """
    pool = EnginePool()
    srv_a = _make_fake_server(alive=True)
    srv_b = _make_fake_server(alive=True)
    seq = iter([srv_a, srv_b])

    async def fake_start(cfg, session_key: str) -> ManagedServer:
        return next(seq)

    pool._start_server = fake_start

    await pool.acquire("gitlab.example.com/group/repo-a", _config())
    await pool.acquire("gitlab.example.com/group/repo-b", _config())

    assert pool._servers["gitlab.example.com/group/repo-a"] is srv_a
    assert pool._servers["gitlab.example.com/group/repo-b"] is srv_b
    assert srv_a is not srv_b, "distinct keys must get distinct ManagedServer objects"

    # Independent ref-counts: stopping repo-a leaves repo-b unaffected.
    await pool.release("gitlab.example.com/group/repo-a", ttl_seconds=0)
    srv_a.stop.assert_awaited_once()
    srv_b.stop.assert_not_awaited()
    assert "gitlab.example.com/group/repo-a" not in pool._servers
    assert pool._ref_counts.get("gitlab.example.com/group/repo-b", 0) == 1


# ---------------------------------------------------------------------------
# Non-keyed coverage preserved from the pre-migration test file:
# _default_start_server home behavior and main._harness_log_dir.
# ---------------------------------------------------------------------------


async def test_default_start_server_uses_config_home(tmp_path: Path, monkeypatch) -> None:
    """The pool launches opencode in the stable engine.home, not a fresh mkdtemp."""
    from ach_agent.engine import pool as poolmod
    from ach_agent.engine.lifecycle import EngineConfig

    captured: dict[str, object] = {}

    async def fake_launch(port: int, home: Path, config: object, session_key: str) -> object:
        captured["home"] = home
        captured["port"] = port
        captured["session_key"] = session_key
        return object()

    async def fake_poll(server: object, timeout: int) -> None:
        return None

    monkeypatch.setattr("ach_agent.engine.lifecycle.launch", fake_launch)
    monkeypatch.setattr("ach_agent.engine.lifecycle.poll_ready", fake_poll)
    monkeypatch.setattr("ach_agent.engine.client.find_free_port", lambda: 12345)

    home = tmp_path / "home"
    cfg = EngineConfig(home=str(home))
    await poolmod._default_start_server(cfg, "k1")

    assert captured["home"] == home
    assert captured["session_key"] == "k1"
    assert home.is_dir()  # created if absent


def test_harness_log_dir_is_volatile_tmp() -> None:
    from ach_agent.main import _harness_log_dir

    d = _harness_log_dir()
    assert str(d).startswith("/tmp/")
    assert d.is_dir()


# ---------------------------------------------------------------------------
# _LRUSessionMap + EnginePool.oc_sessions (persistent session_key → ses_ map)
# ---------------------------------------------------------------------------


def test_lru_session_map_evicts_oldest() -> None:
    from ach_agent.engine.pool import _LRUSessionMap

    m = _LRUSessionMap(maxsize=2)
    m["a"] = "ses-a"
    m["b"] = "ses-b"
    m["c"] = "ses-c"  # exceeds maxsize → evicts "a" (oldest)
    assert "a" not in m
    assert m.get("b") == "ses-b"
    assert m.get("c") == "ses-c"


def test_lru_session_map_get_refreshes_recency() -> None:
    from ach_agent.engine.pool import _LRUSessionMap

    m = _LRUSessionMap(maxsize=2)
    m["a"] = "ses-a"
    m["b"] = "ses-b"
    assert m.get("a") == "ses-a"  # touch "a" → "b" is now oldest
    m["c"] = "ses-c"
    assert "a" in m
    assert "b" not in m


def test_lru_session_map_get_missing_returns_default() -> None:
    from ach_agent.engine.pool import _LRUSessionMap

    m = _LRUSessionMap(maxsize=2)
    assert m.get("nope") is None
    assert m.get("nope", "fallback") == "fallback"


def test_pool_owns_oc_sessions_map() -> None:
    from ach_agent.engine.pool import EnginePool, _LRUSessionMap

    pool = EnginePool()
    assert isinstance(pool.oc_sessions, _LRUSessionMap)
    assert len(pool.oc_sessions) == 0


# ---------------------------------------------------------------------------
# _SqliteSessionMap (session_key → ses_ persisted to state.db, disk-resident)
# ---------------------------------------------------------------------------


def test_sqlite_session_map_survives_reopen(tmp_path):
    """Values written before close() are present after a fresh open (restart)."""
    from ach_agent.engine.pool import _SqliteSessionMap

    db = tmp_path / "state.db"
    m = _SqliteSessionMap(db)
    m["gitlab:server:42"] = "ses-a"
    m["cron:nightly"] = "ses-b"
    m.close()

    m2 = _SqliteSessionMap(db)  # simulate a harness restart
    assert m2.get("gitlab:server:42") == "ses-a"
    assert m2.get("cron:nightly") == "ses-b"
    m2.close()


def test_sqlite_session_map_get_missing_returns_default(tmp_path):
    from ach_agent.engine.pool import _SqliteSessionMap

    m = _SqliteSessionMap(tmp_path / "state.db")
    assert m.get("nope") is None
    assert m.get("nope", "fallback") == "fallback"
    m.close()


def test_sqlite_session_map_evicts_lru_keeps_active(tmp_path):
    """Over maxsize → the LEAST-recently-USED row is evicted; a read-active key survives."""
    from ach_agent.engine.pool import _SqliteSessionMap

    db = tmp_path / "state.db"
    m = _SqliteSessionMap(db, maxsize=2)
    m["a"] = "1"
    m["b"] = "2"
    assert m.get("a") == "1"  # bump "a" → "b" is now least-recently-used
    m["c"] = "3"  # over cap → evicts "b", not the just-read "a"
    assert "a" in m
    assert "b" not in m
    assert m.get("c") == "3"
    m.close()


def test_sqlite_session_map_cap_bounds_row_count(tmp_path):
    from ach_agent.engine.pool import _SqliteSessionMap

    m = _SqliteSessionMap(tmp_path / "state.db", maxsize=3)
    for i in range(5):
        m[f"k{i}"] = f"v{i}"
    assert len(m) == 3  # only the 3 most-recently-used rows remain
    m.close()


def test_sqlite_session_map_pop_deletes_row(tmp_path):
    """pop() removes the row so it does not reappear after reopen."""
    from ach_agent.engine.pool import _SqliteSessionMap

    db = tmp_path / "state.db"
    m = _SqliteSessionMap(db)
    m["lane-1"] = "ses-a"
    assert m.pop("lane-1", None) == "ses-a"
    assert m.pop("lane-1", None) is None  # idempotent
    m.close()

    m2 = _SqliteSessionMap(db)
    assert m2.get("lane-1") is None
    m2.close()

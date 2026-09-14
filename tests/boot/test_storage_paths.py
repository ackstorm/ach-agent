import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from ach_agent.config.schema import CodememMemory


def _cfg(tmp_path: Path, *, home: str = "", work: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        persistence=SimpleNamespace(enabled=True, mount_path=str(tmp_path)),
        engine=SimpleNamespace(home=home, work_dir=work),
    )


def test_distributed_paths_have_three_roots_and_transfer_root(tmp_path: Path) -> None:
    from ach_agent.boot.paths import resolve_role_paths

    paths = resolve_role_paths(_cfg(tmp_path))

    assert paths.harness_state == tmp_path / "state"
    assert paths.engine_home == tmp_path / "home"
    assert paths.work_dir == tmp_path / "workspace"
    assert paths.transfer_root == Path("/run/ach-agent/transfer")


def test_nonpersistent_distributed_paths_use_ephemeral_agent_base() -> None:
    from ach_agent.boot.paths import resolve_role_paths

    cfg = SimpleNamespace(
        persistence=SimpleNamespace(enabled=False, mount_path="/ignored"),
        engine=SimpleNamespace(home="", work_dir=""),
    )
    paths = resolve_role_paths(cfg)
    assert paths.harness_state == Path("/tmp/ach-agent/state")
    assert paths.engine_home == Path("/tmp/ach-agent/home")
    assert paths.work_dir == Path("/tmp/ach-agent/workspace")


def test_standalone_persistent_defaults_remain_legacy(tmp_path: Path) -> None:
    from ach_agent.boot.paths import resolve_role_paths

    paths = resolve_role_paths(_cfg(tmp_path), split_mode=False)
    assert paths.engine_home == tmp_path / "home"
    assert paths.work_dir == tmp_path / "home" / "workspace"


def test_legacy_codemem_is_backed_up_into_batch_and_source_remains(tmp_path: Path) -> None:
    from ach_agent.boot.paths import (
        new_hydration_batch,
        resolve_role_paths,
        stage_legacy_codemem,
    )

    source = tmp_path / "state" / "codemem.db"
    source.parent.mkdir()
    with sqlite3.connect(source) as db:
        db.execute("create table memories (id integer)")
        db.execute("insert into memories values (1)")
    cfg = _cfg(tmp_path)
    cfg.memory = CodememMemory.model_validate({"type": "codemem", "codemem": {}})
    paths = resolve_role_paths(cfg)
    batch = new_hydration_batch(tmp_path / "transfer")
    target = stage_legacy_codemem(cfg, paths, batch, split_mode=True)

    assert target == str(tmp_path / "home" / "state" / "codemem.db")
    assert source.exists()
    with sqlite3.connect(batch / "codemem.db") as db:
        assert db.execute("select id from memories").fetchone() == (1,)


def test_repeated_codemem_handoff_preserves_existing_engine_database(tmp_path: Path) -> None:
    from ach_agent.boot.paths import (
        new_hydration_batch,
        resolve_role_paths,
        stage_legacy_codemem,
    )
    from ach_agent.engine.context import install_hydration, install_legacy_codemem

    source = tmp_path / "state" / "codemem.db"
    source.parent.mkdir()
    with sqlite3.connect(source) as db:
        db.execute("create table memories (id integer)")
        db.execute("insert into memories values (1)")
    cfg = _cfg(tmp_path)
    cfg.memory = CodememMemory.model_validate({"type": "codemem", "codemem": {}})
    paths = resolve_role_paths(cfg)
    home = tmp_path / "home"
    first = new_hydration_batch(tmp_path / "transfer")
    stage_legacy_codemem(cfg, paths, first, split_mode=True)
    install_hydration(first, home, home / "skills")
    install_legacy_codemem(first, home / "state" / "codemem.db")

    with sqlite3.connect(source) as db:
        db.execute("insert into memories values (2)")
    second = new_hydration_batch(tmp_path / "transfer")
    stage_legacy_codemem(cfg, paths, second, split_mode=True)
    install_hydration(second, home, home / "skills")
    install_legacy_codemem(second, home / "state" / "codemem.db")

    with sqlite3.connect(home / "state" / "codemem.db") as db:
        assert db.execute("select id from memories order by id").fetchall() == [(1,)]


def test_explicit_distributed_codemem_outside_home_is_rejected(tmp_path: Path) -> None:
    from ach_agent.boot.paths import (
        new_hydration_batch,
        resolve_role_paths,
        stage_legacy_codemem,
    )

    cfg = _cfg(tmp_path)
    cfg.memory = CodememMemory.model_validate(
        {"type": "codemem", "codemem": {"dbPath": str(tmp_path / "state" / "x.db")}}
    )
    with pytest.raises(ValueError, match="within engine.home"):
        stage_legacy_codemem(
            cfg,
            resolve_role_paths(cfg),
            new_hydration_batch(tmp_path / "transfer"),
            split_mode=True,
        )


def test_distributed_paths_reject_engine_home_outside_root(tmp_path: Path) -> None:
    from ach_agent.boot.paths import resolve_role_paths

    with pytest.raises(ValueError, match="engine.home"):
        resolve_role_paths(_cfg(tmp_path, home=str(tmp_path / "elsewhere")))


def test_distributed_paths_reject_work_dir_outside_root(tmp_path: Path) -> None:
    from ach_agent.boot.paths import resolve_role_paths

    with pytest.raises(ValueError, match="engine.workDir"):
        resolve_role_paths(_cfg(tmp_path, work=str(tmp_path / "elsewhere")))


def test_path_resolution_is_pure(tmp_path: Path) -> None:
    old = tmp_path / "home" / "workspace"
    old.mkdir(parents=True)
    (old / "session-data").write_text("keep")

    from ach_agent.boot.paths import resolve_role_paths

    paths = resolve_role_paths(_cfg(tmp_path))
    assert paths.work_dir == tmp_path / "workspace"
    assert (old / "session-data").read_text() == "keep"

from pathlib import Path
from types import SimpleNamespace


def _cfg(tmp_path: Path, *, home: str = "", work: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        persistence=SimpleNamespace(enabled=True, mount_path=str(tmp_path)),
        engine=SimpleNamespace(home=home, work_dir=work),
    )


def test_standalone_persistent_defaults_remain_legacy(tmp_path: Path) -> None:
    from ach_agent.boot.paths import resolve_role_paths

    paths = resolve_role_paths(_cfg(tmp_path))
    assert paths.engine_home == tmp_path / "home"
    assert paths.work_dir == tmp_path / "home" / "workspace"


def test_path_resolution_is_pure(tmp_path: Path) -> None:
    old = tmp_path / "home" / "workspace"
    old.mkdir(parents=True)
    (old / "session-data").write_text("keep")

    from ach_agent.boot.paths import resolve_role_paths

    paths = resolve_role_paths(_cfg(tmp_path))
    assert paths.work_dir == tmp_path / "home" / "workspace"
    assert (old / "session-data").read_text() == "keep"


def test_nested_explicit_workspace_keeps_original_tree_without_relocation(tmp_path: Path) -> None:
    from ach_agent.boot.paths import resolve_role_paths

    old = tmp_path / "home" / "workspace"
    nested = old / "session-keyed"
    nested.mkdir(parents=True)
    (nested / "session.jsonl").write_text("history", encoding="utf-8")
    cfg = _cfg(tmp_path, work=str(nested))

    paths = resolve_role_paths(cfg)

    assert paths.work_dir == nested
    assert (nested / "session.jsonl").read_text(encoding="utf-8") == "history"
    assert not (tmp_path / "workspace").exists()

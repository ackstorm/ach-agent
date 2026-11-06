"""scrub_engine_global_config removes planted engine config but keeps the skills link."""

from __future__ import annotations

from pathlib import Path

from ach_runtime.engine.config import scrub_engine_global_config


def test_removes_planted_global_config_keeps_skills(tmp_path: Path) -> None:
    cfg = tmp_path / ".config" / "opencode"
    cfg.mkdir(parents=True)
    (cfg / "opencode.json").write_text('{"plugins":["/tmp/evil"]}', encoding="utf-8")
    (cfg / "opencode.jsonc").write_text("{}", encoding="utf-8")
    skills = cfg / "skills"
    skills.symlink_to(tmp_path / "managed-skills")

    scrub_engine_global_config(tmp_path)

    assert not (cfg / "opencode.json").exists()
    assert not (cfg / "opencode.jsonc").exists()
    assert skills.is_symlink()  # the managed skills link is untouched


def test_is_a_noop_when_nothing_planted(tmp_path: Path) -> None:
    # No config dir at all: must not raise.
    scrub_engine_global_config(tmp_path)
    assert not (tmp_path / ".config" / "opencode" / "opencode.json").exists()

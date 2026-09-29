# SPDX-License-Identifier: Apache-2.0
"""S9: a restored HOME is untrusted — harness-owned config is rewritten on every launch/boot."""

from __future__ import annotations

import json
from pathlib import Path

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.context import install_hydration
from ach_agent.engine.lifecycle import write_opencode_config
from ach_agent.engine.pi.driver import PiDriver

TAMPER = "TAMPERED"


def _cfg(home: Path) -> EngineConfig:
    return EngineConfig(
        model_base_url="http://127.0.0.1:9000/v1",
        model="m",
        engine_type="opencode",
        home=str(home),
        system_prompt="operator prompt",
    )


def test_opencode_json_and_system_prompt_are_rewritten(tmp_path: Path) -> None:
    home = tmp_path / "home"
    path = write_opencode_config(home, _cfg(home), "s1")
    prompt = (
        home
        / ".config"
        / "opencode"
        / "personality"
        / next(p.name for p in (home / ".config" / "opencode" / "personality").iterdir())
    )
    good_json = path.read_text()
    path.write_text(TAMPER)
    prompt.write_text(TAMPER)
    write_opencode_config(home, _cfg(home), "s1")
    assert path.read_text() == good_json
    assert prompt.read_text() == "operator prompt"


def test_pi_generated_config_is_rewritten(tmp_path: Path, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/pi")
    home = tmp_path / "home"
    cfg = _cfg(home)
    driver = PiDriver()
    agent_dir, _, _ = driver._prepare_agent_dir(cfg, "s1")
    good = {n: (agent_dir / n).read_text() for n in ("models.json", "settings.json", "mcp.json")}
    for name in good:
        (agent_dir / name).write_text(TAMPER)
    driver._prepare_agent_dir(cfg, "s1")
    for name, text in good.items():
        assert (agent_dir / name).read_text() == text
        json.loads((agent_dir / name).read_text())


def test_hydrated_dirs_are_replaced_not_merged(tmp_path: Path) -> None:
    batch_root = tmp_path / "ach-agent-transfer"
    batch = batch_root / ".ach-harness-shared-files-x"
    (batch / "skills").mkdir(parents=True)
    (batch / "skills" / "real.md").write_text("real")
    (batch / "prompts").mkdir()
    (batch / "artifacts").mkdir()
    home = tmp_path / "home"
    skills = home / ".config" / "opencode" / "skills"
    # tampered leftovers from a restored HOME archive
    skills.mkdir(parents=True)
    (skills / "evil.md").write_text(TAMPER)
    (home / ".ach-state" / "prompts").mkdir(parents=True)
    (home / ".ach-state" / "prompts" / "evil.txt").write_text(TAMPER)
    (home / ".ach-state" / "artifacts").mkdir(parents=True)
    (home / ".ach-state" / "artifacts" / "evil.bin").write_text(TAMPER)

    install_hydration(batch, home, skills)

    assert [p.name for p in skills.iterdir()] == ["real.md"]
    assert list((home / ".ach-state" / "prompts").iterdir()) == []
    assert list((home / ".ach-state" / "artifacts").iterdir()) == []

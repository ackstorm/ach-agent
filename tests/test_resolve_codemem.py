# SPDX-License-Identifier: Apache-2.0
"""Unit tests for resolve_codemem_wiring (ach_agent.main).

Covers the five resolution cases per the boot-helper contract:
  (a) persistence-enabled derivation of db_path
  (b) persistence-disabled derivation → /tmp/ach-home/codemem/codemem.db
  (c) explicit dbPath + project override
  (d) hindsight memory → ("", "")  — not codemem, pass-through
  (e) codemem config but binary not on PATH → ("", "") fail-open
"""

from __future__ import annotations

import pytest

from ach_agent.config.schema import AgentConfig

# ---------------------------------------------------------------------------
# Minimal AgentConfig factory
# ---------------------------------------------------------------------------

_BASE: dict = {
    "schemaVersion": "1",
    "agent": {"name": "a"},
    "model": {"name": "openai.gpt-5", "type": "openai"},
    "capability": {"ach": {"baseUrl": "https://ach.example.com"}},
}


def _cfg(memory: dict | None = None, persistence: dict | None = None) -> AgentConfig:
    raw: dict = dict(_BASE)
    if memory is not None:
        raw["memory"] = memory
    if persistence is not None:
        raw["persistence"] = persistence
    return AgentConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# (a) persistence-enabled derivation
# ---------------------------------------------------------------------------


def test_resolve_derives_db_path_from_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """When db_path is None and persistence.enabled, db_path derives to <mountPath>/codemem/codemem.db."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    from ach_agent.main import resolve_codemem_wiring

    cfg = _cfg(
        memory={"type": "codemem", "codemem": {}},
        persistence={"enabled": True, "mountPath": "/var/lib/ach-agent"},
    )
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == "/var/lib/ach-agent/codemem/codemem.db"
    assert project == "ach-agent"


# ---------------------------------------------------------------------------
# (b) persistence-disabled derivation → /tmp/ach-home
# ---------------------------------------------------------------------------


def test_resolve_derives_db_path_tmp_when_no_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """When db_path is None and persistence.enabled=False, db_path derives to /tmp/ach-home/codemem/codemem.db."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    from ach_agent.main import resolve_codemem_wiring

    cfg = _cfg(
        memory={"type": "codemem", "codemem": {}},
        persistence={"enabled": False},
    )
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == "/tmp/ach-home/codemem/codemem.db"
    assert project == "ach-agent"


# ---------------------------------------------------------------------------
# (c) explicit dbPath + project override
# ---------------------------------------------------------------------------


def test_resolve_respects_explicit_db_path_and_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit dbPath and project in config take precedence over derivation."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/local/bin/codemem")

    from ach_agent.main import resolve_codemem_wiring

    cfg = _cfg(
        memory={
            "type": "codemem",
            "codemem": {"dbPath": "/data/my-agent.db", "project": "my-project"},
        }
    )
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == "/data/my-agent.db"
    assert project == "my-project"


# ---------------------------------------------------------------------------
# (d) hindsight memory → ("", "")
# ---------------------------------------------------------------------------


def test_resolve_returns_empty_for_hindsight(monkeypatch: pytest.MonkeyPatch) -> None:
    """HindsightMemory is not codemem → resolve_codemem_wiring returns ('', '')."""
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    from ach_agent.main import resolve_codemem_wiring

    cfg = _cfg(
        memory={
            "type": "hindsight",
            "hindsight": {"endpoint": "http://hindsight:8080"},
        }
    )
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == ""
    assert project == ""


# ---------------------------------------------------------------------------
# (e) codemem config but binary absent → fail-open ("", "")
# ---------------------------------------------------------------------------


def test_resolve_returns_empty_when_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """codemem configured but `codemem` not on PATH → fail-open: ('', '') returned."""
    monkeypatch.setattr("shutil.which", lambda name: None)

    from ach_agent.main import resolve_codemem_wiring

    cfg = _cfg(memory={"type": "codemem", "codemem": {}})
    db_path, project = resolve_codemem_wiring(cfg)

    assert db_path == ""
    assert project == ""

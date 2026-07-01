# SPDX-License-Identifier: Apache-2.0
"""Tests for memory.type discriminated union (hindsight|codemem).

TDD tests for Task 1: HindsightMemory, CodememMemory, and backward-compat
field_validator on AgentConfig. All sub-model cases test the concrete classes
directly (no helpers.py needed); the backward-compat case uses the fixture
+ _load_raw round-trip to exercise the full load_config path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

# Path to the fixtures directory relative to this file
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _read_fixture(name: str) -> dict:
    """Read a fixture file into a mutable dict (for negative-test mutation)."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _load_raw(tmp_path: Path, raw: dict):
    """Write a raw config dict to a temp file and load it via load_config."""
    from ach_agent.config import load_config

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(str(config_file))


# ---------------------------------------------------------------------------
# HindsightMemory sub-model (direct)
# ---------------------------------------------------------------------------


def test_hindsight_memory_direct() -> None:
    """HindsightMemory validates endpoint, bank, mentalModels with default type."""
    from ach_agent.config.schema import HindsightMemory

    m = HindsightMemory.model_validate(
        {"endpoint": "http://mem:8080", "bank": "gitlab-pr-review", "mentalModels": ["m1"]}
    )
    assert m.type == "hindsight"
    assert m.endpoint == "http://mem:8080"
    assert m.bank == "gitlab-pr-review"
    assert m.mental_models == ["m1"]


# ---------------------------------------------------------------------------
# CodememMemory sub-model (direct)
# ---------------------------------------------------------------------------


def test_codemem_memory_loads() -> None:
    """CodememMemory validates with explicit type and absolute dbPath."""
    from ach_agent.config.schema import CodememMemory

    m = CodememMemory.model_validate(
        {"type": "codemem", "dbPath": "/var/lib/codemem/agent.db"}
    )
    assert m.type == "codemem"
    assert m.db_path == "/var/lib/codemem/agent.db"
    assert m.mission == ""


def test_codemem_rejects_relative_db_path() -> None:
    """CodememMemory rejects a relative dbPath (no leading slash)."""
    from ach_agent.config.schema import CodememMemory

    with pytest.raises(ValidationError, match="absolute"):
        CodememMemory.model_validate({"type": "codemem", "dbPath": "relative/path.db"})


def test_codemem_rejects_dotdot_in_db_path() -> None:
    """CodememMemory rejects a dbPath containing '..' even when rooted."""
    from ach_agent.config.schema import CodememMemory

    with pytest.raises(ValidationError, match="absolute"):
        CodememMemory.model_validate({"type": "codemem", "dbPath": "/var/lib/../escape.db"})


def test_codemem_rejects_hindsight_only_fields() -> None:
    """extra='forbid': bank is not a codemem field — ValidationError expected."""
    from ach_agent.config.schema import CodememMemory

    with pytest.raises(ValidationError):
        CodememMemory.model_validate(
            {"type": "codemem", "dbPath": "/var/lib/codemem/a.db", "bank": "x"}
        )


# ---------------------------------------------------------------------------
# Backward compat: legacy memory block without `type` → hindsight
# ---------------------------------------------------------------------------


def test_legacy_block_without_type_loads_as_hindsight(tmp_path: Path) -> None:
    """A legacy memory block with no `type` key is defaulted to hindsight.

    Uses the fixture + _load_raw round-trip to exercise the full load_config
    path including the @field_validator("memory", mode="before") on AgentConfig.
    """
    from ach_agent.config.schema import HindsightMemory

    raw = _read_fixture("config_webhook.json")
    # Legacy shape: no `type` key — operator pre-v1 configs render this form.
    raw["memory"] = {
        "endpoint": "http://mem:8080",
        "bank": "gitlab-pr-review",
        "mentalModels": ["m1"],
    }
    cfg = _load_raw(tmp_path, raw)
    assert isinstance(cfg.memory, HindsightMemory)
    assert cfg.memory.type == "hindsight"
    assert cfg.memory.endpoint == "http://mem:8080"
    assert cfg.memory.bank == "gitlab-pr-review"

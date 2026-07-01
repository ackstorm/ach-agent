# SPDX-License-Identifier: Apache-2.0
"""Tests for memory.type discriminated union (hindsight|codemem).

TDD tests for Task 1: HindsightMemory, CodememMemory strict nested schema.
All sub-model cases test the concrete classes directly (no helpers.py needed);
the legacy-rejection case uses the fixture + _load_raw round-trip to exercise
the full load_config path.
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
    """HindsightMemory validates with type, hindsight sub-block, endpoint, bank, mentalModels."""
    from ach_agent.config.schema import HindsightMemory

    m = HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {
                "endpoint": "http://mem:8080",
                "bank": "gitlab-pr-review",
                "mentalModels": ["m1"],
            },
        }
    )
    assert m.type == "hindsight"
    assert m.hindsight.endpoint == "http://mem:8080"
    assert m.hindsight.bank == "gitlab-pr-review"
    assert m.hindsight.mental_models == ["m1"]


# ---------------------------------------------------------------------------
# CodememMemory sub-model (direct)
# ---------------------------------------------------------------------------


def test_codemem_memory_loads() -> None:
    """CodememMemory validates with explicit type and codemem sub-block with absolute dbPath."""
    from ach_agent.config.schema import CodememMemory

    m = CodememMemory.model_validate(
        {
            "type": "codemem",
            "codemem": {"dbPath": "/var/lib/codemem/agent.db", "project": "ach-agent"},
        }
    )
    assert m.type == "codemem"
    assert m.codemem.db_path == "/var/lib/codemem/agent.db"
    assert m.codemem.project == "ach-agent"


def test_codemem_rejects_relative_db_path() -> None:
    """CodememMemory rejects a relative dbPath (no leading slash)."""
    from ach_agent.config.schema import CodememMemory

    with pytest.raises(ValidationError, match="absolute"):
        CodememMemory.model_validate(
            {"type": "codemem", "codemem": {"dbPath": "relative/path.db", "project": "test"}}
        )


def test_codemem_rejects_dotdot_in_db_path() -> None:
    """CodememMemory rejects a dbPath containing '..' even when rooted."""
    from ach_agent.config.schema import CodememMemory

    with pytest.raises(ValidationError, match="absolute"):
        CodememMemory.model_validate(
            {"type": "codemem", "codemem": {"dbPath": "/var/lib/../escape.db", "project": "test"}}
        )


def test_codemem_rejects_missing_project() -> None:
    """CodememMemory requires project field — omitting it must raise ValidationError."""
    from ach_agent.config.schema import CodememMemory

    with pytest.raises(ValidationError):
        CodememMemory.model_validate(
            {"type": "codemem", "codemem": {"dbPath": "/var/lib/codemem/agent.db"}}
        )


def test_codemem_rejects_hindsight_only_fields() -> None:
    """extra='forbid': flat dbPath without the codemem: sub-block is rejected by CodememMemory."""
    from ach_agent.config.schema import CodememMemory

    with pytest.raises(ValidationError):
        CodememMemory.model_validate({"type": "codemem", "dbPath": "/var/lib/codemem/a.db"})


# ---------------------------------------------------------------------------
# Strict: legacy memory block without `type` now raises ValidationError
# ---------------------------------------------------------------------------


def test_legacy_block_without_type_raises(tmp_path: Path) -> None:
    """A legacy memory block with no `type` key now hard-fails (no backward-compat coercion).

    The strict nested schema requires an explicit ``type`` discriminator; omitting it
    causes a ValidationError in load_config → sys.exit(1) (SystemExit).
    """
    raw = _read_fixture("config_webhook.json")
    # Legacy shape: no `type` key — was formerly defaulted to hindsight; now rejected.
    raw["memory"] = {
        "endpoint": "http://mem:8080",
        "bank": "gitlab-pr-review",
        "mentalModels": ["m1"],
    }
    with pytest.raises(SystemExit):
        _load_raw(tmp_path, raw)

# SPDX-License-Identifier: Apache-2.0
"""Drift guard for the frozen JSON Schema v1 artifact (docs/plan/agent-config.schema.json).

The artifact is GENERATED from ``AgentConfig`` (scripts/gen_schema.py); the Pydantic model is
the single source of truth. These tests fail if the committed .json falls out of sync with the
model, and prove the schema actually accepts the rendered contract fixtures.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

_REPO = Path(__file__).resolve().parents[2]
_ARTIFACT = _REPO / "docs" / "schemas" / "agent-config-v1.schema.json"
_FIXTURES = sorted((_REPO / "tests" / "config" / "fixtures").glob("config_*.json"))


def _load_gen_schema():
    """Import scripts/gen_schema.py by path (scripts/ is not an importable package)."""
    spec = importlib.util.spec_from_file_location("gen_schema", _REPO / "scripts" / "gen_schema.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_artifact_matches_generated() -> None:
    """The committed artifact is byte-identical to a fresh render (run gen_schema.py to fix)."""
    gen = _load_gen_schema()
    assert _ARTIFACT.read_text(encoding="utf-8") == gen.render(), (
        "agent-config.schema.json is stale — regenerate: uv run python scripts/gen_schema.py"
    )


def test_artifact_is_valid_json_schema() -> None:
    """The artifact is itself a well-formed Draft 2020-12 schema."""
    schema = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


@pytest.mark.parametrize("fixture", _FIXTURES, ids=lambda p: p.name)
def test_rendered_fixtures_validate_against_schema(fixture: Path) -> None:
    """Every rendered-contract fixture (what the operator emits) passes the frozen schema."""
    schema = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    instance = json.loads(fixture.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(instance)


def test_model_thinking_schema_replaces_pi_capability_surface() -> None:
    schema = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert "PiModelCapabilities" not in schema["$defs"]
    assert set(schema["$defs"]["PiEngineBlock"]["properties"]) == {
        "binaryPath",
        "mcpAdapterPath",
    }
    thinking = schema["$defs"]["ThinkingBlock"]["properties"]
    assert thinking["enabled"]["type"] == "boolean"
    effort = thinking["effort"]
    assert {
        "enum": ["minimal", "low", "medium", "high", "xhigh"],
        "type": "string",
    } in effort["anyOf"]
    assert {"type": "null"} in effort["anyOf"]
    model_props = schema["$defs"]["ModelBlock"]["properties"]
    assert model_props["thinking"]["$ref"].endswith("ThinkingBlock")

# SPDX-License-Identifier: MIT
"""Shared fixtures for tests/execution — reuses tests/config's accepted PR-review wire."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ach_runtime.config.public import PublicConfig, public_config
from ach_runtime.config.schema import RuntimeConfig

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


@pytest.fixture
def public_config_fixture() -> PublicConfig:
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config = RuntimeConfig.model_validate(wire)
    return public_config(config, engine_env=config.engine.env)

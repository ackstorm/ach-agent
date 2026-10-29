# SPDX-License-Identifier: MIT
"""Shared fixtures for tests/config — the authoritative PR-review wire fixture."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PR_REVIEW_FIXTURE_PATH = FIXTURES_DIR / "pr-review-runtime.json"


@pytest.fixture
def pr_review_wire() -> dict[str, Any]:
    """A fresh deep copy of the complete representative PR-review RuntimeConfig wire."""
    with PR_REVIEW_FIXTURE_PATH.open(encoding="utf-8") as f:
        return copy.deepcopy(json.load(f))

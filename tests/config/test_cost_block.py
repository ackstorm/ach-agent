# SPDX-License-Identifier: Apache-2.0
"""CostBlock: the Literal is what delivers spec §1.2's boot hard-fail (AC-1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import AgentConfig, CostBlock


def _base() -> dict:
    return {
        "schemaVersion": "1",
        "agent": {"name": "a"},
        "model": {"name": "m", "type": "openai"},
        "capability": {"type": "ach", "ach": {"baseUrl": "https://ach"}},
    }


def test_cost_defaults_to_engine() -> None:
    cfg = AgentConfig.model_validate(_base())
    assert cfg.cost.source == "engine"


@pytest.mark.parametrize("src", ["engine", "litellm_usage", "litellm_headers", "none"])
def test_cost_accepts_every_documented_source(src: str) -> None:
    cfg = AgentConfig.model_validate({**_base(), "cost": {"source": src}})
    assert cfg.cost.source == src


def test_unknown_source_hard_fails() -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**_base(), "cost": {"source": "litellm_spend"}})


def test_cost_block_forbids_extra_keys() -> None:
    with pytest.raises(ValidationError):
        CostBlock.model_validate({"source": "engine", "prices": {}})

# SPDX-License-Identifier: Apache-2.0
"""Registry maps memory.type -> backend module; tools_spec_for returns the right spec."""
from __future__ import annotations


def test_tools_spec_for_hindsight() -> None:
    from ach_agent.config.schema import HindsightMemory
    from ach_agent.memory import tools_spec_for

    cfg = HindsightMemory.model_validate(
        {"type": "hindsight", "hindsight": {"endpoint": "http://m:8080", "bank": "b"}}
    )
    spec = tools_spec_for(cfg)
    assert "memory_recall" in spec and "memory_retain" in spec


def test_tools_spec_for_codemem() -> None:
    from ach_agent.config.schema import CodememMemory
    from ach_agent.memory import tools_spec_for

    cfg = CodememMemory.model_validate({"type": "codemem"})
    spec = tools_spec_for(cfg)
    assert "memory_search" in spec and "memory_remember" in spec


def test_tools_spec_for_none_is_empty() -> None:
    from ach_agent.memory import tools_spec_for

    assert tools_spec_for(None) == ""

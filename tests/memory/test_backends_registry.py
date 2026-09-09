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


def test_tools_spec_for_ach_memory() -> None:
    """Registration is what puts TOOLS_SPEC in the system prompt — an unregistered backend
    ships an agent that was never told how to call its own memory tools."""
    from ach_agent.config.schema import AchMemoryMemory
    from ach_agent.memory import tools_spec_for

    cfg = AchMemoryMemory.model_validate(
        {"type": "ach-memory", "achMemory": {"endpoint": "http://m:8000"}}
    )
    spec = tools_spec_for(cfg)
    assert "memory_recall" in spec and "memory_retain" in spec
    assert "memory_type" in spec  # the typed-retain contract actually reached the prompt


def test_every_union_arm_is_registered() -> None:
    """A new memory.type that nobody registers fails silently — tools_spec_for returns ''."""
    from ach_agent.config.schema import Memory
    from ach_agent.memory import MEMORY_BACKENDS

    arms = {arm.model_fields["type"].annotation.__args__[0] for arm in Memory.__args__[0].__args__}
    assert arms == set(MEMORY_BACKENDS)

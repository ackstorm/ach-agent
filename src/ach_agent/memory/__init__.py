# SPDX-License-Identifier: Apache-2.0
"""Memory package — backend registry and tools_spec_for helper."""

from __future__ import annotations

from ach_agent.memory import codemem, hindsight

MEMORY_BACKENDS = {hindsight.TYPE: hindsight, codemem.TYPE: codemem}


def tools_spec_for(memory_cfg: object | None) -> str:
    """Return the active backend's TOOLS_SPEC, or '' if memory is None/unknown."""
    if memory_cfg is None:
        return ""
    backend = MEMORY_BACKENDS.get(getattr(memory_cfg, "type", ""))
    return backend.TOOLS_SPEC if backend is not None else ""

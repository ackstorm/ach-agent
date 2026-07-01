# SPDX-License-Identifier: Apache-2.0
"""Backend protocol contract for ach-agent memory backends.

Each backend module must expose at module level:
  TYPE: str       — the config discriminator value (e.g. "hindsight", "codemem")
  TOOLS_SPEC: str — prose appended to the boot-static system prompt under ## Memory Tools
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class MemoryBackend(Protocol):
    """Structural protocol every backend module satisfies implicitly."""

    TYPE: str
    TOOLS_SPEC: str

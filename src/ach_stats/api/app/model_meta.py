# SPDX-License-Identifier: Apache-2.0
"""Static model -> (provider, tag) map. provider/tag are metadata, never measured (spec §4.4)."""

from __future__ import annotations

_META: dict[str, tuple[str, str | None]] = {
    "claude-opus-4-8": ("Anthropic", "Frontier"),
    "claude-fable-5": ("Anthropic", "Mythos-tier"),
    "claude-sonnet-5": ("Anthropic", "Balanced"),
    "glm-5-2": ("Zhipu AI", "Open Weight"),
}


def resolve(model: str) -> tuple[str, str | None]:
    return _META.get(model, ("unknown", None))

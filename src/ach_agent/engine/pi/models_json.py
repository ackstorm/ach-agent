# SPDX-License-Identifier: Apache-2.0
"""Build Pi's models.json using the localhost model proxy."""

from __future__ import annotations

from typing import Any

from ach_agent.engine.base.driver import EngineConfig

_PI_PROVIDER_BY_TYPE: dict[str, tuple[str, str]] = {
    "openai": ("ach-openai", "openai-completions"),
    "gemini": ("ach-gemini", "google-generative-ai"),
    "anthropic": ("ach-anthropic", "anthropic-messages"),
}


def build_models_json(cfg: EngineConfig) -> tuple[dict[str, Any], str]:
    """Return the models document and provider name passed to Pi."""
    provider, api = _PI_PROVIDER_BY_TYPE.get(cfg.model_type, _PI_PROVIDER_BY_TYPE["openai"])
    doc: dict[str, Any] = {
        provider: {
            "api": api,
            "baseUrl": cfg.model_base_url,
            "apiKey": "local-proxy",
            "headers": {},
        }
    }
    return doc, provider

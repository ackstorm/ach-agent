# SPDX-License-Identifier: Apache-2.0
from app.model_meta import resolve


def test_known_models():
    assert resolve("claude-opus-4-8") == ("Anthropic", "Frontier")
    assert resolve("claude-sonnet-5") == ("Anthropic", "Balanced")
    assert resolve("glm-5-2") == ("Zhipu AI", "Open Weight")


def test_unknown_model():
    assert resolve("mystery-model-9") == ("unknown", None)

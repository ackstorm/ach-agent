# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.models_json import build_models_json


def test_provider_api_mapping_and_no_ek(monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_secret")
    cfg = EngineConfig(
        model="gemini-flash-latest",
        model_type="gemini",
        model_base_url="http://127.0.0.1:9001/gemini/v1beta",
    )
    doc, provider = build_models_json(cfg)
    blob = json.dumps(doc)
    assert "ek_secret" not in blob
    provider_doc = doc["providers"][provider]
    assert provider_doc["api"] == "google-generative-ai"
    assert provider_doc["baseUrl"] == "http://127.0.0.1:9001/gemini/v1beta"
    assert provider_doc["apiKey"] == "local-proxy"
    assert provider_doc["headers"] == {}


def test_openai_and_anthropic_api_kinds() -> None:
    doc_o, provider_o = build_models_json(
        EngineConfig(model_type="openai", model_base_url="http://x/v1")
    )
    doc_a, provider_a = build_models_json(
        EngineConfig(model_type="anthropic", model_base_url="http://x/anthropic")
    )
    assert doc_o["providers"][provider_o]["api"] == "openai-completions"
    assert doc_a["providers"][provider_a]["api"] == "anthropic-messages"

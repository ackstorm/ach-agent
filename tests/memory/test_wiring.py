# SPDX-License-Identifier: Apache-2.0
import types

import pytest

import ach_agent.main as m
from ach_agent.config.schema import HindsightMemory


def _cfg():
    return HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {
                "endpoint": "https://hs/mcp",
                "bank": "b",
                "auth": {"env": "HS_TOK"},
                "mentalModels": [],
            },
        }
    )


@pytest.mark.asyncio
async def test_wiring_returns_facade_url_not_endpoint(monkeypatch):
    async def fake_prepare(cfg):
        return True, "## Memory\n\nok"

    monkeypatch.setattr(m, "prepare_memory", fake_prepare)
    servers, prompt = await m.select_memory_wiring_async(_cfg(), "http://127.0.0.1:9/mcp")
    assert servers == ["http://127.0.0.1:9/mcp"]  # facade URL, NOT the hindsight endpoint
    assert prompt == "## Memory\n\nok"


@pytest.mark.asyncio
async def test_wiring_empty_when_unavailable(monkeypatch):
    async def fake_prepare(cfg):
        return False, "## Memory\n\nUnavailable"

    monkeypatch.setattr(m, "prepare_memory", fake_prepare)
    servers, _ = await m.select_memory_wiring_async(_cfg(), "http://127.0.0.1:9/mcp")
    assert servers == []


def test_memory_auth_env_collected_for_forward_env_strip():
    """SECURITY: the memory admin secret env NAME must be collected so it's stripped from
    engine.forwardEnv + redacted from logs — same as webhook/a2a secrets."""
    cfg = types.SimpleNamespace(channels=[], memory=_cfg())  # _cfg() has auth={env:HS_TOK}
    assert "HS_TOK" in m.collect_secret_env_names(cfg)


def test_memory_no_auth_collects_nothing():
    mem = HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {"endpoint": "http://hs/mcp", "bank": "b", "mentalModels": []},
        }
    )
    cfg = types.SimpleNamespace(channels=[], memory=mem)
    assert m.collect_secret_env_names(cfg) == []

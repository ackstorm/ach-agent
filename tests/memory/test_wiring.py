# SPDX-License-Identifier: Apache-2.0
import types

import pytest

import ach_agent.boot.engine_runner as engine_runner_mod
from ach_agent.boot.secrets import collect_secret_env_names
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

    monkeypatch.setattr(engine_runner_mod, "prepare_memory", fake_prepare)
    servers, prompt = await engine_runner_mod.select_memory_wiring_async(
        _cfg(), "http://127.0.0.1:9/mcp"
    )
    assert servers == ["http://127.0.0.1:9/mcp"]  # facade URL, NOT the hindsight endpoint
    assert prompt == "## Memory\n\nok"


@pytest.mark.asyncio
async def test_wiring_empty_when_unavailable(monkeypatch):
    async def fake_prepare(cfg):
        return False, "## Memory\n\nUnavailable"

    monkeypatch.setattr(engine_runner_mod, "prepare_memory", fake_prepare)
    servers, _ = await engine_runner_mod.select_memory_wiring_async(
        _cfg(), "http://127.0.0.1:9/mcp"
    )
    assert servers == []


def test_memory_auth_env_collected_for_forward_env_strip():
    """SECURITY: the memory admin secret env NAME must be collected so it's stripped from
    engine.forwardEnv + redacted from logs — same as webhook/a2a secrets."""
    cfg = types.SimpleNamespace(channels=[], memory=_cfg())  # _cfg() has auth={env:HS_TOK}
    assert "HS_TOK" in collect_secret_env_names(cfg)


def test_memory_no_auth_collects_nothing():
    mem = HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {"endpoint": "http://hs/mcp", "bank": "b", "mentalModels": []},
        }
    )
    cfg = types.SimpleNamespace(channels=[], memory=mem)
    assert collect_secret_env_names(cfg) == []


# ---------------------------------------------------------------------------
# ach-memory arm
# ---------------------------------------------------------------------------


def _ach_cfg(**kw):
    from ach_agent.config.schema import AchMemoryMemory

    return AchMemoryMemory.model_validate(
        {
            "type": "ach-memory",
            "achMemory": {"endpoint": "http://ach-memory:8000", "auth": {"env": "AM_TOK"}, **kw},
        }
    )


@pytest.mark.asyncio
async def test_ach_memory_wiring_returns_facade_url_not_endpoint(monkeypatch):
    seen = {}

    async def fake_prepare(cfg, project):
        seen["project"] = project
        return True, "## Memory\n\nok"

    monkeypatch.setattr(engine_runner_mod, "prepare_ach_memory", fake_prepare)
    servers, prompt = await engine_runner_mod.select_memory_wiring_async(
        _ach_cfg(), "http://127.0.0.1:9/mcp", "ach-gitlab-pr"
    )
    assert servers == ["http://127.0.0.1:9/mcp"]  # facade URL, NOT the ach-memory endpoint
    assert prompt == "## Memory\n\nok"
    assert seen["project"] == "ach-gitlab-pr"  # boot-static, not derived from the event


@pytest.mark.asyncio
async def test_ach_memory_wiring_empty_when_unavailable(monkeypatch):
    """D-02 fail-open, unchanged for the new backend."""

    async def fake_prepare(cfg, project):
        return False, "## Memory\n\nUnavailable"

    monkeypatch.setattr(engine_runner_mod, "prepare_ach_memory", fake_prepare)
    servers, prompt = await engine_runner_mod.select_memory_wiring_async(
        _ach_cfg(), "http://127.0.0.1:9/mcp", "ach-gitlab-pr"
    )
    assert servers == []
    assert "Unavailable" in prompt


def test_ach_memory_auth_env_collected_for_forward_env_strip():
    """SECURITY: the ach-memory user key env NAME must be collected so it is stripped from
    engine.forwardEnv and redacted from logs — same path as the hindsight admin secret."""
    cfg = types.SimpleNamespace(channels=[], memory=_ach_cfg())
    assert "AM_TOK" in collect_secret_env_names(cfg)

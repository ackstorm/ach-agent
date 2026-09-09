# SPDX-License-Identifier: Apache-2.0
import types

import pytest

import ach_agent.boot.engine_runner as engine_runner_mod
from ach_agent.boot.secrets import collect_secret_env_names

# ---------------------------------------------------------------------------
# ach-memory arm
# ---------------------------------------------------------------------------


def _ach_cfg(**kw):
    from ach_agent.config.schema import AchMemoryMemory

    return AchMemoryMemory.model_validate(
        {
            "type": "ach-memory",
            "achMemory": {
                "endpoint": "http://ach-memory:8000/mcp/",
                "auth": {"type": "bearer", "env": "AM_TOK"},
                **kw,
            },
        }
    )


@pytest.mark.asyncio
async def test_ach_memory_wiring_returns_facade_url_not_endpoint(monkeypatch):
    seen = {}

    async def fake_prepare(cfg, project, headers):
        seen["project"], seen["headers"] = project, headers
        return True, "## Memory\n\nok"

    monkeypatch.setattr(engine_runner_mod, "prepare_ach_memory", fake_prepare)
    servers, prompt = await engine_runner_mod.select_memory_wiring_async(
        _ach_cfg(), "http://127.0.0.1:9/mcp", "ach-gitlab-pr", {"x-ach-key": "ek_x"}
    )
    assert servers == ["http://127.0.0.1:9/mcp"]  # facade URL, NOT the ach-memory endpoint
    assert prompt == "## Memory\n\nok"
    assert seen["project"] == "ach-gitlab-pr"  # boot-static, not derived from the event
    assert seen["headers"] == {"x-ach-key": "ek_x"}  # boot-static too, never per-event


@pytest.mark.asyncio
async def test_ach_memory_wiring_empty_when_unavailable(monkeypatch):
    """D-02 fail-open, unchanged for the new backend."""

    async def fake_prepare(cfg, project, headers):
        return False, "## Memory\n\nUnavailable"

    monkeypatch.setattr(engine_runner_mod, "prepare_ach_memory", fake_prepare)
    servers, prompt = await engine_runner_mod.select_memory_wiring_async(
        _ach_cfg(), "http://127.0.0.1:9/mcp", "ach-gitlab-pr"
    )
    assert servers == []
    assert "Unavailable" in prompt


def test_ach_memory_auth_env_collected_for_forward_env_strip():
    """SECURITY: the ach-memory user key env NAME must be collected so it is stripped from
    engine.forwardEnv and redacted from logs — same path as the channel secrets."""
    cfg = types.SimpleNamespace(channels=[], memory=_ach_cfg())
    assert "AM_TOK" in collect_secret_env_names(cfg)


def test_ach_auth_arm_contributes_no_env_name():
    """`auth.type: ach` names no env var — the credential is the harness's own ek_, which the
    clean-slate allowlist already keeps out of opencode. Collecting a name here would be
    collecting one that does not exist."""
    from ach_agent.config.schema import AchMemoryMemory

    cfg = types.SimpleNamespace(
        channels=[],
        memory=AchMemoryMemory.model_validate(
            {
                "type": "ach-memory",
                "achMemory": {
                    "endpoint": "https://api.ackstorm.ai/mcp/ach-memory",
                    "auth": {"type": "ach"},
                },
            }
        ),
    )
    assert collect_secret_env_names(cfg) == []

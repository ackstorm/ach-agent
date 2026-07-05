# SPDX-License-Identifier: Apache-2.0
import pytest

from ach_agent.config.schema import HindsightMemory
from ach_agent.memory import hindsight as hs


def _cfg():
    return HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {
                "endpoint": "https://hs/mcp",
                "bank": "bank-1",
                "mission": "reviewer",
                "auth": {"env": "HS_TOK"},
                "mentalModels": [
                    {"id": "arch", "name": "Arch", "sourceQuery": "arch?", "autoRefresh": True},
                    {"id": "conv", "name": "Conv", "sourceQuery": "conv?"},
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_provision_creates_bank_models_and_refreshes(monkeypatch):
    monkeypatch.setenv("HS_TOK", "sekret")
    calls = []

    async def fake_call(endpoint, secret, tool, args):
        calls.append((tool, args))
        return "{}"

    monkeypatch.setattr(hs, "call_hindsight", fake_call)
    await hs.provision_memory(_cfg())

    tools = [t for t, _ in calls]
    assert tools.count(hs.HINDSIGHT_CREATE_BANK) == 1
    assert tools.count(hs.HINDSIGHT_CREATE_MENTAL_MODEL) == 2
    # only the auto_refresh model (arch) is refreshed
    refresh = [a for t, a in calls if t == hs.HINDSIGHT_REFRESH_MENTAL_MODEL]
    assert len(refresh) == 1 and refresh[0]["mental_model_id"] == "arch"
    # bank_id injected everywhere
    assert all("bank_id" in a for _, a in calls)


@pytest.mark.asyncio
async def test_provision_skips_when_auth_configured_but_unset(monkeypatch):
    monkeypatch.delenv("HS_TOK", raising=False)  # cfg has auth={env:HS_TOK} but it's unset
    called = False

    async def fake_call(*a, **k):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(hs, "call_hindsight", fake_call)
    await hs.provision_memory(_cfg())  # must not raise
    assert called is False


@pytest.mark.asyncio
async def test_provision_proceeds_with_no_auth(monkeypatch):
    """No auth field (internal URL) → provisions with secret=None."""
    cfg = HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {
                "endpoint": "http://hindsight.svc/mcp",
                "bank": "b",
                "mentalModels": [{"id": "arch", "name": "Arch", "sourceQuery": "arch?"}],
            },
        }
    )
    secrets_seen = []

    async def fake_call(endpoint, secret, tool, args):
        secrets_seen.append(secret)
        return "{}"

    monkeypatch.setattr(hs, "call_hindsight", fake_call)
    await hs.provision_memory(cfg)
    assert secrets_seen and all(s is None for s in secrets_seen)  # unauthenticated


@pytest.mark.asyncio
async def test_provision_swallows_errors(monkeypatch):
    monkeypatch.setenv("HS_TOK", "sekret")

    async def boom(*a, **k):
        raise RuntimeError("hindsight down")

    monkeypatch.setattr(hs, "call_hindsight", boom)
    await hs.provision_memory(_cfg())  # fail-open: no raise

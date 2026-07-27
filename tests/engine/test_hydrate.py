import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError

from ach_agent.engine.hydrate import (
    HydrationManifest,
    fetch_hydration_manifest,
    hydrate,
    resolve_model,
)

# Captured VERBATIM from the live ACH `POST /platform/hydrate` (2026-06-25). This is
# the real contract: runtime.models are OBJECTS {id, endpoint}, NOT bare strings.
SAMPLE = {
    "schemaVersion": "v1alpha1",
    "environment": "platform",
    "runtime": {
        "models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://ach.example.com/v1"}],
        "mcpServers": [
            {
                "id": "mcp-google-calendar-ro",
                "endpoint": "https://ach.example.com/mcp/mcp-google-calendar-ro",
            }
        ],
        "a2aAgents": [],
    },
    "context": {
        "prompts": [],
        "plugins": [],
        "artifacts": [],
        "skills": [
            {
                "name": "frontend-design@anthropics-skills",
                "id": "frontend-design@anthropics-skills",
                "downloadUrl": "https://ach.example.com/content/skill/frontend-design@anthropics-skills",
            }
        ],
    },
}


async def test_hydrate_parses_manifest(monkeypatch):
    async def fake_post(url, headers, manifest=SAMPLE):
        assert headers["x-ach-key"] == "ek-abc"
        return SAMPLE

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", fake_post)
    m = await hydrate("https://ach.example.com", "ek-abc", agent_name="hydrate-unit-agent")
    assert m.models == ["gemini.gemini-flash-latest"]  # property exposes the ids
    assert m.runtime.models[0].endpoint == "https://ach.example.com/v1"  # real endpoint kept
    assert m.mcp_servers[0].id == "mcp-google-calendar-ro"
    assert m.context.skills[0].download_url.endswith("/skill/frontend-design@anthropics-skills")


async def test_fetch_hydration_manifest_rejects_missing_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_environment = {key: value for key, value in SAMPLE.items() if key != "environment"}

    async def fake_post(url: str, headers: dict[str, str]) -> dict[str, object]:
        return missing_environment

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", fake_post)

    with pytest.raises(ValidationError, match="environment"):
        await fetch_hydration_manifest("https://ach.example.com", "ek-abc")


async def test_hydrate_sets_agent_info_only_after_required_manifest_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_name = "hydrated-info-test-agent"

    async def valid_post(url: str, headers: dict[str, str]) -> dict[str, object]:
        return SAMPLE

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", valid_post)
    manifest = await hydrate("https://ach.example.com", "ek-abc", agent_name=agent_name)

    assert manifest.environment == "platform"
    assert (
        REGISTRY.get_sample_value(
            "ach_agent_info", {"agent": agent_name, "environment": "platform"}
        )
        == 1.0
    )

    missing_environment_agent = "hydrated-info-no-environment-agent"
    missing_environment = {key: value for key, value in SAMPLE.items() if key != "environment"}

    async def missing_environment_post(url: str, headers: dict[str, str]) -> dict[str, object]:
        return missing_environment

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", missing_environment_post)
    with pytest.raises(ValidationError, match="environment"):
        await hydrate("https://ach.example.com", "ek-abc", agent_name=missing_environment_agent)

    assert not any(
        sample.name == "ach_agent_info" and sample.labels.get("agent") == missing_environment_agent
        for family in REGISTRY.collect()
        if family.name == "ach_agent_info"
        for sample in family.samples
    )


def test_resolve_model_hard_fails_when_absent():
    m = HydrationManifest.model_validate(SAMPLE)
    with pytest.raises(SystemExit):
        resolve_model(m, "gemini.not-there")


def test_resolve_model_ok_returns_entry_when_present():
    m = HydrationManifest.model_validate(SAMPLE)
    entry = resolve_model(m, "gemini.gemini-flash-latest")  # no raise
    assert entry is not None
    assert entry.endpoint == "https://ach.example.com/v1"

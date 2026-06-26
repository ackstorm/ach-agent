import pytest

from ach_agent.engine.hydrate import HydrationManifest, hydrate, resolve_model

# Captured VERBATIM from the live ACH `POST /platform/hydrate` (2026-06-25). This is
# the real contract: runtime.models are OBJECTS {id, endpoint}, NOT bare strings.
SAMPLE = {
    "schemaVersion": "v1alpha1",
    "environment": "platform",
    "runtime": {
        "models": [
            {"id": "gemini.gemini-flash-latest", "endpoint": "https://ach.example.com/v1"}
        ],
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
    m = await hydrate("https://ach.example.com", "ek-abc")
    assert m.models == ["gemini.gemini-flash-latest"]  # property exposes the ids
    assert m.model_entries[0].endpoint == "https://ach.example.com/v1"  # real endpoint kept
    assert m.mcp_servers[0].id == "mcp-google-calendar-ro"
    assert m.context.skills[0].download_url.endswith("/skill/frontend-design@anthropics-skills")


def test_resolve_model_hard_fails_when_absent():
    m = HydrationManifest.model_validate(SAMPLE)
    with pytest.raises(SystemExit):
        resolve_model(m, "gemini.not-there")


def test_resolve_model_ok_returns_entry_when_present():
    m = HydrationManifest.model_validate(SAMPLE)
    entry = resolve_model(m, "gemini.gemini-flash-latest")  # no raise
    assert entry is not None
    assert entry.endpoint == "https://ach.example.com/v1"

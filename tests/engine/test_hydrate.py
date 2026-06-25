import pytest

from ach_agent.engine.hydrate import HydrationManifest, hydrate, resolve_model

SAMPLE = {
    "schemaVersion": "v1alpha1",
    "environment": "frontend-dev",
    "runtime": {
        "models": ["openai.gpt-5"],
        "mcpServers": [{"id": "mcp-gofetch", "endpoint": "https://ach/mcp/mcp-gofetch"}],
        "a2aAgents": [],
    },
    "context": {
        "prompts": [],
        "plugins": [],
        "artifacts": [],
        "skills": [
            {"name": "frontend-design", "id": "fd", "downloadUrl": "https://ach/content/skill/fd"}
        ],
    },
}


async def test_hydrate_parses_manifest(monkeypatch):
    async def fake_post(url, headers, manifest=SAMPLE):
        assert headers["x-ach-key"] == "ek-abc"
        return SAMPLE

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", fake_post)
    m = await hydrate("https://ach.ackstorm.ai", "ek-abc")
    assert m.models == ["openai.gpt-5"]
    assert m.mcp_servers[0].id == "mcp-gofetch"
    assert m.context.skills[0].download_url.endswith("/skill/fd")


def test_resolve_model_hard_fails_when_absent():
    m = HydrationManifest.model_validate(SAMPLE)
    with pytest.raises(SystemExit):
        resolve_model(m, "gemini.not-there")


def test_resolve_model_ok_when_present():
    m = HydrationManifest.model_validate(SAMPLE)
    resolve_model(m, "openai.gpt-5")  # no raise

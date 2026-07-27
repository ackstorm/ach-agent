# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent import identity
from ach_agent.engine.hydrate import (
    HydrationManifest,
    fetch_hydration_manifest,
    hydrate,
    resolve_model,
)
from ach_agent.identity import ProcessIdentity


@pytest.fixture(autouse=True)
def _reset_process_identity() -> None:
    identity.reset_for_testing()
    yield
    identity.reset_for_testing()


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


async def test_hydrate_parses_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_post(
        url: str, headers: dict[str, str], manifest: dict[str, object] = SAMPLE
    ) -> dict[str, object]:
        assert headers["x-ach-key"] == "ek-abc"
        assert headers["x-ach-agent"] == "hydrate-unit-agent"
        assert headers["x-ach-environment"] == "platform"
        return SAMPLE

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", fake_post)
    m = await hydrate(
        "https://ach.example.com",
        "ek-abc",
        agent_name="hydrate-unit-agent",
        requested_environment="platform",
    )
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
        await fetch_hydration_manifest(
            "https://ach.example.com",
            "ek-abc",
            ProcessIdentity(agent="hydrate-unit-agent", environment="platform"),
        )


async def test_hydrate_sends_bootstrap_headers_then_commits_validated_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def valid_post(url: str, headers: dict[str, str]) -> dict[str, object]:
        assert url == "https://ach.example.com/platform/hydrate"
        assert headers == {
            "x-ach-key": "ek-abc",
            "x-ach-agent": "classifier",
            "x-ach-environment": "requested-platform",
        }
        return SAMPLE

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", valid_post)
    manifest = await hydrate(
        "https://ach.example.com",
        "ek-abc",
        agent_name="classifier",
        requested_environment="requested-platform",
    )

    assert manifest.environment == "platform"
    assert identity.current() == ProcessIdentity(agent="classifier", environment="platform")


async def test_invalid_hydration_has_no_identity_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity.configure("already-committed", "stable")
    before = identity.current()
    missing_environment = {key: value for key, value in SAMPLE.items() if key != "environment"}

    async def invalid_post(url: str, headers: dict[str, str]) -> dict[str, object]:
        assert headers["x-ach-agent"] == "new-agent"
        assert headers["x-ach-environment"] == "requested-platform"
        return missing_environment

    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", invalid_post)
    with pytest.raises(ValidationError, match="environment"):
        await hydrate(
            "https://ach.example.com",
            "ek-abc",
            agent_name="new-agent",
            requested_environment="requested-platform",
        )

    assert identity.current() == before


def test_resolve_model_hard_fails_when_absent() -> None:
    m = HydrationManifest.model_validate(SAMPLE)
    with pytest.raises(SystemExit):
        resolve_model(m, "gemini.not-there")


def test_resolve_model_ok_returns_entry_when_present() -> None:
    m = HydrationManifest.model_validate(SAMPLE)
    entry = resolve_model(m, "gemini.gemini-flash-latest")  # no raise
    assert entry is not None
    assert entry.endpoint == "https://ach.example.com/v1"

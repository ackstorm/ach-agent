# SPDX-License-Identifier: MIT
from pathlib import Path

import pytest

from ach_runtime.config.schema import parse_config
from ach_runtime.manifest import ManifestError, load_manifest, manifest_to_wire

MINIMAL = """
ach: {environment: engineering-prod}
model: {name: gemini.gemini-flash-latest, type: gemini}
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "agent.yaml"
    path.write_text(text)
    return path


def _wire(tmp_path: Path, text: str) -> dict:
    path = _write(tmp_path, text)
    return manifest_to_wire(
        load_manifest(path), base_dir=tmp_path, name="support-triage", base_url="https://api.ackstorm.ai"
    )


def test_minimal_manifest_is_a_valid_config(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL))
    assert config.capability.server == "ach"
    assert config.capability.ach.environment == "engineering-prod"
    assert config.capability.ach.base_url == "https://api.ackstorm.ai"
    assert config.capability.ach.key.env == "ACH_KEY"
    assert config.model.name == "gemini.gemini-flash-latest"
    assert (config.agent.name, config.agent.namespace) == ("support-triage", "local")


def test_profile_is_read_and_optional(tmp_path) -> None:
    assert load_manifest(_write(tmp_path, MINIMAL)).ach.profile is None
    with_profile = MINIMAL.replace("{environment:", "{profile: support, environment:")
    assert load_manifest(_write(tmp_path, with_profile)).ach.profile == "support"


def test_nothing_needs_storage(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL))
    assert not config.workspace.persistence.enabled
    assert not config.workspace.session.persistence.enabled
    assert not config.artifacts.enabled


def test_only_the_internal_tui_channel(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL))
    [channel] = config.channels
    assert channel.name == "tui"
    assert (channel.routing.workspace_key, channel.routing.session_key) == ("tui", "tui")


def test_prompt_file_is_inlined(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("You triage tickets.")
    config = parse_config(_wire(tmp_path, MINIMAL + "prompt: {file: ./AGENTS.md}\n"))
    assert (config.prompt.system.type, config.prompt.system.text) == ("text", "You triage tickets.")


def test_prompt_text_is_kept(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "prompt: {text: Be brief.}\n"))
    assert (config.prompt.system.type, config.prompt.system.text) == ("text", "Be brief.")


@pytest.mark.parametrize("prompt", ["{}", "{file: a.md, text: b}", "./AGENTS.md"])
def test_prompt_needs_exactly_one_of_file_or_text(tmp_path, prompt) -> None:
    with pytest.raises(ManifestError, match="prompt"):
        load_manifest(_write(tmp_path, MINIMAL + f"prompt: {prompt}\n"))


def test_missing_prompt_file_names_the_path(tmp_path) -> None:
    with pytest.raises(ManifestError, match="NOPE.md"):
        _wire(tmp_path, MINIMAL + "prompt: {file: ./NOPE.md}\n")


def test_exclude_maps_to_the_capability_filter(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "exclude: {mcpServers: [mcp-slack]}\n"))
    assert config.capability.filter.exclude.mcp_servers == ["mcp-slack"]
    assert config.capability.filter.exclude.tools == []


@pytest.mark.parametrize("extra", ["enviroment: typo\n", "channels: []\n", "persistence: s3\n", "baseUrl: x\n"])
def test_unknown_key_is_rejected(tmp_path, extra) -> None:
    with pytest.raises(ManifestError, match=extra.split(":")[0]):
        load_manifest(_write(tmp_path, MINIMAL + extra))

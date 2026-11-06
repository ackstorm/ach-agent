# SPDX-License-Identifier: MIT
from pathlib import Path

import pytest

from ach_runtime.config.schema import ConfigError, parse_config
from ach_runtime.manifest import ManifestError, load_manifest, manifest_to_wire, secret_env_names

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
    assert config.workspace.persistence.type == "none"
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


def test_limits_override_the_defaults(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "limits: {maxSteps: 400, maxInvocationSeconds: 3600}\n"))
    assert config.limits.max_steps == 400
    assert config.limits.max_invocation_seconds == 3600
    assert config.limits.max_concurrent_invocations == 1


def test_deny_tools_defaults_to_empty(tmp_path) -> None:
    assert parse_config(_wire(tmp_path, MINIMAL)).engine.deny_tools == []


def test_deny_tools_replaces_the_default(tmp_path) -> None:
    wire = _wire(tmp_path, MINIMAL + "denyTools: [websearch]\n")
    assert parse_config(wire).engine.deny_tools == ["websearch"]


def test_invalid_deny_tool_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError, match="denyTools"):
        _wire(tmp_path, MINIMAL + "denyTools: [mcp.tool]\n")


def test_invalid_limit_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError):
        _wire(tmp_path, MINIMAL + "limits: {maxSteps: 0}\n")


@pytest.mark.parametrize("extra", ["enviroment: typo\n", "channels: []\n", "persistence: s3\n", "baseUrl: x\n"])
def test_unknown_key_is_rejected(tmp_path, extra) -> None:
    with pytest.raises(ManifestError, match=extra.split(":")[0]):
        load_manifest(_write(tmp_path, MINIMAL + extra))


STANDALONE = """
model:
  name: claude-sonnet-5-5
  type: anthropic
  baseUrl: https://api.anthropic.com/v1
  headers: {x-api-key: {env: ANTHROPIC_API_KEY}}
  limits: {context: 200000, output: 64000}
mcpServers:
  - {name: github, url: "https://gh.example/mcp", headers: {Authorization: {env: GH_AUTH}}}
"""


def test_without_ach_the_capability_is_standalone(tmp_path) -> None:
    config = parse_config(manifest_to_wire(load_manifest(_write(tmp_path, STANDALONE)), base_dir=tmp_path, name="a"))
    assert config.capability.server == "standalone"
    server = config.capability.standalone
    assert (server.model.base_url, server.model.headers["x-api-key"].env) == (
        "https://api.anthropic.com/v1", "ANTHROPIC_API_KEY")
    assert server.model.limits.context == 200000
    assert [(s.name, s.headers["Authorization"].env) for s in server.mcp_servers] == [("github", "GH_AUTH")]
    assert secret_env_names(load_manifest(_write(tmp_path, STANDALONE))) == ["ANTHROPIC_API_KEY", "GH_AUTH"]


def test_standalone_needs_a_base_url(tmp_path) -> None:
    with pytest.raises(ManifestError, match="baseUrl"):
        load_manifest(_write(tmp_path, "model: {name: m, type: openai}\n"))


def test_header_env_names_must_be_plain_variable_names(tmp_path) -> None:
    with pytest.raises(ManifestError, match="env variable name"):
        load_manifest(_write(tmp_path, STANDALONE.replace("GH_AUTH", "'GH=1 *'")))


def test_ach_takes_extra_mcp_servers_and_model_options(tmp_path) -> None:
    extra = (
        "mcpServers: [{name: github, url: 'https://gh.example/mcp', headers: {Authorization: {env: GH_AUTH}}}]\n"
    )
    text = MINIMAL.replace("type: gemini}", "type: gemini, thinking: {enabled: true, effort: low}}") + extra
    config = parse_config(_wire(tmp_path, text))
    assert config.capability.ach.model.thinking.effort == "low"
    assert [s.name for s in config.capability.ach.mcp_servers] == ["github"]


def test_ach_model_limits_reach_the_config(tmp_path) -> None:
    text = MINIMAL.replace("type: gemini}", "type: gemini, limits: {context: 524288, output: 65536}}")
    config = parse_config(_wire(tmp_path, text))
    assert (config.capability.ach.model.limits.context, config.capability.ach.model.limits.output) == (524288, 65536)
    assert parse_config(_wire(tmp_path, MINIMAL)).capability.ach.model.limits is None


def test_the_runtime_schema_validates_the_passed_through_fields(tmp_path) -> None:
    with pytest.raises(ConfigError, match="baseUrl"):  # an ach model is served by ACH
        _wire(tmp_path, MINIMAL.replace("type: gemini}", "type: gemini, baseUrl: 'https://x.example/v1'}"))
    with pytest.raises(ConfigError, match="skills"):
        manifest_to_wire(load_manifest(_write(tmp_path, STANDALONE + "exclude: {skills: [s]}\n")),
                         base_dir=tmp_path, name="a")

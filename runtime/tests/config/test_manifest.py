# SPDX-License-Identifier: Apache-2.0
import re
from pathlib import Path

import pytest

from ach_runtime.config.schema import ConfigError, parse_config
from ach_runtime.manifest import ManifestError, load_manifest, manifest_to_wire, secret_env_names

MINIMAL = """
ach: {environment: engineering-prod}
model: {name: gemini.gemini-flash-latest, api: gemini}
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


def test_engine_pi_sets_the_wire_engine_type(tmp_path) -> None:
    wire = _wire(tmp_path, MINIMAL.replace("api: gemini", "api: openai") + "engine: {type: pi}\n")
    assert parse_config(wire).engine.type == "pi"


def test_engine_pi_with_a_non_openai_model_fails_at_manifest_load(tmp_path) -> None:
    with pytest.raises(ConfigError, match="pi takes openai models only"):
        parse_config(_wire(tmp_path, MINIMAL + "engine: {type: pi}\n"))


@pytest.mark.parametrize("extra", ["", "engine: {type: opencode}\n"])
def test_the_default_engine_leaves_the_wire_type_unset(tmp_path, extra) -> None:
    assert "type" not in _wire(tmp_path, MINIMAL + extra)["engine"]


def test_unknown_engine_is_rejected(tmp_path) -> None:
    with pytest.raises(ManifestError):
        load_manifest(_write(tmp_path, MINIMAL + "engine: {type: codex}\n"))


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
    config = parse_config(_wire(tmp_path, MINIMAL + "prompt: {system: {type: file, file: ./AGENTS.md}}\n"))
    assert (config.prompt.system.type, config.prompt.system.text) == ("text", "You triage tickets.")


def test_prompt_text_is_kept(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "prompt: {system: {type: text, text: Be brief.}}\n"))
    assert (config.prompt.system.type, config.prompt.system.text) == ("text", "Be brief.")


@pytest.mark.parametrize("prompt", [
    "{}", "{system: {type: file}}", "{system: {type: ach, file: a.md}}", "{file: a.md}", "./AGENTS.md",
])
def test_prompt_system_needs_the_block_its_type_names(tmp_path, prompt) -> None:
    with pytest.raises(ManifestError, match="prompt"):
        load_manifest(_write(tmp_path, MINIMAL + f"prompt: {prompt}\n"))


def test_missing_prompt_file_names_the_path(tmp_path) -> None:
    with pytest.raises(ManifestError, match="NOPE.md"):
        _wire(tmp_path, MINIMAL + "prompt: {system: {type: file, file: ./NOPE.md}}\n")


def test_exclude_maps_to_the_capability_filter(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "filter: {exclude: {mcpServers: [mcp-slack]}}\n"))
    assert config.capability.filter.exclude.mcp_servers == ["mcp-slack"]
    assert config.capability.filter.exclude.tools == []


def test_include_maps_to_the_capability_filter(tmp_path) -> None:
    wire = _wire(tmp_path, MINIMAL + "filter: {include: {tools: [github.get_pull_request]}}\n")
    assert wire["capability"]["filter"]["include"]["tools"] == ["github.get_pull_request"]


def test_limits_override_the_defaults(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "limits: {maxSteps: 400, maxInvocationSeconds: 3600}\n"))
    assert config.limits.max_steps == 400
    assert config.limits.max_invocation_seconds == 3600
    assert config.limits.max_concurrent_invocations == 1


def test_deny_tools_defaults_to_empty(tmp_path) -> None:
    assert parse_config(_wire(tmp_path, MINIMAL)).engine.deny_tools == []


def test_deny_tools_replaces_the_default(tmp_path) -> None:
    wire = _wire(tmp_path, MINIMAL + "engine: {denyTools: [websearch]}\n")
    assert parse_config(wire).engine.deny_tools == ["websearch"]


def test_invalid_deny_tool_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError, match="denyTools"):
        _wire(tmp_path, MINIMAL + "engine: {denyTools: [mcp.tool]}\n")


def test_invalid_limit_is_rejected(tmp_path) -> None:
    with pytest.raises(ConfigError):
        _wire(tmp_path, MINIMAL + "limits: {maxSteps: 0}\n")


@pytest.mark.parametrize("extra", [
    "enviroment: typo\n", "channels: []\n", "persistence: s3\n", "baseUrl: x\n",
])
def test_unknown_key_is_rejected(tmp_path, extra) -> None:
    with pytest.raises(ManifestError, match=extra.split(":")[0]):
        load_manifest(_write(tmp_path, MINIMAL + extra))


STANDALONE = """
model:
  name: claude-sonnet-5-5
  api: anthropic
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
        load_manifest(_write(tmp_path, "model: {name: m, api: openai}\n"))


def test_header_env_names_must_be_plain_variable_names(tmp_path) -> None:
    with pytest.raises(ManifestError, match="env variable name"):
        load_manifest(_write(tmp_path, STANDALONE.replace("GH_AUTH", "'GH=1 *'")))


def test_ach_takes_extra_mcp_servers_and_model_options(tmp_path) -> None:
    extra = (
        "mcpServers: [{name: github, url: 'https://gh.example/mcp', headers: {Authorization: {env: GH_AUTH}}}]\n"
    )
    text = MINIMAL.replace("api: gemini}", "api: gemini, thinking: {enabled: true, effort: low}}") + extra
    config = parse_config(_wire(tmp_path, text))
    assert config.capability.ach.model.thinking.effort == "low"
    assert [s.name for s in config.capability.ach.mcp_servers] == ["github"]


def test_ach_model_limits_reach_the_config(tmp_path) -> None:
    text = MINIMAL.replace("api: gemini}", "api: gemini, limits: {context: 524288, output: 65536}}")
    config = parse_config(_wire(tmp_path, text))
    assert (config.capability.ach.model.limits.context, config.capability.ach.model.limits.output) == (524288, 65536)
    assert parse_config(_wire(tmp_path, MINIMAL)).capability.ach.model.limits is None


def test_the_runtime_schema_validates_the_passed_through_fields(tmp_path) -> None:
    with pytest.raises(ConfigError, match="baseUrl"):  # an ach model is served by ACH
        _wire(tmp_path, MINIMAL.replace("api: gemini}", "api: gemini, baseUrl: 'https://x.example/v1'}"))
    with pytest.raises(ConfigError, match="repo"):
        manifest_to_wire(load_manifest(_write(tmp_path, STANDALONE + "skills: [{name: s, type: github}]\n")),
                         base_dir=tmp_path, name="a")


SKILLS = """\
skills:
  - {name: toolkit, type: github, repo: acme/skills, headers: {Authorization: {env: SKILLS_TOKEN}}}
"""


def test_a_standalone_manifest_carries_its_skills_to_the_wire(tmp_path) -> None:
    manifest = load_manifest(_write(tmp_path, STANDALONE + SKILLS))
    wire = manifest_to_wire(manifest, base_dir=tmp_path, name="a")
    assert wire["capability"]["standalone"]["skills"][0]["repo"] == "acme/skills"
    assert parse_config(wire).capability.standalone.skills[0].archive_url() == \
        "https://api.github.com/repos/acme/skills/tarball"
    assert "SKILLS_TOKEN" in secret_env_names(manifest)


def test_skills_need_a_standalone_manifest(tmp_path) -> None:
    with pytest.raises(ManifestError, match="standalone"):
        manifest_to_wire(load_manifest(_write(tmp_path, MINIMAL + SKILLS)), base_dir=tmp_path, name="a")


def test_model_type_is_rejected_in_favour_of_api(tmp_path) -> None:
    with pytest.raises(ManifestError, match="api"):
        load_manifest(_write(tmp_path, MINIMAL.replace("api: gemini", "type: gemini")))


def test_model_api_defaults_to_openai(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL.replace(", api: gemini", "")))
    assert config.model.type == "openai"


@pytest.mark.parametrize(("old", "new"), [
    ("exclude: {tools: [a]}\n", "filter.exclude"),
    ("denyTools: [websearch]\n", "engine.denyTools"),
    ("engine: pi\n", "engine: {type: <name>}"),
    ("prompt: {file: a.md}\n", "prompt.system"),
    ("prompt: {text: x}\n", "prompt.system"),
])
def test_an_old_form_names_its_agent_cr_field(tmp_path, old, new) -> None:
    with pytest.raises(ManifestError, match=re.escape(new)):
        load_manifest(_write(tmp_path, MINIMAL + old))


def test_prompt_compose_reaches_the_config(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "prompt: {system: {type: text, text: x}, compose: replace}\n"))
    assert config.prompt.compose == "replace"


def test_an_ach_prompt_passes_through_with_its_file(tmp_path) -> None:
    config = parse_config(_wire(tmp_path, MINIMAL + "prompt: {system: {type: ach, ach: finops, achFile: main.md}}\n"))
    assert (config.prompt.system.type, config.prompt.system.ach, config.prompt.system.file) == ("ach", "finops", "main.md")

# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path

import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine.pi_config import agent_dir, build_pi_env, to_pi_config, write_pi_config

FIXTURE = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


def _public(model_type: str = "openai", params: dict | None = None, **engine):
    config = RuntimeConfig.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    public = public_config(config, engine_env=config.engine.env)
    model = public.model.model_copy(update={"type": model_type, "params": params or {}})
    return public.model_copy(update={"model": model, "engine": public.engine.model_copy(update=engine)})


def _catalog(model_type: str, name: str) -> dict:
    return {
        "providers": {model_type: {
            "baseURL": "http://facade/v1", "apiKey": "local-proxy", "headers": {"x-token": "t"},
            "models": {name: {"limit": {"context": 1000, "output": 100},
                              "cost": {"input": 1, "output": 2, "cache_read": 0.5},
                              "modalities": {"input": ["text", "image"]}}},
        }},
        "mcp": {"docs": {"url": "http://facade/mcp/docs", "headers": {"x-token": "t"}}},
    }


def _args(public, tmp_path) -> list[str]:
    return to_pi_config(public, home=tmp_path, catalog=None, mcp=None, resolved_system_prompt=None).args


def _with_thinking(public, **update):
    thinking = public.model.thinking.model_copy(update=update)
    return public.model_copy(update={"model": public.model.model_copy(update={"thinking": thinking})})


def test_openai_provider_points_at_the_facade(tmp_path) -> None:
    public = _public()
    config = to_pi_config(public, home=tmp_path, catalog=_catalog("openai", public.model.name), mcp=None, resolved_system_prompt=None)
    provider = config.models["providers"]["ach"]
    assert provider["api"] == "openai-completions"
    assert provider["baseUrl"] == "http://facade/v1"
    assert provider["authHeader"] is True
    assert provider["headers"] == {"x-token": "t"}


def test_model_metadata_maps_to_pi_fields(tmp_path) -> None:
    public = _public()
    config = to_pi_config(public, home=tmp_path, catalog=_catalog("openai", public.model.name), mcp=None, resolved_system_prompt=None)
    model = config.models["providers"]["ach"]["models"][0]
    assert model["id"] == public.model.name
    assert (model["contextWindow"], model["maxTokens"]) == (1000, 100)
    assert model["input"] == ["text", "image"]
    assert model["cost"] == {"input": 1, "output": 2, "cacheRead": 0.5, "cacheWrite": 0}


def test_mcp_servers_are_direct(tmp_path) -> None:
    public = _public()
    config = to_pi_config(public, home=tmp_path, catalog=_catalog("openai", public.model.name), mcp=None, resolved_system_prompt=None)
    assert config.mcp == {"mcpServers": {"docs": {"url": "http://facade/mcp/docs", "headers": {"x-token": "t"}, "exposure": "direct"}}}


def test_every_model_and_mcp_request_carries_the_session_baggage(tmp_path) -> None:
    public = _public()
    mcp = {"progress": {"url": "http://facade/runs/inv/", "headers": {"x-token": "t"}}}
    config = to_pi_config(public, home=tmp_path, catalog=_catalog("openai", public.model.name), mcp=mcp,
                          resolved_system_prompt=None, session_ref="s" * 64)
    baggage = {"baggage": "session.id=" + "s" * 64}
    assert config.models["providers"]["ach"]["headers"] == {"x-token": "t", **baggage}
    assert config.mcp["mcpServers"]["progress"]["headers"] == {"x-token": "t", **baggage}
    assert mcp["progress"]["headers"] == {"x-token": "t"}  # the caller's catalog is not mutated


def test_compaction_maps_to_settings(tmp_path) -> None:
    public = _public()
    config = to_pi_config(public, home=tmp_path, catalog=None, mcp=None, resolved_system_prompt=None)
    compaction = public.engine.compaction
    assert config.settings["compaction"] == {
        "enabled": compaction.auto, "keepRecentTokens": compaction.keep.tokens, "reserveTokens": compaction.buffer,
    }


def test_deny_tools_become_an_exclude_flag_without_opencode_defaults(tmp_path) -> None:
    public = _public(deny_tools=["write", "edit"], type="pi")
    args = to_pi_config(public, home=tmp_path, catalog=None, mcp=None, resolved_system_prompt=None).args
    assert args[args.index("--exclude-tools") + 1] == "write,edit"


def test_params_on_openai_become_sampling_params(tmp_path) -> None:
    public = _public("openai", {"temperature": 0.2})
    config = to_pi_config(public, home=tmp_path, catalog=None, mcp=None, resolved_system_prompt=None)
    assert config.models["providers"]["ach"]["models"][0]["samplingParams"] == {"temperature": 0.2}


@pytest.mark.parametrize("model_type", ["gemini", "anthropic"])
def test_other_model_types_fail_with_a_config_error(tmp_path, model_type) -> None:
    with pytest.raises(ValueError, match="model.type"):
        to_pi_config(_public(model_type), home=tmp_path, catalog=None, mcp=None, resolved_system_prompt=None)


def test_write_is_harness_owned(tmp_path) -> None:
    config = to_pi_config(_public(), home=tmp_path, catalog=None, mcp=None, resolved_system_prompt=None)
    write_pi_config(config, tmp_path)
    (agent_dir(tmp_path) / "models.json").write_text("tampered")
    write_pi_config(config, tmp_path)
    assert json.loads((agent_dir(tmp_path) / "models.json").read_text())["providers"]["ach"]
    assert (agent_dir(tmp_path) / "settings.json").exists()


def test_env_points_both_distributions_at_one_agent_dir(tmp_path) -> None:
    env = build_pi_env(home=tmp_path, scratch=tmp_path / "s", approved_env={})
    assert env["PI_CODING_AGENT_DIR"] == env["PIG_CODING_AGENT_DIR"] == str(tmp_path / ".pi" / "agent")
    assert (env["PI_OFFLINE"], env["PIG_OFFLINE"], env["PI_TELEMETRY"]) == ("1", "1", "0")
    assert "OPENCODE_SERVER_PASSWORD" not in env


def test_context_files_are_disabled(tmp_path) -> None:
    assert "--no-context-files" in _args(_public(), tmp_path)


def test_thinking_flag_follows_the_model_config(tmp_path) -> None:
    on = _args(_with_thinking(_public(), enabled=True, effort="high"), tmp_path)
    assert on[on.index("--thinking") + 1] == "high"
    off = _args(_with_thinking(_public(), enabled=False), tmp_path)
    assert off[off.index("--thinking") + 1] == "off"


@pytest.mark.parametrize("compose,flag", [("append", "--append-system-prompt"), ("replace", "--system-prompt")])
def test_system_prompt_flag_and_file(tmp_path, compose, flag) -> None:
    public = _public()
    system = public.prompt.system.model_copy(update={"type": "text", "text": "be brief"})
    prompt = public.prompt.model_copy(update={"compose": compose, "system": system})
    config = to_pi_config(
        public.model_copy(update={"prompt": prompt}), home=tmp_path, catalog=None, mcp=None, resolved_system_prompt=None,
    )
    assert config.args[config.args.index(flag) + 1] == str(agent_dir(tmp_path) / "system-prompt.md")
    write_pi_config(config, tmp_path)
    assert (agent_dir(tmp_path) / "system-prompt.md").read_text() == "be brief"


def test_skill_flag_only_when_skills_dir_exists(tmp_path) -> None:
    assert "--skill" not in _args(_public(), tmp_path)
    skills = tmp_path / ".ach-state" / "skills"
    skills.mkdir(parents=True)
    args = _args(_public(), tmp_path)
    assert args[args.index("--skill") + 1] == str(skills)

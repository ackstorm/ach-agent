# SPDX-License-Identifier: MIT
"""engine/config.py — env allowlist/XDG pinning and PublicConfig -> plugin fixture
translation (contract §§4-5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.config.schema import PromptBlock, SystemAch, SystemFile, ThinkingBlock
from ach_runtime.engine.config import _ENGINE_ENV_ALLOWLIST, _thinking_options, build_env, managed_content, to_fixture
from ach_runtime.engine.opencode import _expects_native_system

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


@pytest.fixture
def public():
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config = RuntimeConfig.model_validate(wire)
    return public_config(config, engine_env=config.engine.env)


def test_build_env_pins_home_and_all_four_xdg_paths(tmp_path: Path) -> None:
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    env = build_env(home=home, scratch=scratch, approved_env={}, password="secret")
    assert env["HOME"] == str(home)
    assert env["XDG_CONFIG_HOME"] == str(home / ".config")
    assert env["XDG_DATA_HOME"] == str(home / ".local" / "share")
    assert env["XDG_STATE_HOME"] == str(home / ".local" / "state")
    assert env["XDG_CACHE_HOME"] == str(home / ".cache")
    assert env["TMPDIR"] == str(scratch)


def test_build_env_disables_project_config_and_models_fetch(tmp_path: Path) -> None:
    env = build_env(home=tmp_path / "h", scratch=tmp_path / "s", approved_env={}, password="x")
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
    assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
    assert env["OPENCODE_SERVER_PASSWORD"] == "x"


def test_build_env_never_leaks_ambient_names(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "super-secret")
    env = build_env(home=tmp_path / "h", scratch=tmp_path / "s", approved_env={}, password="x")
    assert "ACH_TOKEN" not in env


_KUBELET_INJECTED = (
    "KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBERNETES_SERVICE_PORT_HTTPS",
    "KUBERNETES_PORT", "KUBERNETES_PORT_443_TCP", "KUBERNETES_PORT_443_TCP_ADDR",
    "KUBERNETES_PORT_443_TCP_PORT", "KUBERNETES_PORT_443_TCP_PROTO",
)


def test_allowlist_excludes_kubernetes_names() -> None:
    assert not any(name.startswith("KUBERNETES_") for name in _ENGINE_ENV_ALLOWLIST)


def test_build_env_drops_kubelet_and_pod_identity_env(tmp_path: Path, monkeypatch) -> None:
    # The kubelet injects KUBERNETES_* even with enableServiceLinks: false; the pod spec
    # adds the ACH_* identity names. None of them may reach the engine or its tool shells.
    for name in (*_KUBELET_INJECTED, "ACH_SANDBOX_VERIFY_KEY", "ACH_POD_UID", "ACH_POD_NAMESPACE"):
        monkeypatch.setenv(name, "leak")
    env = build_env(home=tmp_path / "h", scratch=tmp_path / "s", approved_env={}, password="x")
    assert not [n for n in env if n.startswith(("KUBERNETES_", "ACH_"))]


def test_to_fixture_carries_approved_model_and_facade_url(public) -> None:
    fixture = to_fixture(public)
    assert fixture["maxSteps"] == public.limits.max_steps
    provider = fixture["providers"][public.model.type]
    assert provider["baseURL"] == public.infrastructure.facade_endpoint
    assert public.model.name in provider["models"]


def test_to_fixture_rejects_zero_steps_downstream(public) -> None:
    # maxSteps > 0 is already enforced by LimitsBlock; to_fixture must not silently
    # coerce — the plugin's own assertFixture is the final gate (contract §4).
    fixture = to_fixture(public)
    assert isinstance(fixture["maxSteps"], int) and fixture["maxSteps"] > 0


def test_managed_content_sets_conservative_isolation_flags(public) -> None:
    content = managed_content(public, to_fixture(public))
    assert content["snapshots"] is False
    assert content["lsp"] is False
    assert content["compaction"]["buffer"] == public.engine.compaction.buffer
    assert len(content["plugins"]) == 1
    assert content["plugins"][0]["options"]["fixture"]["maxSteps"] == public.limits.max_steps


def test_catalog_translation_preserves_grants_and_scoped_mcp(public) -> None:
    catalog = {
        "providers": {public.model.type: {
            "baseURL": "https://facade.example/t/model-token",
            "apiKey": "local-proxy",
            "models": {public.model.name: {
                "tool_call": False,
                "cost": {"input": 0, "output": 2.5},
                "limit": {"context": 8192, "output": 512},
                "modalities": {"input": ["text", "image"], "output": ["text"]},
                "temperature": 0.25,
            }},
        }},
        "mcp": {"search": {"url": "https://facade.example/t/mcp-token", "headers": {}}},
    }
    fixture = to_fixture(public, catalog=catalog)
    model = fixture["providers"][public.model.type]["models"][public.model.name]
    assert model["tool_call"] is False
    assert model["cost"] == {"input": 0, "output": 2.5}
    assert model["limit"] == {"context": 8192, "output": 512}
    assert model["modalities"]["input"] == ["text", "image"]
    assert fixture["mcp"] == catalog["mcp"]
    assert fixture["maxSteps"] == public.limits.max_steps
    assert "local-proxy" not in json.dumps(catalog["mcp"])


def test_operator_selected_forward_env_is_allowed_but_ambient_credentials_are_not(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-aws-sentinel")
    monkeypatch.setenv("ACH_TOKEN", "ambient-ach-sentinel")
    approved = {
        "AWS_REGION": "eu-central-1",
        "HTTPS_PROXY": "http://proxy.internal:8080",
        "SSL_CERT_FILE": "/etc/ssl/custom.pem",
    }
    env = build_env(home=tmp_path / "h", scratch=tmp_path / "s", approved_env=approved, password="x")
    assert all(env[name] == value for name, value in approved.items())
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "ACH_TOKEN" not in env


@pytest.mark.parametrize(
    ("model_type", "enabled", "effort", "expected"),
    [
        ("openai", False, None, {}),
        ("openai", True, None, {}),
        ("openai", True, "high", {"reasoningEffort": "high"}),
        ("gemini", True, "xhigh", {"thinkingConfig": {"thinkingLevel": "high"}}),
        ("anthropic", True, "minimal", {"thinking": {"type": "enabled", "budgetTokens": 1024}}),
        ("anthropic", True, "low", {"thinking": {"type": "enabled", "budgetTokens": 4096}}),
        ("anthropic", True, "medium", {"thinking": {"type": "enabled", "budgetTokens": 10000}}),
        ("anthropic", True, "high", {"thinking": {"type": "enabled", "budgetTokens": 24576}}),
        ("anthropic", True, "xhigh", {"thinking": {"type": "enabled", "budgetTokens": 32000}}),
    ],
)
def test_thinking_options_use_legacy_provider_spellings(model_type, enabled, effort, expected):
    assert _thinking_options(model_type, enabled, effort) == expected


@pytest.mark.parametrize(
    "system", [SystemFile(type="file", file="prompts/system.md"), SystemAch(type="ach", ach="review")]
)
def test_required_external_prompt_needs_private_resolved_text(public, system):
    config = public.model_copy(update={"prompt": PromptBlock(system=system, compose="append")})
    with pytest.raises(ValueError, match="must be resolved before engine start"):
        to_fixture(config)
    fixture = to_fixture(config, resolved_system_prompt="")
    assert fixture["systemPrompt"] == ""
    assert fixture["promptCompose"] == "append"
    assert "systemPromptUnresolved" not in fixture


@pytest.mark.parametrize(
    "system", [SystemFile(type="file", file="prompts/system.md"), SystemAch(type="ach", ach="review")]
)
def test_lookup_only_fixture_omits_only_unresolved_prompt_marker(public, system):
    config = public.model_copy(update={"prompt": PromptBlock(system=system, compose="replace")})
    fixture = to_fixture(config, lookup_only_prompt=True)
    assert fixture["promptCompose"] == "replace"
    assert "systemPrompt" not in fixture
    assert "systemPromptUnresolved" not in fixture


def test_explicit_model_params_override_thinking_options(public):
    model = public.model.model_copy(update={
        "type": "openai",
        "thinking": ThinkingBlock(enabled=True, effort="low"),
        "params": {"reasoningEffort": "high", "temperature": 0.25},
    })
    config = public.model_copy(update={"model": model})
    fixture = to_fixture(config)
    assert fixture["requestOptions"] == {"reasoningEffort": "high", "temperature": 0.25}


def test_memory_tools_without_prompt_block_default_to_append(public):
    """A memory-tools persona with no `prompt` block must still carry a compose mode the
    plugin applies — the schema default `append`, so readiness expects no native system."""
    config = public.model_copy(update={"prompt": None})
    catalog = {
        "providers": {public.model.type: {"baseURL": "http://facade", "models": {public.model.name: {}}}},
        "mcp": {},
        "memoryToolsSpec": "use memory",
    }
    fixture = to_fixture(config, catalog=catalog)
    assert fixture["systemPrompt"] == "## Memory Tools\nuse memory"
    assert fixture["promptCompose"] == PromptBlock().compose == "append"
    assert not _expects_native_system(fixture)


def test_to_fixture_carries_engine_deny_tools(public) -> None:
    assert to_fixture(public)["denyTools"] == []
    denied = public.model_copy(
        update={"engine": public.engine.model_copy(update={"deny_tools": ["question", "webfetch"]})}
    )
    assert to_fixture(denied)["denyTools"] == ["question", "webfetch"]

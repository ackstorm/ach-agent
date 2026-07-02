"""Config schema tests: CFG-04/05/06 + D-01/D-04/D-05/D-06 regression suite.

Each test function imports the module under test inside the function body
(not at module level) to allow monkeypatching and avoid cross-test pollution.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Path to the fixtures directory relative to this file
FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixture-loading helpers
# ---------------------------------------------------------------------------


def _read_fixture(name: str) -> dict:
    """Read a fixture file into a mutable dict (for negative-test mutation)."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _load_raw(tmp_path: Path, raw: dict):
    """Write a raw config dict to a temp file and load it via load_config."""
    from ach_agent.config import load_config

    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    return load_config(str(config_file))


def _load_fixture(tmp_path: Path, name: str):
    """Load a named fixture via load_config (round-tripping through a temp file)."""
    return _load_raw(tmp_path, _read_fixture(name))


# ---------------------------------------------------------------------------
# Shared valid v3 base dict (mutated minimally in negative tests so each test
# isolates a single rejection cause).
# ---------------------------------------------------------------------------

_VALID_WEBHOOK_BASE: dict = {
    "schemaVersion": "1",
    "agent": {"name": "test-agent"},
    "model": {"name": "openai.gpt-5", "type": "openai", "params": {"temperature": 1}},
    "engine": {"workDir": "/workspace", "startupTimeoutSeconds": 30},
    "capability": {
        "type": "ach",
        "ach": {"baseUrl": "https://ach.example.com", "environment": "test"},
        "filter": {"exclude": {"tools": []}},
    },
    "limits": {
        "maxConcurrentInvocations": 1,
        "maxInvocationSeconds": 1800,
        "maxQueuedTotal": 100,
        "idempotencyWindowSeconds": 3600,
        "maxSteps": 50,
        "terminalOutputRetries": 1,
    },
    "channels": [
        {
            "name": "test-webhook",
            "type": "webhook",
            "source": "gitlab",
            "webhook": {"auth": {"type": "gitlab_token", "secretPath": "/etc/secret"}},
        }
    ],
}

_VALID_CRON_BASE: dict = {
    "schemaVersion": "1",
    "agent": {"name": "test-agent"},
    "model": {"name": "openai.gpt-5", "type": "openai", "params": {"temperature": 1}},
    "engine": {"workDir": "/workspace", "startupTimeoutSeconds": 30},
    "capability": {
        "type": "ach",
        "ach": {"baseUrl": "https://ach.example.com", "environment": "test"},
        "filter": {"exclude": {"tools": []}},
    },
    "limits": {
        "maxConcurrentInvocations": 1,
        "maxInvocationSeconds": 1800,
        "maxQueuedTotal": 100,
        "idempotencyWindowSeconds": 3600,
        "maxSteps": 50,
        "terminalOutputRetries": 1,
    },
    "channels": [
        {
            "name": "heartbeat",
            "type": "cron",
            "cron": {"schedule": "* * * * *", "timezone": "UTC"},
        }
    ],
}


# ---------------------------------------------------------------------------
# Contract-close reshape (this session): engine block, prompt.system,
# filter.exclude three lists, header_token, dropped namespace/session/expire.
# ---------------------------------------------------------------------------


def test_engine_holds_workdir_and_startup() -> None:
    from ach_agent.config.schema import AgentConfig

    cfg = AgentConfig.model_validate(
        {
            "schemaVersion": "1",
            "agent": {"name": "a"},
            "model": {"name": "m", "type": "openai"},
            "capability": {"type": "ach", "ach": {"baseUrl": "http://x"}},
            "engine": {
                "workDir": "/w",
                "startupTimeoutSeconds": 9,
                "forwardEnv": ["SSL_CERT_FILE"],
            },
        }
    )
    assert cfg.engine.work_dir == "/w"
    assert cfg.engine.startup_timeout_seconds == 9
    assert cfg.engine.forward_env == ["SSL_CERT_FILE"]
    assert not hasattr(cfg, "governed")


def test_engine_idle_ttl_default() -> None:
    """engine.idle_ttl_seconds defaults to 30.0 and an explicit 0 round-trips (Task 2)."""
    from ach_agent.config.schema import EngineBlock

    assert EngineBlock.model_validate({}).idle_ttl_seconds == 30.0
    assert EngineBlock.model_validate({"idleTtlSeconds": 0}).idle_ttl_seconds == 0.0
    assert EngineBlock.model_validate({"idleTtlSeconds": 120.5}).idle_ttl_seconds == 120.5


def test_engine_max_tool_calls_default() -> None:
    """engine.max_tool_calls defaults to 0 (disabled) and accepts a positive override (Plan 4)."""
    import pytest

    from ach_agent.config.schema import EngineBlock

    assert EngineBlock.model_validate({}).max_tool_calls == 0
    assert EngineBlock.model_validate({"maxToolCalls": 80}).max_tool_calls == 80
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        EngineBlock.model_validate({"maxToolCalls": -1})


def test_engine_home_field_and_workdir_default_empty() -> None:
    from ach_agent.config.schema import EngineBlock

    e = EngineBlock.model_validate({"home": "/h", "workDir": "/h/ws"})
    assert e.home == "/h"
    assert e.work_dir == "/h/ws"
    # Both default to "" (empty) so the harness can derive them from persistence at boot.
    blank = EngineBlock.model_validate({})
    assert blank.home == ""
    assert blank.work_dir == ""


def test_prompt_system_field() -> None:
    from ach_agent.config.schema import PromptBlock, SystemText

    b = PromptBlock.model_validate({"system": {"type": "text", "text": "hi"}})
    assert isinstance(b.system, SystemText)
    assert b.system.text == "hi"


def test_contract_reserved_fields_accepted() -> None:
    """prompt.compose is a CONTRACT §2 reserved field: the operator renders it,
    the harness must ACCEPT it (extra=forbid) even though it does not yet execute layering.
    Guards against re-removing it as 'inert'."""
    from ach_agent.config.schema import PromptBlock

    p = PromptBlock.model_validate({"system": {"type": "text", "text": "hi"}, "compose": "append"})
    assert p.compose == "append"


def test_agent_namespace_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import AgentBlock

    with pytest.raises(ValidationError):
        AgentBlock.model_validate({"name": "a", "namespace": "x"})


def test_filter_exclude_three_lists() -> None:
    from ach_agent.config.schema import CapabilityFilterExcludeBlock

    e = CapabilityFilterExcludeBlock.model_validate(
        {"tools": ["t"], "mcpServers": ["s"], "skills": ["k"]}
    )
    assert e.tools == ["t"] and e.mcp_servers == ["s"] and e.skills == ["k"]


def test_webhook_header_token_auth() -> None:
    from ach_agent.config.schema import WebhookAuthBlock

    a = WebhookAuthBlock.model_validate(
        {"type": "header_token", "header": "X-Api-Key", "secretPath": "/s"}
    )
    assert a.type == "header_token" and a.header == "X-Api-Key"


def test_webhook_gitlab_events() -> None:
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import WebhookBlock

    assert WebhookBlock().gitlab_events is None
    # Config key is camelCase `gitlabEvents` (renamed from snake_case — no dual name).
    b = WebhookBlock.model_validate({"gitlabEvents": ["merge_request", "note"]})
    assert b.gitlab_events == ["merge_request", "note"]
    with pytest.raises(ValidationError):
        WebhookBlock.model_validate({"gitlabEvents": ["pipeline"]})
    # snake_case is REJECTED (extra=forbid) — this is a rename, not an alias.
    with pytest.raises(ValidationError):
        WebhookBlock.model_validate({"gitlab_events": ["merge_request"]})


def test_channel_session_and_expire_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import ChannelConfig

    with pytest.raises(ValidationError):
        ChannelConfig.model_validate(
            {
                "name": "c",
                "type": "cron",
                "cron": {"schedule": "* * * * *"},
                "session": {"mode": "auto"},
            }
        )
    with pytest.raises(ValidationError):
        ChannelConfig.model_validate(
            {
                "name": "c",
                "type": "cron",
                "cron": {"schedule": "* * * * *"},
                "expire": 30,
            }
        )


# ---------------------------------------------------------------------------
# Positive: CFG-06 — each fixture loads, type + representative sub-field verified
# ---------------------------------------------------------------------------


def test_load_valid_cron_config() -> None:
    """CFG-01/06: load a valid v3 cron config without error."""
    from ach_agent.config import load_config

    config = load_config(str(FIXTURES_DIR / "config_cron.json"))
    assert len(config.channels) == 1
    assert config.channels[0].type == "cron"


def test_cron_channel_config() -> None:
    """CHN-02 / CFG-06: cron channel parses schedule and timezone."""
    from ach_agent.config import load_config

    config = load_config(str(FIXTURES_DIR / "config_cron.json"))
    channel = config.channels[0]
    assert channel.type == "cron"
    assert channel.cron is not None
    assert channel.cron.schedule == "* * * * *"
    assert channel.cron.timezone == "Europe/Madrid"


def test_load_valid_webhook_config() -> None:
    """CFG-06: valid v3 webhook fixture loads; source and auth.type asserted."""
    from ach_agent.config import load_config

    config = load_config(str(FIXTURES_DIR / "config_webhook.json"))
    assert len(config.channels) == 1
    ch = config.channels[0]
    assert ch.type == "webhook"
    assert ch.source == "gitlab"
    assert ch.webhook is not None
    assert ch.webhook.auth.type == "gitlab_token"


def test_load_valid_queue_config() -> None:
    """CFG-06: valid v3 queue fixture loads; ackMode asserted."""
    from ach_agent.config import load_config

    config = load_config(str(FIXTURES_DIR / "config_queue.json"))
    assert len(config.channels) == 1
    ch = config.channels[0]
    assert ch.type == "queue"
    assert ch.queue is not None
    assert ch.queue.ack_mode == "onComplete"


def test_load_valid_a2a_config() -> None:
    """CFG-06: valid v3 a2a fixture loads; mode and secretPath asserted."""
    from ach_agent.config import load_config

    config = load_config(str(FIXTURES_DIR / "config_a2a.json"))
    assert len(config.channels) == 1
    ch = config.channels[0]
    assert ch.type == "a2a"
    assert ch.a2a is not None
    assert ch.a2a.mode == "async"
    assert ch.a2a.auth.secret_path == "/etc/ach-agent/secrets/a2a/key"


# ---------------------------------------------------------------------------
# Positive: CFG-05 — every new v3 field parses correctly
# ---------------------------------------------------------------------------


def test_v3_fields_parse(tmp_path: Path) -> None:
    """CFG-05: all v3 fields parse — model.name/type/params, engine.work_dir,
    engine.startup_timeout_seconds, limits.max_steps, limits.terminal_output_retries,
    capability.filter.exclude.tools."""
    from ach_agent.config.schema import AgentConfig

    cfg = {
        "schemaVersion": "1",
        "agent": {"name": "field-test"},
        "model": {"name": "openai.gpt-5", "type": "openai", "params": {"temperature": 1}},
        "engine": {"workDir": "/custom/work", "startupTimeoutSeconds": 60},
        "capability": {
            "type": "ach",
            "ach": {"baseUrl": "https://ach.example.com", "environment": "staging"},
            "filter": {"exclude": {"tools": ["dangerous-tool", "restricted-tool"]}},
        },
        "limits": {
            "maxConcurrentInvocations": 2,
            "maxInvocationSeconds": 900,
            "maxQueuedTotal": 50,
            "idempotencyWindowSeconds": 7200,
            "maxSteps": 25,
            "terminalOutputRetries": 3,
        },
        "channels": [],
    }
    config = AgentConfig.model_validate_json(json.dumps(cfg))
    assert config.model.name == "openai.gpt-5"
    assert config.model.type == "openai"
    assert config.model.params == {"temperature": 1}
    assert config.engine.work_dir == "/custom/work"
    assert config.engine.startup_timeout_seconds == 60
    assert config.limits.max_steps == 25
    assert config.limits.terminal_output_retries == 3
    assert config.capability.filter.exclude.tools == ["dangerous-tool", "restricted-tool"]


# ---------------------------------------------------------------------------
# Positive/Negative: ModelBlock {name, type, params} shape
# ---------------------------------------------------------------------------


def test_model_block_name_type_params(tmp_path: Path) -> None:
    """CFG-05: model block parses as {name, type, params}."""
    cfg = _load_fixture(tmp_path, "config_webhook.json")
    assert cfg.model.name == "openai.gpt-5"
    assert cfg.model.type == "openai"
    assert cfg.model.params == {"temperature": 1}


def test_model_type_rejects_unknown_provider(tmp_path: Path) -> None:
    """D-06: model.type outside the closed provider Literal → sys.exit(1)."""
    raw = _read_fixture("config_webhook.json")
    raw["model"] = {"name": "x", "type": "bedrock", "params": {}}
    with pytest.raises(SystemExit):
        _load_raw(tmp_path, raw)


def test_model_params_is_open_dict(tmp_path: Path) -> None:
    """model.params is an open, unvalidated dict (arbitrary keys allowed)."""
    raw = _read_fixture("config_webhook.json")
    raw["model"] = {"name": "g", "type": "gemini", "params": {"thinking_level": "medium", "x": 1}}
    cfg = _load_raw(tmp_path, raw)
    assert cfg.model.params["thinking_level"] == "medium"


# ---------------------------------------------------------------------------
# Positive/Negative: YAML-authored contracts (local dev convenience)
# ---------------------------------------------------------------------------


def test_load_valid_yaml_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A .yaml contract validates against the SAME schema as JSON (aliases honored)."""
    from ach_agent.config import load_config

    monkeypatch.delenv("ACH_BASE_URL", raising=False)  # assert the contract's own baseUrl
    yaml_text = """
schemaVersion: "1"
agent:
  name: yaml-agent
model:
  name: gemini.gemini-flash-latest
  type: openai
capability:
  type: ach
  ach:
    baseUrl: https://ach.example.com
    environment: platform
prompt:
  system:
    type: text
    text: "You are a concise assistant."
channels: []
"""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml_text, encoding="utf-8")

    cfg = load_config(str(config_file))
    assert cfg.schema_version == "1"
    assert cfg.model.name == "gemini.gemini-flash-latest"
    assert cfg.model.type == "openai"
    assert cfg.capability.ach.base_url == "https://ach.example.com"
    from ach_agent.config.schema import SystemText

    assert cfg.prompt is not None
    assert isinstance(cfg.prompt.system, SystemText)
    assert cfg.prompt.system.text == "You are a concise assistant."


def test_yaml_unknown_key_hard_fails(tmp_path: Path) -> None:
    """A .yaml contract is held to the same extra='forbid' rule → sys.exit(1)."""
    from ach_agent.config import load_config

    yaml_text = """
schemaVersion: "1"
agent: {name: x}
model: {name: openai.gpt-5, type: openai}
capability: {type: ach, ach: {baseUrl: https://ach.example.com, environment: test}}
unexpectedKey: true
"""
    config_file = tmp_path / "bad.yaml"
    config_file.write_text(yaml_text, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_malformed_yaml_hard_fails(tmp_path: Path) -> None:
    """Malformed YAML → parse error → sys.exit(1) (mirrors JSON schema-mismatch path)."""
    from ach_agent.config import load_config

    config_file = tmp_path / "broken.yml"
    config_file.write_text("schemaVersion: \"1\"\n  bad: : indentation:\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_capability_ach_environment_is_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """capability.ach.environment is implicit (EK scopes it) → defaults to 'platform'."""
    from ach_agent.config import load_config

    monkeypatch.delenv("ACH_BASE_URL", raising=False)  # assert the contract's own baseUrl
    raw = {
        "schemaVersion": "1",
        "agent": {"name": "x"},
        "model": {"name": "gemini.gemini-flash-latest", "type": "openai"},
        "capability": {"type": "ach", "ach": {"baseUrl": "https://ach.example.com"}},
    }
    config_file = tmp_path / "no_env.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")

    cfg = load_config(str(config_file))
    assert cfg.capability.ach.environment == "platform"
    assert cfg.capability.ach.base_url == "https://ach.example.com"


# ---------------------------------------------------------------------------
# ACH_BASE_URL env override (local-dev convenience; baked/sample configs ship hostless)
# ---------------------------------------------------------------------------


def test_ach_base_url_env_fills_when_config_omits_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config that omits baseUrl loads when ACH_BASE_URL supplies it at runtime."""
    from ach_agent.config import load_config

    monkeypatch.setenv("ACH_BASE_URL", "https://ach.example.com")
    raw = {
        "schemaVersion": "1",
        "agent": {"name": "x"},
        "model": {"name": "gemini.gemini-flash-latest", "type": "openai"},
        "capability": {"type": "ach", "ach": {}},
    }
    config_file = tmp_path / "hostless.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")

    cfg = load_config(str(config_file))
    assert cfg.capability.ach.base_url == "https://ach.example.com"


def test_ach_base_url_env_overrides_config_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACH_BASE_URL wins over a baseUrl pinned in the contract (point at staging vs prod)."""
    from ach_agent.config import load_config

    monkeypatch.setenv("ACH_BASE_URL", "https://staging.example.com")
    raw = {
        "schemaVersion": "1",
        "agent": {"name": "x"},
        "model": {"name": "gemini.gemini-flash-latest", "type": "openai"},
        "capability": {"type": "ach", "ach": {"baseUrl": "https://pinned.example.com"}},
    }
    config_file = tmp_path / "pinned.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")

    cfg = load_config(str(config_file))
    assert cfg.capability.ach.base_url == "https://staging.example.com"


def test_missing_base_url_and_no_env_hard_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No baseUrl in the contract AND no ACH_BASE_URL env → sys.exit(1)."""
    from ach_agent.config import load_config

    monkeypatch.delenv("ACH_BASE_URL", raising=False)
    raw = {
        "schemaVersion": "1",
        "agent": {"name": "x"},
        "model": {"name": "gemini.gemini-flash-latest", "type": "openai"},
        "capability": {"type": "ach", "ach": {}},
    }
    config_file = tmp_path / "no_host.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Negative: CFG-02 — unknown top-level key → sys.exit(1)
# ---------------------------------------------------------------------------


def test_unknown_key_hard_fail(tmp_path: Path) -> None:
    """CFG-02: unknown top-level key → sys.exit(1) (non-zero exit)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "unexpectedKey": True,
    }
    config_file = tmp_path / "bad.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Negative: CFG-03 — unknown channel type → sys.exit(1)
# ---------------------------------------------------------------------------


def test_unknown_channel_type_rejected(tmp_path: Path) -> None:
    """CFG-03: unknown channel type → ValidationError → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [{"name": "pigeon-post", "type": "carrierpigeon"}],
    }
    config_file = tmp_path / "bad_channel.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Negative: CFG-04 — removed v2 blocks hard-fail
# ---------------------------------------------------------------------------


def test_engine_block_hard_fails(tmp_path: Path) -> None:
    """CFG-04: top-level engine block → extra='forbid' → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "engine": {
            "type": "opencode",
            "binaryPath": "opencode",
            "workDir": "/workspace",
        },
    }
    config_file = tmp_path / "bad_engine.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_response_actions_hard_fails(tmp_path: Path) -> None:
    """CFG-04: channel with responseActions → extra='forbid' → sys.exit(1).

    Mirrors a realistic v2 config shape.
    """
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                **_VALID_WEBHOOK_BASE["channels"][0],
                "responseActions": [{"name": "reply", "kind": "reply", "inputSchema": {}}],
            }
        ],
    }
    config_file = tmp_path / "bad_response_actions.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_response_block_hard_fails(tmp_path: Path) -> None:
    """CFG-04: channel with response block → extra='forbid' → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                **_VALID_WEBHOOK_BASE["channels"][0],
                "response": {"mode": "actionRequired", "fallback": "fail"},
            }
        ],
    }
    config_file = tmp_path / "bad_response.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_webhook_deliver_hard_fails(tmp_path: Path) -> None:
    """CFG-04: webhook block with deliver → extra='forbid' → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                **_VALID_WEBHOOK_BASE["channels"][0],
                "webhook": {
                    "auth": {"type": "gitlab_token", "secretPath": "/etc/secret"},
                    "deliver": {"mode": "post", "url": "https://example.com"},
                },
            }
        ],
    }
    config_file = tmp_path / "bad_deliver.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_input_schema_in_response_actions_hard_fails(tmp_path: Path) -> None:
    """CFG-04: inputSchema nested inside responseActions → hard-fails.

    Mirrors realistic v2 nesting: the channel carries responseActions with inputSchema
    on the action entry. extra='forbid' rejects the removed responseActions key (or
    the nested inputSchema), proving the legacy v2 shape is rejected.
    """
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                **_VALID_WEBHOOK_BASE["channels"][0],
                "responseActions": [
                    {
                        "name": "create_issue",
                        "kind": "sideEffect",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"title": {"type": "string"}},
                        },
                    }
                ],
            }
        ],
    }
    config_file = tmp_path / "bad_input_schema.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_consent_tier_in_response_actions_hard_fails(tmp_path: Path) -> None:
    """CFG-04: consentTier nested inside responseActions → hard-fails.

    Mirrors realistic v2 nesting: the channel carries responseActions with consentTier
    on the action entry. extra='forbid' rejects the removed responseActions key (or
    the nested consentTier), proving the legacy v2 shape is rejected.
    """
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                **_VALID_WEBHOOK_BASE["channels"][0],
                "responseActions": [
                    {
                        "name": "create_issue",
                        "kind": "sideEffect",
                        "consentTier": "auto",
                        "inputSchema": {},
                    }
                ],
            }
        ],
    }
    config_file = tmp_path / "bad_consent_tier.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Negative: D-06 — closed Literal bad enum values hard-fail
# ---------------------------------------------------------------------------


def test_bad_webhook_source_hard_fails(tmp_path: Path) -> None:
    """D-06: webhook.source:'slack' (removed) → ValidationError → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                **_VALID_WEBHOOK_BASE["channels"][0],
                "source": "slack",
            }
        ],
    }
    config_file = tmp_path / "bad_source.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_bad_webhook_auth_type_hard_fails(tmp_path: Path) -> None:
    """D-06: webhook.auth.type:'bearer' → ValidationError → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                **_VALID_WEBHOOK_BASE["channels"][0],
                "webhook": {"auth": {"type": "bearer", "secretPath": "/etc/secret"}},
            }
        ],
    }
    config_file = tmp_path / "bad_auth_type.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_bad_queue_ack_mode_hard_fails(tmp_path: Path) -> None:
    """D-06: queue.ackMode:'onReceive' → ValidationError → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                "name": "bad-queue",
                "type": "queue",
                "queue": {"type": "redis", "key": "ach:test", "ackMode": "onReceive"},
            }
        ],
    }
    config_file = tmp_path / "bad_ack_mode.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_bad_a2a_mode_hard_fails(tmp_path: Path) -> None:
    """D-06: a2a.mode:'sync' → ValidationError → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                "name": "bad-a2a",
                "type": "a2a",
                "a2a": {
                    "mode": "sync",
                    "auth": {"header": "x-api-key", "secretPath": "/etc/secret"},
                },
            }
        ],
    }
    config_file = tmp_path / "bad_a2a_mode.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Negative: D-04 — channel type↔block coherence validator
# ---------------------------------------------------------------------------


def test_cron_channel_with_webhook_block_hard_fails(tmp_path: Path) -> None:
    """D-04: cron channel carrying a webhook block → @model_validator → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                "name": "bad-cron",
                "type": "cron",
                "cron": {"schedule": "* * * * *", "timezone": "UTC"},
                "webhook": {"auth": {"type": "hmac", "secretPath": "/etc/secret"}},
            }
        ],
    }
    config_file = tmp_path / "bad_cron_webhook.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_tui_is_not_a_channel_type(tmp_path: Path) -> None:
    """`tui` is the --tui launch modifier, NOT a channel type → unknown type hard-fails."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [{"name": "console", "type": "tui"}],
    }
    config_file = tmp_path / "bad_tui_type.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_webhook_channel_missing_source_hard_fails(tmp_path: Path) -> None:
    """D-04: webhook channel missing source field → @model_validator → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "channels": [
            {
                "name": "no-source-webhook",
                "type": "webhook",
                "webhook": {"auth": {"type": "hmac", "secretPath": "/etc/secret"}},
            }
        ],
    }
    config_file = tmp_path / "bad_no_source.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Negative: D-05 — capability.type:'direct' hard-fails
# ---------------------------------------------------------------------------


def test_capability_type_direct_hard_fails(tmp_path: Path) -> None:
    """D-05: capability.type:'direct' → Literal['ach'] rejects → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "capability": {
            "type": "direct",
            "ach": {"baseUrl": "https://ach.example.com", "environment": "test"},
        },
    }
    config_file = tmp_path / "bad_direct.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# Negative: D-01 — schemaVersion != "1" hard-fails
# ---------------------------------------------------------------------------


def test_memory_block_uses_bank_not_scope():
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import HindsightMemory, HindsightParams

    m = HindsightMemory(
        type="hindsight",
        hindsight=HindsightParams(endpoint="http://mem:8080", bank="gitlab-pr-review"),
    )
    assert m.hindsight.bank == "gitlab-pr-review"

    # the old key is gone — extra='forbid' must reject it on HindsightParams
    with pytest.raises(ValidationError):
        HindsightMemory(
            type="hindsight",
            hindsight=HindsightParams(endpoint="http://mem:8080", scope="x"),  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# prompt.system discriminated union (SystemText | SystemFile | None)
# ---------------------------------------------------------------------------


def test_prompt_system_text_form():
    from ach_agent.config.schema import PromptBlock, SystemText

    b = PromptBlock.model_validate({"system": {"type": "text", "text": "hi"}, "compose": "append"})
    assert isinstance(b.system, SystemText)
    assert b.system.text == "hi"


def test_prompt_system_file_form():
    from ach_agent.config.schema import PromptBlock, SystemFile

    b = PromptBlock.model_validate({"system": {"type": "file", "file": "prompts/p/x.md"}})
    assert isinstance(b.system, SystemFile)
    assert b.system.file == "prompts/p/x.md"


def test_prompt_system_string_shorthand_rejected():
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import PromptBlock

    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": "an inline persona"})


def test_prompt_system_missing_type_rejected():
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import PromptBlock

    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": {"text": "no type"}})


def test_prompt_system_file_absolute_rejected():
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import PromptBlock

    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": {"type": "file", "file": "/etc/passwd"}})


def test_prompt_system_file_traversal_rejected():
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import PromptBlock

    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": {"type": "file", "file": "../../secrets/ek"}})


def test_prompt_system_ach_form():
    from ach_agent.config.schema import PromptBlock, SystemAch

    b = PromptBlock.model_validate({"system": {"type": "ach", "ach": "my-prompt"}})
    assert isinstance(b.system, SystemAch)
    assert b.system.ach == "my-prompt"
    assert b.system.file == ""


def test_prompt_system_ach_with_file_subpath():
    from ach_agent.config.schema import PromptBlock, SystemAch

    b = PromptBlock.model_validate({"system": {"type": "ach", "ach": "p", "file": "sub.md"}})
    assert isinstance(b.system, SystemAch)
    assert b.system.file == "sub.md"


def test_prompt_system_ach_traversal_rejected():
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import PromptBlock

    for bad in ({"type": "ach", "ach": "../evil"}, {"type": "ach", "ach": "p", "file": "../x"}):
        with pytest.raises(ValidationError):
            PromptBlock.model_validate({"system": bad})


def test_prompt_system_ach_empty_rejected():
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import PromptBlock

    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": {"type": "ach", "ach": "  "}})


def test_prompt_system_omitted_is_none():
    from ach_agent.config.schema import PromptBlock

    assert PromptBlock.model_validate({"compose": "append"}).system is None


def test_channel_session_defaults_auto_and_validates() -> None:
    """channel.session defaults to 'auto', accepts 'none', rejects other strings."""
    import pytest
    from pydantic import ValidationError

    from ach_agent.config.schema import ChannelConfig

    cron_block = {"cron": {"schedule": "* * * * *"}}
    c = ChannelConfig(name="c", type="cron", **cron_block)
    assert c.session == "auto"
    ChannelConfig(name="c", type="cron", session="none", **cron_block)
    with pytest.raises(ValidationError):
        ChannelConfig(name="c", type="cron", session="sometimes", **cron_block)


def test_schema_version_wrong_hard_fails(tmp_path: Path) -> None:
    """D-01: schemaVersion:'3' → Literal['1'] rejects → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        **_VALID_WEBHOOK_BASE,
        "schemaVersion": "3",
    }
    config_file = tmp_path / "bad_schema_version.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0

"""Config schema tests: CFG-01/02/03 (Pydantic v2 validation).

Each test function imports the module under test inside the function body
(not at module level) to allow monkeypatching and avoid cross-test pollution.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

# Path to the fixtures directory relative to this file
FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_load_valid_cron_config() -> None:
    """CFG-01: load a valid hand-written cron-only config without error."""
    from ach_agent.config import load_config

    config = load_config(str(FIXTURES_DIR / "config_cron.json"))
    assert len(config.channels) == 1
    assert config.channels[0].type == "cron"


def test_unknown_key_hard_fail(tmp_path: Path) -> None:
    """CFG-02: unknown top-level key → sys.exit(1) (non-zero exit)."""
    from ach_agent.config import load_config

    bad = {
        "schemaVersion": "1",
        "unexpectedKey": True,
        "agent": {"name": "test-agent", "namespace": "test"},
        "engine": {
            "type": "opencode",
            "binaryPath": "opencode",
            "workDir": "/workspace",
            "sessionDir": "/var/lib/ach-agent/opencode/sessions",
        },
        "model": {"default": "gpt-4o-mini", "provider": "openai"},
        "limits": {},
        "channels": [],
    }
    config_file = tmp_path / "bad.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_unknown_channel_type_rejected(tmp_path: Path) -> None:
    """CFG-03: unknown channel type → ValidationError → sys.exit(1)."""
    from ach_agent.config import load_config

    bad = {
        "schemaVersion": "1",
        "agent": {"name": "test-agent", "namespace": "test"},
        "engine": {
            "type": "opencode",
            "binaryPath": "opencode",
            "workDir": "/workspace",
            "sessionDir": "/var/lib/ach-agent/opencode/sessions",
        },
        "model": {"default": "gpt-4o-mini", "provider": "openai"},
        "limits": {},
        "channels": [
            {
                "name": "pigeon-post",
                "type": "carrierpigeon",
            }
        ],
    }
    config_file = tmp_path / "bad_channel.json"
    config_file.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_cron_channel_config() -> None:
    """CHN-02 config half: cron channel block parses schedule + session.continuity."""
    from ach_agent.config import load_config

    config = load_config(str(FIXTURES_DIR / "config_cron.json"))
    channel = config.channels[0]
    assert channel.type == "cron"
    assert channel.cron is not None
    assert channel.cron.schedule == "* * * * *"
    assert channel.session.continuity == "durable"


# ---------------------------------------------------------------------------
# D-04 / consent_tier tests (Phase 5 additive revision)
# ---------------------------------------------------------------------------


def test_consent_tier_explicit_auto(tmp_path: Path) -> None:
    """D-04: responseActions entry with consentTier="auto" loads and parses correctly."""
    import json

    from ach_agent.config.schema import AgentConfig

    cfg_data = {
        "schemaVersion": "1",
        "agent": {"name": "test-agent", "namespace": "test"},
        "engine": {
            "type": "opencode",
            "binaryPath": "opencode",
            "workDir": "/workspace",
            "sessionDir": "/var/lib/ach-agent/opencode/sessions",
        },
        "model": {"default": "gpt-4o-mini", "provider": "openai"},
        "channels": [
            {
                "name": "test-channel",
                "type": "webhook",
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
    config = AgentConfig.model_validate_json(json.dumps(cfg_data))
    block = config.channels[0].response_actions[0]
    assert block.consent_tier == "auto", (
        f"Expected consent_tier='auto', got {block.consent_tier!r}"
    )


def test_consent_tier_default_when_absent(tmp_path: Path) -> None:
    """D-04 / Pitfall 2: omitting consentTier resolves to 'consent' (backward-compat).

    Existing Phase 1–4 configs without consentTier must still validate and default
    to "consent" — the safe tier — without any schema failure.
    """
    import json

    from ach_agent.config.schema import AgentConfig

    cfg_data = {
        "schemaVersion": "1",
        "agent": {"name": "test-agent", "namespace": "test"},
        "engine": {
            "type": "opencode",
            "binaryPath": "opencode",
            "workDir": "/workspace",
            "sessionDir": "/var/lib/ach-agent/opencode/sessions",
        },
        "model": {"default": "gpt-4o-mini", "provider": "openai"},
        "channels": [
            {
                "name": "test-channel",
                "type": "webhook",
                "responseActions": [
                    {
                        "name": "channel_message",
                        "kind": "reply",
                        "inputSchema": {},
                    }
                ],
            }
        ],
    }
    config = AgentConfig.model_validate_json(json.dumps(cfg_data))
    block = config.channels[0].response_actions[0]
    assert block.consent_tier == "consent", (
        f"Expected default consent_tier='consent' when absent, got {block.consent_tier!r}"
    )


def test_consent_tier_invalid_value_rejected(tmp_path: Path) -> None:
    """D-04: an invalid consentTier value raises ValidationError (Pydantic rejects it)."""
    import json

    from pydantic import ValidationError

    from ach_agent.config.schema import AgentConfig

    cfg_data = {
        "schemaVersion": "1",
        "agent": {"name": "test-agent", "namespace": "test"},
        "engine": {
            "type": "opencode",
            "binaryPath": "opencode",
            "workDir": "/workspace",
            "sessionDir": "/var/lib/ach-agent/opencode/sessions",
        },
        "model": {"default": "gpt-4o-mini", "provider": "openai"},
        "channels": [
            {
                "name": "test-channel",
                "type": "webhook",
                "responseActions": [
                    {
                        "name": "create_issue",
                        "kind": "sideEffect",
                        "consentTier": "forced",
                        "inputSchema": {},
                    }
                ],
            }
        ],
    }
    with pytest.raises(ValidationError):
        AgentConfig.model_validate_json(json.dumps(cfg_data))

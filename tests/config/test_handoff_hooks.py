# SPDX-License-Identifier: Apache-2.0
"""channel.handoff + agent-level hooks.sessionStart/sessionSuspend — replaces prepare/cleanup."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from ach_agent.boot.secrets import collect_secret_env_names
from ach_agent.config.schema import AgentConfig

_BASE: dict = {
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
            "webhook": {"auth": {"type": "gitlab_token", "secret": {"env": "SECRET"}}},
        }
    ],
}


def _base() -> dict:
    return copy.deepcopy(_BASE)


def test_prepare_and_cleanup_are_gone() -> None:
    for field in ("prepare", "cleanup"):
        raw = _base()
        raw["channels"][0][field] = {"script": "true"}
        with pytest.raises(ValidationError):
            AgentConfig.model_validate(raw)


def test_handoff_and_hooks_parse() -> None:
    raw = _base()
    raw["channels"][0]["handoff"] = {"script": "true", "secretEnv": {"TOK": {"env": "TOK"}}}
    assert AgentConfig.model_validate(raw).channels[0].handoff.scope == "event"  # default
    raw["channels"][0]["handoff"]["scope"] = "session"
    raw["hooks"] = {"sessionStart": {"script": "true"}, "sessionSuspend": {"script": "true"}}
    cfg = AgentConfig.model_validate(raw)
    assert cfg.channels[0].handoff is not None
    assert cfg.channels[0].handoff.scope == "session"
    assert cfg.hooks.session_suspend is not None
    assert "TOK" in collect_secret_env_names(cfg)


def test_hooks_reject_secret_env() -> None:
    raw = _base()
    raw["hooks"] = {"sessionStart": {"script": "true", "secretEnv": {"X": {"env": "X"}}}}
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(raw)


def test_timeouts_fit_the_lane() -> None:
    raw = _base()
    raw["limits"]["maxInvocationSeconds"] = 60
    raw["hooks"] = {"sessionStart": {"script": "true", "timeoutSeconds": 120}}
    with pytest.raises(ValidationError, match="sessionStart"):
        AgentConfig.model_validate(raw)


def test_handoff_timeout_fits_the_lane() -> None:
    raw = _base()
    raw["limits"]["maxInvocationSeconds"] = 60
    raw["channels"][0]["handoff"] = {"script": "true", "timeoutSeconds": 120}
    with pytest.raises(ValidationError, match="handoff"):
        AgentConfig.model_validate(raw)


def test_webhook_script_forbids_handoff() -> None:
    raw = _base()
    raw["channels"][0] = {
        "name": "gitlab-register",
        "type": "webhook-script",
        "source": "gitlab",
        "webhook": {"auth": {"type": "none"}, "gitlabEvents": ["push"]},
        "script": {"script": "true"},
        "handoff": {"script": "true"},
    }
    with pytest.raises(ValidationError, match="forbids.*handoff"):
        AgentConfig.model_validate(raw)

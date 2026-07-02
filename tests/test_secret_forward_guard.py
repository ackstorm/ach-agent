# SPDX-License-Identifier: Apache-2.0
import pytest

from ach_agent.config.schema import (
    AgentBlock,
    AgentConfig,
    CapabilityAchBlock,
    CapabilityBlock,
    ChannelConfig,
    EngineBlock,
    ModelBlock,
    SecretSource,
    WebhookAuthBlock,
    WebhookBlock,
)
from ach_agent.main import collect_secret_env_names, strip_forwarded_secrets


def _base_kwargs() -> dict:
    return dict(
        schema_version="1",
        agent=AgentBlock(name="test-agent"),
        model=ModelBlock(name="openai.gpt-5", type="openai"),
        capability=CapabilityBlock(ach=CapabilityAchBlock(base_url="https://ach.example.com")),
    )


def _webhook_channel(secret_env: str) -> ChannelConfig:
    return ChannelConfig(
        name="wh",
        type="webhook",
        source="generic",
        webhook=WebhookBlock(
            auth=WebhookAuthBlock(type="hmac", secret=SecretSource(env=secret_env)),
        ),
    )


@pytest.fixture
def minimal_cfg_with_env_secret() -> AgentConfig:
    return AgentConfig(channels=[_webhook_channel("ACH_SECRET_X")], **_base_kwargs())


@pytest.fixture
def cfg_secret_also_forwarded() -> AgentConfig:
    return AgentConfig(
        channels=[_webhook_channel("ACH_SECRET_X")],
        engine=EngineBlock(forward_env=["SAFE_VAR", "ACH_SECRET_X"]),
        **_base_kwargs(),
    )


def test_collect_secret_env_names_from_config(minimal_cfg_with_env_secret):
    names = collect_secret_env_names(minimal_cfg_with_env_secret)
    assert "ACH_SECRET_X" in names


def test_strip_removes_secret_from_forward_env(cfg_secret_also_forwarded):
    # forwardEnv = ["SAFE_VAR", "ACH_SECRET_X"]; the secret name must be stripped (fail-safe),
    # SAFE_VAR kept. NO SystemExit — strip + warn, not hard-fail.
    cleaned = strip_forwarded_secrets(cfg_secret_also_forwarded)
    assert "ACH_SECRET_X" not in cleaned
    assert "SAFE_VAR" in cleaned

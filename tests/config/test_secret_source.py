# SPDX-License-Identifier: Apache-2.0
import pytest
from pydantic import ValidationError

from ach_agent.config.schema import (
    A2AAuthBlock,
    A2ABlock,
    SecretSource,
    WebhookAuthBlock,
    WebhookBlock,
)


def test_secret_source_env_only_ok():
    s = SecretSource(env="ACH_SECRET_X")
    assert s.env == "ACH_SECRET_X"


def test_secret_source_file_key_rejected():
    with pytest.raises(ValidationError):
        SecretSource(file="/p")  # file is no longer a field → extra_forbidden


def test_secret_source_neither_rejected():
    with pytest.raises(ValidationError):
        SecretSource()


def test_secret_source_invalid_env_name_rejected():
    with pytest.raises(ValidationError):
        SecretSource(env="bad-name-with-dashes")


def test_webhook_auth_requires_secret_unless_none():
    with pytest.raises(ValidationError):
        WebhookAuthBlock(type="gitlab_token")  # no secret
    WebhookAuthBlock(type="none")  # secret optional for none
    WebhookAuthBlock(type="gitlab_token", secret=SecretSource(env="ACH_SECRET_X"))


def test_a2a_auth_requires_secret():
    with pytest.raises(ValidationError):
        A2AAuthBlock()  # no secret
    A2AAuthBlock(secret=SecretSource(env="ACH_SECRET_A2A"))


def test_webhook_block_requires_auth_secret_by_default():
    # Omitting auth entirely is now a hard error (fail-closed): the default WebhookAuthBlock
    # is type="hmac" with no secret, which the validator rejects.
    with pytest.raises(ValidationError):
        WebhookBlock()


def test_a2a_block_requires_auth_secret_by_default():
    # Omitting auth entirely is now a hard error (fail-closed): the default A2AAuthBlock
    # has no secret, which the validator rejects.
    with pytest.raises(ValidationError):
        A2ABlock()

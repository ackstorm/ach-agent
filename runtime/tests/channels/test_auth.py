# SPDX-License-Identifier: MIT
"""channels.auth — webhook auth verification, ported from old-ach-agent `channels/webhook.py`."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from ach_runtime.channels.auth import verify_webhook_auth
from ach_runtime.config.schema import WebhookAuthBlock

SECRET_ENV = "ACH_SECRET_TEST_AUTH"


def test_gitlab_token_auth_accepts_matching_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "my-secret")
    auth = WebhookAuthBlock.model_validate({"type": "gitlab_token", "env": SECRET_ENV})
    assert verify_webhook_auth(auth, {"x-gitlab-token": "my-secret"}, b"{}") is True


def test_gitlab_token_auth_rejects_wrong_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "my-secret")
    auth = WebhookAuthBlock.model_validate({"type": "gitlab_token", "env": SECRET_ENV})
    assert verify_webhook_auth(auth, {"x-gitlab-token": "wrong"}, b"{}") is False


def test_gitlab_token_auth_rejects_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRET_ENV, raising=False)
    auth = WebhookAuthBlock.model_validate({"type": "gitlab_token", "env": SECRET_ENV})
    assert verify_webhook_auth(auth, {"x-gitlab-token": "anything"}, b"{}") is False


def test_hmac_auth_accepts_valid_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "hmac-secret")
    body = b'{"a":1}'
    sig = hmac.new(b"hmac-secret", body, hashlib.sha256).hexdigest()
    auth = WebhookAuthBlock.model_validate({"type": "hmac", "env": SECRET_ENV})
    assert verify_webhook_auth(auth, {"x-hub-signature-256": f"sha256={sig}"}, body) is True


def test_hmac_auth_rejects_bad_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "hmac-secret")
    auth = WebhookAuthBlock.model_validate({"type": "hmac", "env": SECRET_ENV})
    assert verify_webhook_auth(auth, {"x-hub-signature-256": "sha256=deadbeef"}, b"{}") is False


def test_header_token_auth_uses_configured_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "static-secret")
    auth = WebhookAuthBlock.model_validate(
        {"type": "header_token", "env": SECRET_ENV, "header": "X-Api-Key"}
    )
    assert verify_webhook_auth(auth, {"x-api-key": "static-secret"}, b"{}") is True
    assert verify_webhook_auth(auth, {"x-api-key": "wrong"}, b"{}") is False


def test_none_auth_always_passes() -> None:
    auth = WebhookAuthBlock.model_validate({"type": "none"})
    assert verify_webhook_auth(auth, {}, b"{}") is True


def test_non_ascii_credentials_are_rejected_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "my-secret")
    token = WebhookAuthBlock.model_validate({"type": "gitlab_token", "env": SECRET_ENV})
    signed = WebhookAuthBlock.model_validate({"type": "hmac", "env": SECRET_ENV})
    assert verify_webhook_auth(token, {"x-gitlab-token": "my-sécret"}, b"{}") is False
    assert verify_webhook_auth(signed, {"x-hub-signature-256": "sha256=é"}, b"{}") is False

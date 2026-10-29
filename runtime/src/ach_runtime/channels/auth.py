# SPDX-License-Identifier: MIT
"""Webhook auth verification — ported from old-ach-agent `channels/webhook.py` (contract §2).

Adapted to the resolved wire: `WebhookAuthBlock.env` is already a private env-alias NAME
(not a `SecretSource` union) — the control pod resolves it to a value via `os.environ`
per-call, never cached (old SEC-02).
"""

from __future__ import annotations

import hashlib
import hmac
import os

from ach_runtime.config.schema import WebhookAuthBlock


def _resolve_env_secret(env_name: str | None) -> str:
    if not env_name:
        return ""
    return os.environ.get(env_name, "")


def verify_hmac(signature: str, env_name: str | None, raw_body: bytes) -> bool:
    """GitHub-style `X-Hub-Signature-256: sha256=<hex>` over the raw body."""
    if not signature:
        return False
    sig = signature[len("sha256=") :] if signature.startswith("sha256=") else signature
    secret = _resolve_env_secret(env_name)
    if not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig.encode(), expected.encode())


def verify_header_token(header_token: str, env_name: str | None) -> bool:
    """Constant-time compare of a static shared secret carried in a configurable header."""
    secret = _resolve_env_secret(env_name)
    if not header_token or not secret:
        return False
    return hmac.compare_digest(header_token.encode(), secret.encode())


def verify_webhook_auth(
    auth: WebhookAuthBlock, lower_headers: dict[str, str], raw_body: bytes
) -> bool:
    """Dispatch by `auth.type`. `lower_headers` keys must already be lowercased by the caller."""
    if auth.type == "none":
        return True
    if auth.type == "gitlab_token":
        return verify_header_token(lower_headers.get("x-gitlab-token", ""), auth.env)
    if auth.type == "hmac":
        return verify_hmac(lower_headers.get("x-hub-signature-256", ""), auth.env, raw_body)
    if auth.type == "header_token":
        return verify_header_token(lower_headers.get(auth.header.lower(), ""), auth.env)
    return False

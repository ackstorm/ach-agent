# SPDX-License-Identifier: Apache-2.0
"""Secret env-NAME helpers: collect, strip, and resolve upstream credentials.

`collect_secret_env_names` and `strip_forwarded_secrets` handle env NAMES only — the
rendered config carries names; the process reads `os.environ[NAME]` at use time.
`resolve_model_upstream` resolves the actual credential value and must never log it,
only the header name and base URL.
"""

from __future__ import annotations

import os

import structlog

from ach_agent.config.schema import AgentConfig, HindsightMemory
from ach_agent.security.preflight import DEGRADED_ENV

log = structlog.get_logger(__name__)


def collect_secret_env_names(cfg: AgentConfig) -> list[str]:
    """Every secret.env name across webhook + a2a channel auth + the memory admin secret."""
    names: list[str] = []
    for ch in cfg.channels:
        wh = ch.webhook
        if wh is not None and wh.auth.secret is not None and wh.auth.secret.env:
            names.append(wh.auth.secret.env)
        a2a = ch.a2a
        if a2a is not None and a2a.auth.secret is not None and a2a.auth.secret.env:
            names.append(a2a.auth.secret.env)
    # memory.hindsight.auth: the admin secret joins the same forwardEnv-strip + log-redaction
    # path as channel secrets. No-auth memory config → nothing appended.
    mem = cfg.memory
    if isinstance(mem, HindsightMemory) and mem.hindsight.auth is not None:
        names.append(mem.hindsight.auth.env)
    return names


def strip_forwarded_secrets(cfg: AgentConfig) -> list[str]:
    """Fail-SAFE: remove any secret.env name from engine.forwardEnv so a misconfig can never
    leak the secret into opencode's env. Returns the cleaned forward-env list; logs a WARN for
    each stripped name (operator agreement: strip + warn, NOT hard-fail).
    """
    secret_names = set(collect_secret_env_names(cfg))
    cleaned: list[str] = []
    stripped: list[str] = []
    for name in cfg.engine.forward_env:
        (stripped if name in secret_names else cleaned).append(name)
    if stripped:
        log.warning(
            "secret env name(s) present in engine.forwardEnv — stripped so they never reach the "
            "agent (fix the config)",
            names=sorted(stripped),
        )
    return cleaned


def resolve_model_upstream(ek: str, default_base: str) -> tuple[str, str, str]:
    """Resolve the model proxy's upstream (base_url, auth header, token).

    Default: the ACH base URL + the ek_. The ACH_MODEL_* env vars swap in a raw provider
    key for local A/B testing — that bypasses ACH governance and ek-hygiene, so it is
    gated behind ACH_INSECURE_ALLOW_DEGRADED=1 (same gate as the preflight host checks).
    The credential itself is NEVER logged.
    """
    override_base = os.environ.get("ACH_MODEL_BASE_URL")
    override_header = os.environ.get("ACH_MODEL_HEADER")
    raw_token = os.environ.get("ACH_MODEL_TOKEN", "")
    if not (override_base or override_header or raw_token):
        return default_base, "x-ach-key", ek

    if os.environ.get(DEGRADED_ENV) != "1":
        log.error(
            "ACH_MODEL_* upstream override is set but the degraded gate is not. This path "
            "uses a raw provider key instead of the ek_ and bypasses ACH governance. "
            f"Set {DEGRADED_ENV}=1 to allow it (local testing only), or unset the override.",
        )
        raise SystemExit(1)

    log.warning(
        "model proxy upstream override ACTIVE — raw provider key, ek-hygiene bypassed",
        base_url=override_base or default_base,
        auth_header=override_header or "x-ach-key",
    )
    # Catch the classic 'No api key passed in' 401: ACH_MODEL_TOKEN set but its credential
    # is empty — e.g. `Bearer ${LITELLM_API_KEY}` where the var was never exported, so it
    # expanded to a bare scheme word. The credential itself is NEVER logged.
    cred = raw_token.split(" ", 1)[1] if " " in raw_token.strip() else raw_token
    if raw_token and not cred.strip():
        log.warning(
            "ACH_MODEL_TOKEN has an empty credential — only a scheme word, no key. "
            "Likely an unexpanded ${...} var. The upstream will 401 'No api key passed in.'",
        )
    return (
        override_base or default_base,
        override_header or "x-ach-key",
        raw_token or ek,
    )

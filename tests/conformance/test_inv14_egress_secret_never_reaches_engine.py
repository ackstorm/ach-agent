# SPDX-License-Identifier: Apache-2.0
"""New conformance invariant: egress service secrets never reach the engine.

Counterpart to INV-12 (ek_ secret hygiene) for egress.services[].auth secrets —
design doc §10 "Secret delivery" acceptance row: "Only H/proxy receive real values;
absent from C/E env, public config, workspace, hydration transfer, argv, logs, and
engine-readable mounts."
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_SECRET = "real-secret-do-not-leak"


def _cfg_with_secret():
    from ach_agent.config.schema import AgentConfig

    return AgentConfig.model_validate(
        {
            "schemaVersion": "1",
            "agent": {"name": "a"},
            "model": {"name": "m", "type": "openai"},
            "capability": {"type": "ach", "ach": {"baseUrl": "https://x", "environment": "prod"}},
            "egress": {
                "services": [
                    {
                        "name": "github",
                        "origin": "https://api.github.com:443",
                        "auth": {
                            "header": "Authorization",
                            "prefix": "Bearer ",
                            "secret": {"env": "ACH_SECRET_EGRESS_0"},
                            "placeholderEnv": "GH_TOKEN",
                        },
                    }
                ]
            },
        }
    )


def test_egress_secret_not_in_public_engine_config(monkeypatch: Any) -> None:
    monkeypatch.setenv("ACH_SECRET_EGRESS_0", _SECRET)
    from ach_agent.egress.resolver import resolve_services
    from ach_agent.execution.wire import PublicEngineConfig

    cfg = _cfg_with_secret()
    resolved = resolve_services(cfg.egress)
    public_cfg = PublicEngineConfig().model_copy(
        update={
            "egress_proxy_url": "http://127.0.0.1:5555",
            "egress_proxy_capability": "cap-value",
            "egress_ca_cert": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
            "egress_placeholder_env": [r.placeholder_env for r in resolved if r.placeholder_env],
        }
    )
    assert _SECRET not in public_cfg.model_dump_json()


def test_egress_secret_not_in_engine_env(monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setenv("ACH_SECRET_EGRESS_0", _SECRET)
    from ach_agent.execution.service import _egress_env
    from ach_agent.execution.wire import PublicEngineConfig

    public_cfg = PublicEngineConfig().model_copy(
        update={
            "egress_proxy_url": "http://127.0.0.1:5555",
            "egress_proxy_capability": "cap-value",
            "egress_ca_cert": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
            "egress_placeholder_env": ["GH_TOKEN"],
            "home": str(tmp_path),
        }
    )
    env = _egress_env(public_cfg)
    assert all(_SECRET not in v for v in env.values())
    assert env["GH_TOKEN"] == "non-secret"


def test_egress_secret_stripped_even_if_in_forward_env(monkeypatch: Any) -> None:
    monkeypatch.setenv("ACH_SECRET_EGRESS_0", _SECRET)
    from ach_agent.boot.secrets import collect_secret_env_names, strip_forwarded_secrets
    from ach_agent.config.schema import AgentConfig

    cfg = _cfg_with_secret()
    cfg = AgentConfig.model_validate(
        {
            **cfg.model_dump(by_alias=True, exclude_none=True),
            "engine": {"forwardEnv": ["SAFE_VAR", "ACH_SECRET_EGRESS_0"]},
        }
    )
    assert "ACH_SECRET_EGRESS_0" in collect_secret_env_names(cfg)
    cleaned = strip_forwarded_secrets(cfg)
    assert "ACH_SECRET_EGRESS_0" not in cleaned
    assert "SAFE_VAR" in cleaned


def test_egress_secret_redacted_in_logs(capsys: Any, monkeypatch: Any) -> None:
    monkeypatch.setenv("ACH_SECRET_EGRESS_0", _SECRET)
    import structlog

    from ach_agent.boot.secrets import collect_secret_env_names
    from ach_agent.engine.sanitized_env import add_secret_redaction, configure_logging

    configure_logging()
    cfg = _cfg_with_secret()
    add_secret_redaction(collect_secret_env_names(cfg))
    structlog.get_logger("conformance").info("boot", token=_SECRET)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert _SECRET not in combined
    assert "[REDACTED]" in combined


def test_ca_private_key_not_in_projection() -> None:
    from ach_agent.execution.wire import PublicEngineConfig

    public_cfg = PublicEngineConfig().model_copy(
        update={"egress_ca_cert": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----"}
    )
    assert "PRIVATE KEY" not in public_cfg.egress_ca_cert


def test_sandbox_engine_config_carries_no_egress_secret(monkeypatch: Any, tmp_path: Path) -> None:
    """INV-14 (sandboxed): the config sent to a sandbox has the gateway egress URL and the
    capability, never the secret; the claim body carries no env at all."""
    import datetime as dt

    from ach_agent.egress.resolver import resolve_services
    from ach_agent.execution.wire import PublicEngineConfig
    from ach_agent.sandbox.claims import claim_body
    from tests.sandbox.test_sessions import make_sessions

    monkeypatch.setenv("ACH_SECRET_EGRESS_0", _SECRET)
    resolve_services(_cfg_with_secret().egress)  # the secret is loaded into H

    sessions, box = make_sessions(tmp_path, egress_url="http://bot.ach.svc:8096")
    sent = sessions.engine_config(
        box,
        PublicEngineConfig(
            egress_proxy_url="http://0.0.0.0:8096",
            egress_proxy_capability="cap-value",
            egress_ca_cert="-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
        ),
    )
    assert _SECRET not in sent.model_dump_json()
    assert sent.egress_proxy_url == "http://bot.ach.svc:8096"
    body = claim_body("c", {}, "wp", dt.datetime.now(dt.UTC))
    assert _SECRET not in str(body) and "env" not in body["spec"]


def test_sandbox_key_and_egress_secrets_never_in_engine_env_names() -> None:
    """Names ach renders into the SandboxTemplate come from engine_env_names; the sandbox key
    and every egress secret NAME are excluded even if listed in forwardEnv."""
    from ach_agent.boot.roles import _engine_env_names
    from ach_agent.config.schema import AgentConfig

    raw = {
        "schemaVersion": "1",
        "agent": {"name": "a"},
        "model": {"name": "m", "type": "openai"},
        "capability": {"type": "ach", "ach": {"baseUrl": "https://x", "environment": "prod"}},
        "engine": {"forwardEnv": ["ACH_SECRET_EGRESS_0", "ACH_SANDBOX_KEY", "OK"]},
        "persistence": {"enabled": True},
        "sandbox": {
            "enabled": True,
            "warmPool": "wp",
            "gatewayHost": "h",
            "sessions": {"bucket": "b"},
        },
        "egress": {
            "services": [
                {
                    "name": "github",
                    "origin": "https://api.github.com:443",
                    "auth": {
                        "header": "Authorization",
                        "prefix": "Bearer ",
                        "secret": {"env": "ACH_SECRET_EGRESS_0"},
                        "placeholderEnv": "GH_TOKEN",
                    },
                }
            ]
        },
    }
    names = _engine_env_names(AgentConfig.model_validate(raw))
    assert "ACH_SECRET_EGRESS_0" not in names and "ACH_SANDBOX_KEY" not in names
    assert "OK" in names

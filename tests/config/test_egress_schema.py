from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import AgentConfig, EgressBlock, EgressServiceBlock


def _base_service(**overrides: object) -> dict:
    base = {
        "name": "github",
        "origin": "https://api.github.com:443",
        "auth": {
            "header": "Authorization",
            "prefix": "Bearer ",
            "secret": {"env": "ACH_SECRET_EGRESS_0"},
            "placeholderEnv": "GH_TOKEN",
        },
    }
    base.update(overrides)
    return base


def test_egress_services_must_be_non_empty() -> None:
    with pytest.raises(ValidationError):
        EgressBlock.model_validate({"services": []})


def test_service_requires_https_origin() -> None:
    with pytest.raises(ValidationError):
        EgressServiceBlock.model_validate(_base_service(origin="http://api.github.com:443"))


def test_service_origin_rejects_userinfo_and_path() -> None:
    with pytest.raises(ValidationError):
        EgressServiceBlock.model_validate(_base_service(origin="https://user@api.github.com:443"))
    with pytest.raises(ValidationError):
        EgressServiceBlock.model_validate(_base_service(origin="https://api.github.com:443/v3"))


def test_service_origin_rejects_wildcard_and_ip_literal() -> None:
    with pytest.raises(ValidationError):
        EgressServiceBlock.model_validate(_base_service(origin="https://*.github.com:443"))
    with pytest.raises(ValidationError):
        EgressServiceBlock.model_validate(_base_service(origin="https://192.0.2.1:443"))


def test_duplicate_service_names_rejected() -> None:
    with pytest.raises(ValidationError):
        EgressBlock.model_validate(
            {"services": [_base_service(name="github"), _base_service(name="github", origin="https://other.example.com:443")]}
        )


def test_duplicate_origins_rejected() -> None:
    with pytest.raises(ValidationError):
        EgressBlock.model_validate(
            {"services": [_base_service(name="a"), _base_service(name="b")]}
        )


def test_forbidden_auth_header_rejected() -> None:
    for header in ("Host", "Content-Length", "Transfer-Encoding", "Connection", "Cookie", "Proxy-Authorization"):
        with pytest.raises(ValidationError):
            EgressServiceBlock.model_validate(_base_service(auth={**_base_service()["auth"], "header": header}))


def test_placeholder_env_collides_with_forwarded_env_rejected() -> None:
    # ACH_TOKEN is a protected/managed name — must be rejected as a placeholderEnv.
    with pytest.raises(ValidationError):
        EgressServiceBlock.model_validate(
            _base_service(auth={**_base_service()["auth"], "placeholderEnv": "ACH_TOKEN"})
        )


@pytest.mark.parametrize(
    "name",
    ["HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "NO_PROXY", "no_proxy", "ALL_PROXY",
     "SSL_CERT_FILE", "SSL_CERT_DIR", "HOME", "PATH", "TMPDIR", "OPENCODE_CONFIG"],
)
def test_placeholder_env_rejects_proxy_trust_and_pinned_names(name: str) -> None:
    with pytest.raises(ValidationError):
        EgressServiceBlock.model_validate(
            _base_service(auth={**_base_service()["auth"], "placeholderEnv": name})
        )


def test_forward_env_colliding_with_egress_env_rejected() -> None:
    cfg_kwargs = {
        "schemaVersion": "1",
        "agent": {"name": "a"},
        "model": {"name": "m", "type": "openai"},
        "capability": {"type": "ach", "ach": {"baseUrl": "https://x", "environment": "prod"}},
        "egress": {"services": [_base_service()]},
    }
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**cfg_kwargs, "engine": {"forwardEnv": ["GH_TOKEN"]}})
    with pytest.raises(ValidationError):
        AgentConfig.model_validate({**cfg_kwargs, "engine": {"forwardEnv": ["HTTPS_PROXY"]}})


def test_agent_config_rejects_unknown_egress_field() -> None:
    with pytest.raises(ValidationError):
        AgentConfig.model_validate(
            {
                "schemaVersion": "1",
                "agent": {"name": "a"},
                "model": {"name": "m", "type": "openai"},
                "capability": {"type": "ach", "ach": {"baseUrl": "https://x", "environment": "prod"}},
                "egress": {"services": [], "bogusField": True},
            }
        )

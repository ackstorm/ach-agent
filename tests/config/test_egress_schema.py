from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import AgentConfig, EgressBlock, EgressServiceAccess, EgressServiceBlock


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


def test_egress_absent_is_valid() -> None:
    block = EgressBlock.model_validate({"services": []})
    assert block.default_action == "deny"
    assert block.services == []


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
        EgressServiceBlock.model_validate(_base_service(placeholderEnv="ACH_TOKEN"))


def test_access_default_deny_empty_allow_denies_everything() -> None:
    access = EgressServiceAccess.model_validate({"allow": []})
    assert access.default_action == "deny"
    assert access.allow == []


def test_access_path_prefix_must_end_in_slash() -> None:
    with pytest.raises(ValidationError):
        EgressServiceAccess.model_validate(
            {"allow": [{"methods": ["GET"], "pathPrefix": "/api/v4/projects/123"}]}
        )


def test_access_rule_requires_exactly_one_of_path_exact_or_prefix() -> None:
    with pytest.raises(ValidationError):
        EgressServiceAccess.model_validate(
            {"allow": [{"methods": ["GET"], "pathPrefix": "/a/", "pathExact": "/b"}]}
        )
    with pytest.raises(ValidationError):
        EgressServiceAccess.model_validate({"allow": [{"methods": ["GET"]}]})


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

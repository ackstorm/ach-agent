from __future__ import annotations

from ach_agent.egress.policy import ResolvedService, match_service


def _svc(name: str, host: str) -> ResolvedService:
    return ResolvedService(
        name=name, host=host, port=443, header="Authorization", prefix="Bearer ",
        secret="x", placeholder_env="",
    )


_SERVICES = [_svc("github", "api.github.com"), _svc("gitlab", "gitlab.example.com")]


def test_match_service_exact_origin_case_insensitive_host() -> None:
    svc = match_service(_SERVICES, "API.GitHub.com", 443)
    assert svc is not None and svc.name == "github"


def test_match_service_unmatched_host_returns_none() -> None:
    assert match_service(_SERVICES, "evil.example.com", 443) is None


def test_match_service_wrong_port_returns_none() -> None:
    assert match_service(_SERVICES, "api.github.com", 8443) is None

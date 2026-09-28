from __future__ import annotations

import pytest

from ach_agent.config.schema import EgressServiceAccess
from ach_agent.egress.policy import ResolvedService, is_authorized, match_service


def _svc(name: str, host: str, access: EgressServiceAccess | None = None) -> ResolvedService:
    return ResolvedService(
        name=name, host=host, port=443, header="Authorization", prefix="Bearer ",
        secret="x", placeholder_env="", access=access,
    )


_GITLAB_ACCESS = EgressServiceAccess.model_validate({
    "allow": [
        {"methods": ["GET"], "pathPrefix": "/api/v4/projects/123/"},
        {"methods": ["POST"], "pathExact": "/api/v4/projects/123/merge_requests/42/notes"},
    ]
})
_SERVICES = [_svc("github", "api.github.com"), _svc("gitlab", "gitlab.example.com", _GITLAB_ACCESS)]


def test_match_service_exact_origin_case_insensitive_host() -> None:
    assert match_service(_SERVICES, "API.GitHub.com", 443).name == "github"  # type: ignore[union-attr]
    assert match_service(_SERVICES, "evil.example.com", 443) is None
    assert match_service(_SERVICES, "api.github.com", 8443) is None


def test_no_access_block_authorizes_every_method_and_safe_path() -> None:
    assert is_authorized(None, "DELETE", "/anything")


def test_access_rules() -> None:
    assert is_authorized(_GITLAB_ACCESS, "GET", "/api/v4/projects/123/issues")
    assert is_authorized(_GITLAB_ACCESS, "POST", "/api/v4/projects/123/merge_requests/42/notes")
    assert not is_authorized(_GITLAB_ACCESS, "DELETE", "/api/v4/projects/123/issues")
    assert not is_authorized(_GITLAB_ACCESS, "GET", "/api/v4/projects/999/issues")
    assert not is_authorized(_GITLAB_ACCESS, "POST", "/api/v4/projects/123/merge_requests/99/notes")


def test_path_prefix_matches_subtree_only() -> None:
    assert not is_authorized(_GITLAB_ACCESS, "GET", "/api/v4/projects/123")
    assert not is_authorized(_GITLAB_ACCESS, "GET", "/api/v4/projects/1234/")
    assert is_authorized(_GITLAB_ACCESS, "GET", "/api/v4/projects/123/")


def test_query_is_not_an_authorization_input() -> None:
    # design §7: path evaluated before the query; query is forwarded, not authorized.
    assert is_authorized(_GITLAB_ACCESS, "POST", "/api/v4/projects/123/merge_requests/42/notes?x=1")
    assert is_authorized(None, "GET", "/search?q=a//b/../c")


@pytest.mark.parametrize(
    "path",
    [
        "/api/v4/projects/123/../456/issues",
        "/api/v4/projects/123/./issues",
        "/api/v4/projects/123/%2e%2e/456",
        "/api/v4/projects/123/%2E/x",
        "/api/v4/projects/123%2f..%2f456",
        "/api/v4/projects/123/%5cx",
        "/api/v4/projects/123/%252e%252e/456",  # double-encoded
        "/api/v4/projects//123/issues",
        "/api/v4/projects/123/a\\b",  # single backslash
        "/api/v4/projects/123/%zz",  # invalid escape
        "relative/path",
    ],
)
def test_unsafe_paths_rejected(path: str) -> None:
    assert not is_authorized(None, "GET", path)

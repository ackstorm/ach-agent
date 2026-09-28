from __future__ import annotations

from ach_agent.config.schema import EgressBlock
from ach_agent.egress.policy import is_authorized, match_service


def _cfg() -> EgressBlock:
    return EgressBlock.model_validate({
        "services": [
            {
                "name": "github",
                "origin": "https://api.github.com:443",
                "auth": {"header": "Authorization", "prefix": "Bearer ", "secret": {"env": "E0"}},
            },
            {
                "name": "gitlab",
                "origin": "https://gitlab.example.com:443",
                "auth": {"header": "PRIVATE-TOKEN", "secret": {"env": "E1"}},
                "access": {
                    "allow": [
                        {"methods": ["GET"], "pathPrefix": "/api/v4/projects/123/"},
                        {"methods": ["POST"], "pathExact": "/api/v4/projects/123/merge_requests/42/notes"},
                    ]
                },
            },
        ]
    })


def test_match_service_by_exact_origin() -> None:
    svc = match_service(_cfg(), host="api.github.com", port=443)
    assert svc is not None
    assert svc.name == "github"


def test_match_service_unmatched_host_returns_none() -> None:
    assert match_service(_cfg(), host="evil.example.com", port=443) is None


def test_match_service_wrong_port_returns_none() -> None:
    assert match_service(_cfg(), host="api.github.com", port=8443) is None


def test_match_service_hostname_case_insensitive() -> None:
    svc = match_service(_cfg(), host="API.GitHub.com", port=443)
    assert svc is not None and svc.name == "github"


def test_no_access_block_authorizes_every_method_and_path() -> None:
    svc = match_service(_cfg(), host="api.github.com", port=443)
    assert svc is not None
    assert is_authorized(svc, method="DELETE", path="/anything")


def test_access_block_allows_matching_rule() -> None:
    svc = match_service(_cfg(), host="gitlab.example.com", port=443)
    assert svc is not None
    assert is_authorized(svc, method="GET", path="/api/v4/projects/123/issues")
    assert is_authorized(svc, method="POST", path="/api/v4/projects/123/merge_requests/42/notes")


def test_access_block_denies_unmatched_method_or_path() -> None:
    svc = match_service(_cfg(), host="gitlab.example.com", port=443)
    assert svc is not None
    assert not is_authorized(svc, method="DELETE", path="/api/v4/projects/123/issues")
    assert not is_authorized(svc, method="GET", path="/api/v4/projects/999/issues")
    assert not is_authorized(svc, method="POST", path="/api/v4/projects/123/merge_requests/99/notes")


def test_access_path_prefix_matches_subtree_only() -> None:
    svc = match_service(_cfg(), host="gitlab.example.com", port=443)
    assert svc is not None
    # "/api/v4/projects/123" (no trailing slash, a sibling-like prefix) must NOT match
    # "/api/v4/projects/123/" — design §7 path handling.
    assert not is_authorized(svc, method="GET", path="/api/v4/projects/123")
    assert is_authorized(svc, method="GET", path="/api/v4/projects/123/")


def test_dot_segments_and_encoded_slashes_rejected() -> None:
    svc = match_service(_cfg(), host="gitlab.example.com", port=443)
    assert svc is not None
    assert not is_authorized(svc, method="GET", path="/api/v4/projects/123/../456/issues")
    assert not is_authorized(svc, method="GET", path="/api/v4/projects/123%2f..%2f456")
    assert not is_authorized(svc, method="GET", path="/api/v4/projects//123/issues")

"""Contract tests for the role healthcheck command."""

# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ach_agent import healthcheck


@pytest.mark.parametrize("role", ["channels", "harness", "engine"])
@pytest.mark.parametrize("check", ["startup", "readiness", "liveness"])
def test_healthcheck_main_delegates_role_and_check(monkeypatch, role: str, check: str) -> None:
    seen: list[tuple[str, str]] = []
    def request(actual_role: str, actual_check: str) -> bool:
        seen.append((actual_role, actual_check))
        return True

    monkeypatch.setattr(healthcheck, "_request", request)

    assert healthcheck.main(["--role", role, "--check", check]) == 0
    assert seen == [(role, check)]


def test_healthcheck_returns_failure_when_endpoint_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(healthcheck, "_request", lambda _role, _check: False)
    assert healthcheck.main(["--role", "engine", "--check", "readiness"]) == 1


@pytest.mark.parametrize("role", ["channels", "harness", "engine"])
def test_request_uses_role_endpoint_and_status(monkeypatch, role: str) -> None:
    calls: list[dict[str, object]] = []

    class Client:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, path: str):
            assert path == "/readyz"
            return SimpleNamespace(status_code=200)

    monkeypatch.setattr(healthcheck.httpx, "Client", Client)
    assert healthcheck._request(role, "readiness")
    assert calls
    if role == "channels":
        assert calls[0]["base_url"] == "http://127.0.0.1:8080"

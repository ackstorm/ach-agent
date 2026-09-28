from __future__ import annotations

import pytest

from ach_agent.config.schema import EgressBlock
from ach_agent.egress.resolver import EgressConfigError, resolve_services


def _cfg(origin: str = "https://API.github.com:443") -> EgressBlock:
    return EgressBlock.model_validate({
        "services": [{
            "name": "github",
            "origin": origin,
            "auth": {"header": "Authorization", "prefix": "Bearer ",
                     "secret": {"env": "TEST_EGRESS_SECRET_0"}, "placeholderEnv": "GH_TOKEN"},
        }]
    })


def test_resolve_services_reads_env_and_parses_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_EGRESS_SECRET_0", "s3cr3t-value")
    [r] = resolve_services(_cfg())
    assert (r.name, r.host, r.port, r.secret, r.placeholder_env) == (
        "github", "api.github.com", 443, "s3cr3t-value", "GH_TOKEN"
    )
    assert "s3cr3t-value" not in repr(r)


def test_origin_without_port_defaults_to_443(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_EGRESS_SECRET_0", "v")
    assert resolve_services(_cfg("https://api.github.com"))[0].port == 443


@pytest.mark.parametrize("value", [None, "", "   ", "bad\r\nvalue"])
# NUL is not parametrized: os.environ cannot hold a NUL byte on POSIX (setenv raises
# ValueError before resolve_services ever runs) — the resolver's NUL check is
# defense-in-depth for a value that can't actually arrive via os.environ.
def test_bad_secret_fails_closed(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("TEST_EGRESS_SECRET_0", raising=False)
    else:
        monkeypatch.setenv("TEST_EGRESS_SECRET_0", value)
    with pytest.raises(EgressConfigError):
        resolve_services(_cfg())

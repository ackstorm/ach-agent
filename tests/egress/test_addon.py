from __future__ import annotations

import pytest
from mitmproxy.test import taddons, tflow

from ach_agent.egress.addon import EgressAddon
from ach_agent.egress.policy import ResolvedService

_SECRET = "real-secret-value"


def _service() -> ResolvedService:
    return ResolvedService(
        name="github",
        host="api.github.com",
        port=443,
        header="Authorization",
        prefix="Bearer ",
        secret=_SECRET,
        placeholder_env="GH_TOKEN",
    )


def _flow(
    host: str = "api.github.com",
    port: int = 443,
    scheme: str = "https",
    method: str = "GET",
    path: str = "/rate_limit",
) -> tflow.HTTPFlow:
    flow = tflow.tflow()
    flow.request.scheme = scheme
    flow.request.host = host
    flow.request.port = port
    flow.request.method = method
    flow.request.path = path
    return flow


def _run(addon: EgressAddon, flow: tflow.HTTPFlow) -> None:
    with taddons.context(addon):
        addon.request(flow)


def _no_secret(flow: tflow.HTTPFlow) -> bool:
    return all(_SECRET not in v for v in flow.request.headers.values())


def test_injects_configured_header_replacing_caller_value() -> None:
    flow = _flow()
    flow.request.headers["authorization"] = "Bearer caller-supplied-fake"
    flow.request.headers.add("Authorization", "Bearer duplicate")
    _run(EgressAddon([_service()]), flow)
    assert flow.request.headers.get_all("Authorization") == [f"Bearer {_SECRET}"]
    assert flow.response is None


def test_strips_proxy_authorization_before_forward() -> None:
    flow = _flow()
    flow.request.headers["Proxy-Authorization"] = "Basic capability"
    _run(EgressAddon([_service()]), flow)
    assert "Proxy-Authorization" not in flow.request.headers


def test_plain_http_to_service_origin_passes_through_untouched() -> None:
    # http://api.github.com:443/ must not get the secret injected — scope is credential
    # substitution only, no destination filtering, but also no cleartext leakage.
    flow = _flow(scheme="http")
    _run(EgressAddon([_service()]), flow)
    assert flow.response is None
    assert _no_secret(flow)


def test_unmatched_host_passes_through_untouched() -> None:
    flow = _flow(host="evil.example.com")
    flow.request.headers["Authorization"] = "Bearer caller-token"
    _run(EgressAddon([_service()]), flow)
    assert flow.response is None
    assert flow.request.headers.get("Authorization") == "Bearer caller-token"


def test_hook_exception_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bug in matching/injection must deny (502), never forward with stale state."""

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("simulated bug")

    monkeypatch.setattr("ach_agent.egress.addon.match_service", _boom)
    flow = _flow()
    _run(EgressAddon([_service()]), flow)
    assert flow.response is not None and flow.response.status_code == 502
    assert _no_secret(flow)

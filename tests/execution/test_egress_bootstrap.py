# SPDX-License-Identifier: Apache-2.0
"""Engine bootstrap projection of the egress proxy into env vars (design §8b).

Engine-wide by design: opencode/pi's own process and every child (bun/node, gh, glab)
inherit HTTP(S)_PROXY/NO_PROXY and the CA bundle vars. Loopback (model/MCP proxies) must
never recurse through the egress proxy — NO_PROXY covers every loopback form.
"""

from __future__ import annotations

import stat
from pathlib import Path

from ach_agent.execution.service import _egress_env, _engine_config, _write_egress_ca_bundle
from ach_agent.execution.wire import PublicEngineConfig

_CA = "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n"


def _public(home: Path, placeholders: list[str] | None = None) -> PublicEngineConfig:
    return PublicEngineConfig().model_copy(
        update={
            "home": str(home),
            "egress_proxy_url": "http://127.0.0.1:5555",
            "egress_proxy_capability": "cap-value",
            "egress_ca_cert": _CA,
            "egress_placeholder_env": placeholders or ["GH_TOKEN"],
        }
    )


def test_no_egress_adds_nothing(tmp_path: Path) -> None:
    public = PublicEngineConfig().model_copy(update={"home": str(tmp_path)})
    assert _egress_env(public) == {}
    _write_egress_ca_bundle(public)
    assert list(tmp_path.iterdir()) == []


def test_egress_env_projection(tmp_path: Path) -> None:
    env = _egress_env(_public(tmp_path))

    proxy_url = "http://ach-egress:cap-value@127.0.0.1:5555"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == proxy_url
    assert env["HTTP_PROXY"] == env["http_proxy"] == proxy_url
    assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost,::1"
    assert env["GH_TOKEN"] == "non-secret"
    ca_path = str(tmp_path / ".ach-egress-ca-bundle.pem")
    assert env["SSL_CERT_FILE"] == env["NODE_EXTRA_CA_CERTS"] == ca_path


def test_env_projection_writes_no_file(tmp_path: Path) -> None:
    # _engine_config runs on every acquire; it must not touch the shared bundle.
    _engine_config(_public(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_bundle_written_atomically_with_proxy_ca(tmp_path: Path) -> None:
    public = _public(tmp_path)
    _write_egress_ca_bundle(public)
    ca_path = Path(_egress_env(public)["SSL_CERT_FILE"])
    assert _CA.strip() in ca_path.read_text()
    assert stat.S_IMODE(ca_path.stat().st_mode) == 0o644
    assert [p.name for p in tmp_path.iterdir()] == [ca_path.name]  # no .tmp left behind


def test_multiple_placeholders_all_non_secret(tmp_path: Path) -> None:
    env = _egress_env(_public(tmp_path, ["GH_TOKEN", "GITLAB_TOKEN_PLACEHOLDER"]))
    assert env["GH_TOKEN"] == "non-secret"
    assert env["GITLAB_TOKEN_PLACEHOLDER"] == "non-secret"

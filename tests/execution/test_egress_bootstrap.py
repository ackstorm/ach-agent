# SPDX-License-Identifier: Apache-2.0
"""Engine bootstrap projection of the egress proxy into env vars (design §8b).

Engine-wide by design: opencode/pi's own process and every child (bun/node, gh, glab)
inherit HTTP(S)_PROXY/NO_PROXY/SSL_CERT_FILE. Loopback (model/MCP proxies) must never
recurse through the egress proxy — NO_PROXY covers every loopback form.
"""

from __future__ import annotations

import stat
from pathlib import Path

from ach_agent.execution.service import _egress_env
from ach_agent.execution.wire import PublicEngineConfig


def test_no_egress_adds_nothing(tmp_path: Path) -> None:
    public = PublicEngineConfig()
    assert _egress_env(public, str(tmp_path)) == {}


def test_egress_env_projection(tmp_path: Path) -> None:
    public = PublicEngineConfig().model_copy(
        update={
            "egress_proxy_url": "http://127.0.0.1:5555",
            "egress_proxy_capability": "cap-value",
            "egress_ca_cert": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n",
            "egress_placeholder_env": ["GH_TOKEN"],
        }
    )
    env = _egress_env(public, str(tmp_path))

    proxy_url = "http://ach-egress:cap-value@127.0.0.1:5555"
    assert env["HTTPS_PROXY"] == env["https_proxy"] == proxy_url
    assert env["HTTP_PROXY"] == env["http_proxy"] == proxy_url
    assert env["NO_PROXY"] == env["no_proxy"] == "127.0.0.1,localhost,::1"
    assert env["GH_TOKEN"] == "non-secret"

    ca_path = env["SSL_CERT_FILE"]
    contents = Path(ca_path).read_text()
    assert "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----" in contents
    assert stat.S_IMODE(Path(ca_path).stat().st_mode) == 0o644


def test_multiple_placeholders_all_non_secret(tmp_path: Path) -> None:
    public = PublicEngineConfig().model_copy(
        update={
            "egress_proxy_url": "http://127.0.0.1:5555",
            "egress_proxy_capability": "cap",
            "egress_ca_cert": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----\n",
            "egress_placeholder_env": ["GH_TOKEN", "GITLAB_TOKEN_PLACEHOLDER"],
        }
    )
    env = _egress_env(public, str(tmp_path))
    assert env["GH_TOKEN"] == "non-secret"
    assert env["GITLAB_TOKEN_PLACEHOLDER"] == "non-secret"

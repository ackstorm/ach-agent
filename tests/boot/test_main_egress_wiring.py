# SPDX-License-Identifier: Apache-2.0
"""Narrow wiring test for the egress projection on PublicEngineConfig (design §6, §8).

`_run_harness`'s boot path has no unit seam today (it's an integration-shaped
function) — this only exercises the wire model itself. Startup-failure behavior is
covered by tests/egress/test_proxy.py (EgressStartupError) plus the absence of a
try/except at the main.py call site (verified by direct code read, not tested here).
"""

from __future__ import annotations

from ach_agent.execution.wire import PublicEngineConfig


def test_public_engine_config_default_has_no_egress() -> None:
    public_cfg = PublicEngineConfig()
    assert public_cfg.egress_proxy_url == ""
    assert public_cfg.egress_proxy_capability == ""
    assert public_cfg.egress_ca_cert == ""
    assert public_cfg.egress_placeholder_env == []


def test_public_engine_config_egress_fields_roundtrip() -> None:
    public_cfg = PublicEngineConfig().model_copy(
        update={
            "egress_proxy_url": "http://127.0.0.1:5555",
            "egress_proxy_capability": "cap-value",
            "egress_ca_cert": "-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
            "egress_placeholder_env": ["GH_TOKEN"],
        }
    )
    dumped = public_cfg.model_dump(by_alias=True)
    assert dumped["egressProxyUrl"] == "http://127.0.0.1:5555"
    assert dumped["egressProxyCapability"] == "cap-value"
    assert dumped["egressCaCert"].startswith("-----BEGIN CERTIFICATE-----")
    assert dumped["egressPlaceholderEnv"] == ["GH_TOKEN"]

    restored = PublicEngineConfig.model_validate(dumped)
    assert restored.egress_proxy_url == "http://127.0.0.1:5555"
    assert restored.egress_placeholder_env == ["GH_TOKEN"]

# SPDX-License-Identifier: Apache-2.0
"""The dev-only model-proxy upstream override must be gated: it swaps the ek_ for a raw
provider key, so an unset ACH_INSECURE_ALLOW_DEGRADED must abort the boot."""

import pytest

from ach_agent.main import _resolve_model_upstream


def test_no_override_returns_ach_defaults(monkeypatch):
    monkeypatch.delenv("ACH_MODEL_BASE_URL", raising=False)
    monkeypatch.delenv("ACH_MODEL_HEADER", raising=False)
    monkeypatch.delenv("ACH_MODEL_TOKEN", raising=False)
    base, header, token = _resolve_model_upstream("ek_live", "https://ach.example")
    assert (base, header, token) == ("https://ach.example", "x-ach-key", "ek_live")


def test_override_without_degraded_flag_exits(monkeypatch):
    monkeypatch.setenv("ACH_MODEL_BASE_URL", "http://litellm.local")
    monkeypatch.delenv("ACH_INSECURE_ALLOW_DEGRADED", raising=False)
    with pytest.raises(SystemExit) as exc:
        _resolve_model_upstream("ek_live", "https://ach.example")
    assert exc.value.code == 1


def test_override_with_degraded_flag_is_allowed(monkeypatch):
    monkeypatch.setenv("ACH_MODEL_BASE_URL", "http://litellm.local")
    monkeypatch.setenv("ACH_MODEL_TOKEN", "Bearer sk-test")
    monkeypatch.setenv("ACH_INSECURE_ALLOW_DEGRADED", "1")
    base, header, token = _resolve_model_upstream("ek_live", "https://ach.example")
    assert base == "http://litellm.local"
    assert token == "Bearer sk-test"

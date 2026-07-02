# SPDX-License-Identifier: Apache-2.0
from ach_agent.config.schema import SecretSource, resolve_secret


def test_resolve_env(monkeypatch):
    monkeypatch.setenv("ACH_SECRET_T", "  s3cr3t\n")
    assert resolve_secret(SecretSource(env="ACH_SECRET_T")) == "s3cr3t"


def test_resolve_env_unset_returns_none(monkeypatch):
    monkeypatch.delenv("ACH_SECRET_MISSING", raising=False)
    assert resolve_secret(SecretSource(env="ACH_SECRET_MISSING")) is None

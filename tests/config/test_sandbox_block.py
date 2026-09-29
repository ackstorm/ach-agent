# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import AgentConfig
from tests.config.test_handoff_hooks import _base

_SB = {"enabled": True, "warmPool": "wp", "gatewayHost": "h", "sessions": {"bucket": "b"}}


def _cfg(**extra: object) -> AgentConfig:
    return AgentConfig.model_validate({**_base(), **extra})


def test_sandbox_defaults_off() -> None:
    assert _cfg().sandbox.enabled is False


def test_requires_persistence() -> None:
    with pytest.raises(ValidationError, match="persistence"):
        _cfg(sandbox=_SB)


def test_requires_bucket_pool_and_host() -> None:
    for missing in ("warmPool", "gatewayHost"):
        with pytest.raises(ValidationError, match=missing):
            _cfg(persistence={"enabled": True}, sandbox={**_SB, missing: ""})
    with pytest.raises(ValidationError, match="bucket"):
        _cfg(persistence={"enabled": True}, sandbox={**_SB, "sessions": {}})


def test_defaults() -> None:
    sb = _cfg(persistence={"enabled": True}, sandbox=_SB).sandbox
    assert sb.idle_seconds == 900
    assert sb.key_env == "ACH_SANDBOX_KEY"
    assert sb.egress_port == 8096
    assert sb.sessions.cache_ttl_seconds == 86400

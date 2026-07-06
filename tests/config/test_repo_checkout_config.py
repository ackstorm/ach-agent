from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import EngineBlock, RepoCheckoutBlock


def test_defaults_disabled() -> None:
    rc = RepoCheckoutBlock()
    assert rc.enabled is False
    assert rc.tmp_base == "/tmp/gitlab"
    assert rc.ttl_seconds == 3600.0


def test_enabled_requires_server_id() -> None:
    with pytest.raises(ValidationError, match="mcpServerId is required"):
        RepoCheckoutBlock(enabled=True)


def test_enabled_with_server_id_ok() -> None:
    rc = RepoCheckoutBlock.model_validate({"enabled": True, "mcpServerId": "gitlab"})
    assert rc.mcp_server_id == "gitlab"


def test_engineblock_default_has_repo_checkout() -> None:
    eb = EngineBlock()
    assert eb.repo_checkout.enabled is False

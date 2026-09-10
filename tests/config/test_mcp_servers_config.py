from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from ach_agent.config.schema import (
    AgentConfig,
    EngineBlock,
    LocalMcpServer,
    McpServerConfig,
    RemoteMcpServer,
)

_ADAPTER = TypeAdapter(dict[str, McpServerConfig])


def test_local_parses() -> None:
    m = _ADAPTER.validate_python(
        {"fs": {"type": "local", "command": "docker", "args": ["run", "--rm", "mcp/filesystem"]}}
    )
    e = m["fs"]
    assert isinstance(e, LocalMcpServer)
    assert e.command == "docker"
    assert e.args == ["run", "--rm", "mcp/filesystem"]
    assert e.env == []


def test_remote_parses() -> None:
    m = _ADAPTER.validate_python(
        {
            "other": {
                "type": "remote",
                "url": "https://x/mcp",
                "headers": {"Authorization": "Bearer ${env:T}"},
            }
        }
    )
    e = m["other"]
    assert isinstance(e, RemoteMcpServer)
    assert e.url == "https://x/mcp"
    assert e.headers == {"Authorization": "Bearer ${env:T}"}


def test_repo_checkout_is_rejected() -> None:
    """Removed: the repo now arrives through the channel-owned workspace, not an MCP
    resource the harness fronts. A config still carrying the block must fail loudly rather
    than parse into a server nothing hosts."""
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python(
            {"x": {"type": "repoCheckout", "repoCheckout": {"sourceMcpServerId": "g"}}}
        )


def test_local_requires_command() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"x": {"type": "local"}})


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"x": {"type": "bogus"}})


def test_engineblock_has_no_repo_checkout() -> None:
    # The block moved out of engine to top-level mcpServers.
    assert not hasattr(EngineBlock(), "repo_checkout")


def test_agentconfig_mcp_servers_default_empty() -> None:
    cfg = AgentConfig.model_validate(
        {
            "schemaVersion": "1",
            "agent": {"name": "a"},
            "model": {"name": "m", "type": "openai"},
            "capability": {"type": "ach", "ach": {"baseUrl": "https://ach"}},
        }
    )
    assert cfg.mcp_servers == {}

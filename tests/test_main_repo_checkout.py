from __future__ import annotations

from ach_agent.engine.hydrate import McpServer
from ach_agent.main import resolve_repo_archive_endpoint


def test_resolve_finds_by_id() -> None:
    servers = [McpServer(id="gitlab", endpoint="https://mcp/gl"), McpServer(id="jira", endpoint="x")]
    assert resolve_repo_archive_endpoint(servers, "gitlab") == "https://mcp/gl"


def test_resolve_missing_returns_none() -> None:
    servers = [McpServer(id="jira", endpoint="x")]
    assert resolve_repo_archive_endpoint(servers, "gitlab") is None

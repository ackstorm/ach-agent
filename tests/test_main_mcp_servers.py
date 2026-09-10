from __future__ import annotations

from ach_agent.config.schema import LocalMcpServer, RemoteMcpServer
from ach_agent.main import collect_passthrough_mcp


def test_local_and_remote_both_pass_through() -> None:
    """Every mcpServers entry is now a passthrough — the harness hosts no MCP of its own
    under this block since repoCheckout was removed."""
    out = collect_passthrough_mcp(
        {
            "fs": LocalMcpServer(type="local", command="docker", args=["run"]),
            "other": RemoteMcpServer(type="remote", url="https://x/mcp"),
        }
    )
    assert set(out) == {"fs", "other"}
    assert out["fs"]["type"] == "local"
    assert out["other"]["type"] == "remote"

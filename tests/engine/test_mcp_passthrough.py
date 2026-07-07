from __future__ import annotations

import pytest

from ach_agent.config.schema import LocalMcpServer, RemoteMcpServer
from ach_agent.engine.mcp_passthrough import to_opencode_entry


def test_local_to_entry() -> None:
    spec = LocalMcpServer(type="local", command="docker", args=["run", "--rm", "mcp/fs"])
    assert to_opencode_entry(spec) == {
        "type": "local",
        "command": ["docker", "run", "--rm", "mcp/fs"],
        "enabled": True,
    }


def test_local_env_resolved_from_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "val1")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    spec = LocalMcpServer(type="local", command="x", env=["MY_VAR", "MISSING_VAR"])
    entry = to_opencode_entry(spec)
    assert entry["environment"] == {"MY_VAR": "val1"}  # missing name omitted


def test_remote_to_entry() -> None:
    spec = RemoteMcpServer(type="remote", url="https://x/mcp")
    assert to_opencode_entry(spec) == {"type": "remote", "url": "https://x/mcp", "enabled": True}


def test_remote_headers_expand_env_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "sekret")
    spec = RemoteMcpServer(
        type="remote", url="https://x/mcp", headers={"Authorization": "Bearer ${env:TOKEN}"}
    )
    entry = to_opencode_entry(spec)
    assert entry["headers"] == {"Authorization": "Bearer sekret"}

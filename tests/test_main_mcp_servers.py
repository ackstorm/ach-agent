from __future__ import annotations

from ach_agent.config.schema import (
    LocalMcpServer,
    RemoteMcpServer,
    RepoCheckoutParams,
    RepoCheckoutServer,
)
from ach_agent.main import collect_passthrough_mcp, find_repo_checkout


def _repo() -> RepoCheckoutServer:
    return RepoCheckoutServer(
        type="repoCheckout", repo_checkout=RepoCheckoutParams(source_mcp_server_id="mcp-gitlab-ro")
    )


def test_collect_passthrough_skips_repocheckout() -> None:
    servers = {
        "repo-checkout": _repo(),
        "fs": LocalMcpServer(type="local", command="docker", args=["run"]),
        "other": RemoteMcpServer(type="remote", url="https://x/mcp"),
    }
    out = collect_passthrough_mcp(servers)
    assert set(out) == {"fs", "other"}  # repoCheckout is NOT passthrough
    assert out["fs"]["type"] == "local"
    assert out["other"]["type"] == "remote"


def test_find_repo_checkout_returns_name_and_params() -> None:
    servers = {"fs": LocalMcpServer(type="local", command="x"), "repo-checkout": _repo()}
    found = find_repo_checkout(servers)
    assert found is not None
    name, params = found
    assert name == "repo-checkout"
    assert params.source_mcp_server_id == "mcp-gitlab-ro"


def test_find_repo_checkout_none_when_absent() -> None:
    assert find_repo_checkout({"fs": LocalMcpServer(type="local", command="x")}) is None

from __future__ import annotations

from ach_agent.channels.message_event import MessageEvent
from ach_agent.engine.hydrate import McpServer
from ach_agent.main import build_engine_prompt, resolve_repo_archive_endpoint


def test_resolve_finds_by_id() -> None:
    servers = [
        McpServer(id="gitlab", endpoint="https://mcp/gl"),
        McpServer(id="jira", endpoint="x"),
    ]
    assert resolve_repo_archive_endpoint(servers, "gitlab") == "https://mcp/gl"


def test_resolve_missing_returns_none() -> None:
    servers = [McpServer(id="jira", endpoint="x")]
    assert resolve_repo_archive_endpoint(servers, "gitlab") is None


def _mr_event(head_sha: str | None) -> MessageEvent:
    dc = {"project_id": 42, "kind": "merge_request", "mr_iid": 7}
    if head_sha:
        dc["head_sha"] = head_sha
    return MessageEvent(
        idempotency_key="i",
        session_key="42:7",
        channel_name="gl",
        payload={"object_attributes": {"iid": 7, "title": "x"}},
        delivery_context=dc,
        source_trait="sync",
    )


def test_prompt_hint_added_when_enabled_and_sha() -> None:
    out = build_engine_prompt(_mr_event("9af2c1e0"), repo_checkout_enabled=True)
    assert "checkout_repo(project=42, ref=9af2c1e0)" in out


def test_prompt_hint_absent_when_disabled() -> None:
    out = build_engine_prompt(_mr_event("9af2c1e0"), repo_checkout_enabled=False)
    assert "checkout_repo" not in out


def test_prompt_hint_absent_without_sha() -> None:
    out = build_engine_prompt(_mr_event(None), repo_checkout_enabled=True)
    assert "checkout_repo" not in out

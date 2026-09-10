# SPDX-License-Identifier: Apache-2.0
"""ach-memory facade: the containment boundary between the agent and the service.

The two invariants worth their own tests, because they are the ones that regress silently:
no exposed signature carries `scope` or `project_slug`, and the harness OVERRIDES both
rather than filling them in when absent.
"""

from __future__ import annotations

import json
import socket

import pytest

from ach_agent.memory.ach_memory_facade import AchMemoryFacade

EXPECTED_TOOLS = [
    "memory_get_mental_model",
    "memory_list_mental_models",
    "memory_recall",
    "memory_reflect",
    "memory_retain",
]


def _facade() -> AchMemoryFacade:
    return AchMemoryFacade("http://ach-memory:8000", "sekret", "ach-gitlab-pr")


@pytest.mark.asyncio
async def test_exposes_exactly_the_five_agent_facing_tools() -> None:
    """Everything governance-shaped (create/delete model, forget, correct, restore,
    documents, operations, working state) stays off the agent's surface."""
    names = sorted(t.name for t in await _facade()._mcp.list_tools())
    assert names == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_no_exposed_tool_accepts_scope_or_project_slug() -> None:
    """The agent must not be able to choose, or be argued into choosing, another bank."""
    for tool in await _facade()._mcp.list_tools():
        schema = json.dumps(tool.inputSchema)
        assert "project_slug" not in schema, tool.name
        assert "scope" not in schema, tool.name


@pytest.mark.asyncio
async def test_invoke_injects_scope_and_project(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_call(endpoint, secret, tool, args):
        seen.update({"endpoint": endpoint, "secret": secret, "tool": tool, "args": args})
        return "RESULT"

    monkeypatch.setattr("ach_agent.memory.ach_memory_facade.call_ach_memory", fake_call)
    result = await _facade()._invoke("recall", {"query": "q"})

    assert result == "RESULT"
    assert seen["args"] == {"query": "q", "scope": "project", "project_slug": "ach-gitlab-pr"}
    assert seen["secret"] == "sekret"


@pytest.mark.asyncio
async def test_injection_overrides_rather_than_fills(monkeypatch) -> None:
    """A fill-when-absent bridge is a convenience for a trusted user; this is a containment
    boundary. An agent that names another project must not win."""
    seen: dict[str, object] = {}

    async def fake_call(endpoint, secret, tool, args):
        seen.update(args)
        return "RESULT"

    monkeypatch.setattr("ach_agent.memory.ach_memory_facade.call_ach_memory", fake_call)
    await _facade()._invoke(
        "recall", {"query": "q", "project_slug": "someone/else", "scope": "user"}
    )

    assert seen["project_slug"] == "ach-gitlab-pr"
    assert seen["scope"] == "project"


@pytest.mark.asyncio
async def test_omitted_optionals_are_dropped_not_sent_as_null(monkeypatch) -> None:
    """`tags` is optional on the service side; sending an explicit null is not the same as
    omitting it, and would break against a build that has not shipped the parameter."""
    seen: dict[str, object] = {}

    async def fake_call(endpoint, secret, tool, args):
        seen.update(args)
        return "RESULT"

    monkeypatch.setattr("ach_agent.memory.ach_memory_facade.call_ach_memory", fake_call)
    await _facade()._invoke("recall", {"query": "q", "tags": None})

    assert "tags" not in seen


@pytest.mark.asyncio
async def test_a_failing_call_is_fail_soft(monkeypatch) -> None:
    """An exception crossing into opencode is a bug — degrade to a note instead."""

    async def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr("ach_agent.memory.ach_memory_facade.call_ach_memory", boom)
    assert await _facade()._invoke("recall", {"query": "q"}) == "Memory temporarily unavailable."


@pytest.mark.asyncio
async def test_retain_schema_carries_the_typed_contract() -> None:
    """ach-memory rejects a retain missing any of these, so they must be REQUIRED here."""
    tools = {t.name: t for t in await _facade()._mcp.list_tools()}
    schema = tools["memory_retain"].inputSchema

    assert set(schema["required"]) == {"content", "memory_type", "basis", "trigger", "evidence"}
    assert "tags" not in schema["required"]
    assert "gotcha" in json.dumps(schema)  # the memory_type enum reached the agent
    assert "artifact_excerpt" in json.dumps(schema)  # the evidence kind enum too


@pytest.mark.asyncio
async def test_start_returns_a_loopback_url_and_stop_tears_down() -> None:
    f = _facade()
    url = await f.start()
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
    port = int(url.split(":")[2].split("/")[0])
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass  # connect succeeds → listening
    await f.stop()


@pytest.mark.asyncio
async def test_recall_sends_tags_as_the_service_names_them(monkeypatch) -> None:
    """ach-memory renamed recall's tag parameter to `tags_filter` (retain kept `tags`).
    The agent sees one name; the split lives here, so a rename upstream cannot silently
    turn a narrowed recall into an unfiltered one."""
    seen: dict[str, object] = {}

    async def fake_call(endpoint, secret, tool, args):
        seen.update({"tool": tool, **args})
        return "RESULT"

    monkeypatch.setattr("ach_agent.memory.ach_memory_facade.call_ach_memory", fake_call)
    await _facade()._mcp.call_tool("memory_recall", {"query": "q", "tags": ["repo:a/b"]})

    assert seen["tags_filter"] == ["repo:a/b"]
    assert "tags" not in seen

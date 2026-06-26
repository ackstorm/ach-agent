# SPDX-License-Identifier: Apache-2.0
"""Tests for engine/a2a_egress.py — peer agents as harness-hosted MCP tools.

The A2AAgentClient is mocked via the client_factory injection point, so these
tests exercise the ToolSpec handler closures + the notification store WITHOUT a
live a2a-sdk wire connection (asyncio_mode=auto → bare async tests).
"""
from __future__ import annotations

from typing import Any

from ach_agent.engine.a2a_egress import (
    A2ANotificationStore,
    ToolSpec,
    build_a2a_tools,
)
from ach_agent.engine.hydrate import A2AAgent


# ---------------------------------------------------------------------------
# Fake A2AAgentClient — records calls, configurable per-method behaviour
# ---------------------------------------------------------------------------


class FakeClient:
    """Stands in for A2AAgentClient. Async methods mirror the real surface."""

    def __init__(
        self,
        *,
        send_result: str = "peer reply",
        async_task_id: str = "task-123",
        status_result: dict[str, Any] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.send_result = send_result
        self.async_task_id = async_task_id
        self.status_result = status_result or {
            "task_id": "task-123",
            "status": "completed",
            "result": "polled reply",
            "error": None,
        }
        self.raises = raises
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def send_task(self, prompt: str, context_id: str | None = None) -> str:
        self.calls.append(("send_task", (prompt, context_id)))
        if self.raises:
            raise self.raises
        return self.send_result

    async def send_task_async(self, prompt: str, context_id: str | None = None) -> str:
        self.calls.append(("send_task_async", (prompt, context_id)))
        if self.raises:
            raise self.raises
        return self.async_task_id

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("get_task_status", (task_id,)))
        if self.raises:
            raise self.raises
        return self.status_result

    async def wait_task(
        self, task_id: str, timeout: float = 300.0
    ) -> dict[str, Any]:
        self.calls.append(("wait_task", (task_id, timeout)))
        if self.raises:
            raise self.raises
        return self.status_result


def _agent(agent_id: str) -> A2AAgent:
    return A2AAgent(id=agent_id, endpoint=f"https://peer/{agent_id}")


def _factory(client: FakeClient):
    def make(_agent: A2AAgent) -> Any:
        return client

    return make


def _by_name(tools: list[ToolSpec], name: str) -> ToolSpec:
    return next(t for t in tools if t.name == name)


# ---------------------------------------------------------------------------
# build_a2a_tools — shape
# ---------------------------------------------------------------------------


def test_two_agents_yield_six_toolspecs() -> None:
    tools = build_a2a_tools([_agent("alpha"), _agent("beta")])
    names = {t.name for t in tools}
    assert len(tools) == 6
    assert names == {
        "a2a_alpha",
        "a2a_alpha_async",
        "a2a_alpha_status",
        "a2a_beta",
        "a2a_beta_async",
        "a2a_beta_status",
    }


def test_no_agents_yields_empty() -> None:
    assert build_a2a_tools([]) == []


# ---------------------------------------------------------------------------
# blocking tool — a2a_{name}
# ---------------------------------------------------------------------------


async def test_blocking_tool_returns_result() -> None:
    client = FakeClient(send_result="hello from peer")
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev").handler(prompt="hi")
    assert out == {"ok": True, "result": "hello from peer"}
    assert client.calls == [("send_task", ("hi", None))]


async def test_blocking_tool_passes_context_id() -> None:
    client = FakeClient()
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    await _by_name(tools, "a2a_rev").handler(prompt="hi", context_id="ctx-9")
    assert client.calls == [("send_task", ("hi", "ctx-9"))]


async def test_blocking_tool_error_is_caught() -> None:
    client = FakeClient(raises=RuntimeError("peer boom"))
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev").handler(prompt="hi")
    assert out["ok"] is False
    assert "peer boom" in out["error"]


# ---------------------------------------------------------------------------
# async tool — a2a_{name}_async
# ---------------------------------------------------------------------------


async def test_async_tool_returns_task_id_and_registers() -> None:
    client = FakeClient(async_task_id="task-async-1")
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev_async").handler(prompt="go")
    assert out == {"ok": True, "task_id": "task-async-1"}


async def test_async_tool_error_is_caught() -> None:
    client = FakeClient(raises=ValueError("no task"))
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev_async").handler(prompt="go")
    assert out["ok"] is False
    assert "no task" in out["error"]


# ---------------------------------------------------------------------------
# status tool — a2a_{name}_status
# ---------------------------------------------------------------------------


async def test_status_tool_no_wait_polls() -> None:
    client = FakeClient(
        status_result={
            "task_id": "t1",
            "status": "working",
            "result": None,
            "error": None,
        }
    )
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev_status").handler(task_id="t1", wait=False)
    assert out["ok"] is True
    assert out["status"] == "working"
    assert ("get_task_status", ("t1",)) in client.calls


async def test_status_tool_wait_resolves_via_notification_store() -> None:
    import asyncio

    # Shared store is internal; we drive completion by first registering the
    # task through the async tool, then notifying via the store the tools share.
    client = FakeClient(async_task_id="t-wait")
    store = A2ANotificationStore()
    tools = build_a2a_tools(
        [_agent("rev")], client_factory=_factory(client), _store=store
    )
    # register the task
    await _by_name(tools, "a2a_rev_async").handler(prompt="go")

    async def _notify() -> None:
        await asyncio.sleep(0.01)
        store.notify_completion(
            "t-wait", {"status": "completed", "result": "done!"}
        )

    notifier = asyncio.create_task(_notify())
    out = await _by_name(tools, "a2a_rev_status").handler(
        task_id="t-wait", wait=True, timeout=5.0
    )
    await notifier
    assert out["ok"] is True
    assert out["status"] == "completed"
    assert out["result"] == "done!"


async def test_status_tool_error_is_caught() -> None:
    client = FakeClient(raises=RuntimeError("status boom"))
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev_status").handler(task_id="t1", wait=False)
    assert out["ok"] is False
    assert "status boom" in out["error"]


# ---------------------------------------------------------------------------
# closure binding — no late-binding loop bug across agents
# ---------------------------------------------------------------------------


async def test_per_agent_closures_bind_correct_client() -> None:
    alpha = FakeClient(send_result="from-alpha")
    beta = FakeClient(send_result="from-beta")
    clients = {"alpha": alpha, "beta": beta}

    def factory(agent: A2AAgent) -> Any:
        return clients[agent.id]

    tools = build_a2a_tools(
        [_agent("alpha"), _agent("beta")], client_factory=factory
    )
    out_a = await _by_name(tools, "a2a_alpha").handler(prompt="x")
    out_b = await _by_name(tools, "a2a_beta").handler(prompt="x")
    assert out_a == {"ok": True, "result": "from-alpha"}
    assert out_b == {"ok": True, "result": "from-beta"}


# ---------------------------------------------------------------------------
# notification store unit behaviour
# ---------------------------------------------------------------------------


async def test_notification_store_wait_unknown_returns_none() -> None:
    store = A2ANotificationStore()
    assert await store.wait_for_task("nope", timeout=0.1) is None


async def test_notification_store_register_then_notify() -> None:
    import asyncio

    store = A2ANotificationStore()
    store.register_task("t1")

    async def _notify() -> None:
        await asyncio.sleep(0.01)
        assert store.notify_completion("t1", {"result": "ok"}) is True

    asyncio.create_task(_notify())
    got = await store.wait_for_task("t1", timeout=5.0)
    assert got == {"result": "ok"}

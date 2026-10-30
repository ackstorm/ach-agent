# SPDX-License-Identifier: MIT
"""Legacy peer A2A client and tool behavior, hosted behind the Harness facade."""

from __future__ import annotations

import contextlib
import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import a2a.client
import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.a2a import A2AAgentClient, ToolSpec, build_a2a_tools
from ach_runtime.facades.a2a import A2AEgressFacade
from ach_runtime.harness.hydrate import A2AAgentEntry
from ach_runtime.harness.keys import KeyAuth, KeySource
from ach_runtime.harness.tokens import facade_token

IDENTITY = ProcessIdentity(agent="classifier", environment="platform")
KEY = b"fixture-key"
FACADE_BASE = "http://localhost:8081"


class FakeClient:
    def __init__(self, *, result: str = "peer reply", task_id: str = "task-123",
                 status: dict[str, Any] | None = None, raises: Exception | None = None) -> None:
        self.result = result
        self.task_id = task_id
        self.status = status or {"task_id": "task-123", "status": "completed",
                                 "result": "polled reply", "error": None}
        self.raises = raises
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = 0

    async def send_task(self, prompt: str, context_id: str | None = None) -> str:
        self.calls.append(("send_task", (prompt, context_id)))
        if self.raises:
            raise self.raises
        return self.result

    async def send_task_async(self, prompt: str, context_id: str | None = None) -> str:
        self.calls.append(("send_task_async", (prompt, context_id)))
        if self.raises:
            raise self.raises
        return self.task_id

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        self.calls.append(("get_task_status", (task_id,)))
        if self.raises:
            raise self.raises
        return self.status

    async def wait_task(self, task_id: str, timeout: float = 300.0,
                        poll_interval: float = 2.0) -> dict[str, Any]:
        self.calls.append(("wait_task", (task_id, timeout)))
        if self.raises:
            raise self.raises
        return self.status

    async def close(self) -> None:
        self.closed += 1


def _agent(agent_id: str) -> A2AAgentEntry:
    return A2AAgentEntry(id=agent_id, endpoint=f"https://peer/{agent_id}")


def _factory(client: FakeClient):
    return lambda _agent: client


def _by_name(tools: list[ToolSpec], name: str) -> ToolSpec:
    return next(tool for tool in tools if tool.name == name)


async def _owned(claim: str) -> bool:
    return claim == "owned-ws"


@contextlib.asynccontextmanager
async def _client_session(
    app: Any, token: str, *, session_ids: list[str] | None = None,
    base_url: str = FACADE_BASE,
) -> AsyncIterator[Any]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as http_client:
        async with streamable_http_client(
            f"{base_url}/t/{token}/a2a/", http_client=http_client
        ) as (read, write, get_session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                if session_ids is not None:
                    session_id = get_session_id()
                    assert session_id
                    session_ids.append(session_id)
                yield session


def _delegation_methods(app: Any) -> list[str]:
    guarded = app.routes[0].app
    sdk = guarded._app
    methods: list[str] = []

    async def observe(scope: Any, receive: Any, send: Any) -> None:
        methods.append(scope.get("method", ""))
        await sdk(scope, receive, send)

    guarded._app = observe
    return methods


def test_two_agents_yield_six_toolspecs() -> None:
    tools = build_a2a_tools([_agent("alpha"), _agent("beta")], identity=IDENTITY)
    assert {tool.name for tool in tools} == {
        "a2a_alpha", "a2a_alpha_async", "a2a_alpha_status",
        "a2a_beta", "a2a_beta_async", "a2a_beta_status",
    }


def test_no_agents_yields_empty() -> None:
    assert build_a2a_tools([], identity=IDENTITY) == []


async def test_blocking_tool_returns_result_and_passes_context_id() -> None:
    client = FakeClient(result="hello from peer")
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client), identity=IDENTITY)
    result = await _by_name(tools, "a2a_rev").handler(prompt="hi", context_id="ctx-9")
    assert result == {"ok": True, "result": "hello from peer"}
    assert client.calls == [("send_task", ("hi", "ctx-9"))]


async def test_blocking_tool_error_is_caught() -> None:
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(FakeClient(raises=RuntimeError("peer boom"))), identity=IDENTITY)
    assert await _by_name(tools, "a2a_rev").handler(prompt="hi") == {
        "ok": False, "error": "peer boom"
    }


async def test_async_tool_returns_task_id_and_catches_errors() -> None:
    client = FakeClient(task_id="task-async-1")
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client), identity=IDENTITY)
    assert await _by_name(tools, "a2a_rev_async").handler(prompt="go") == {
        "ok": True, "task_id": "task-async-1"
    }
    broken = build_a2a_tools([_agent("rev")], client_factory=_factory(FakeClient(raises=ValueError("no task"))), identity=IDENTITY)
    assert (await _by_name(broken, "a2a_rev_async").handler(prompt="go"))["ok"] is False


async def test_status_tool_poll_wait_timeout_and_error_shapes() -> None:
    status = {"task_id": "t", "status": "completed", "result": "done", "error": None}
    client = FakeClient(status=status)
    tool = _by_name(build_a2a_tools([_agent("rev")], client_factory=_factory(client), identity=IDENTITY), "a2a_rev_status")
    assert await tool.handler(task_id="t", wait=False) == {"ok": True, "status": "completed", "result": "done"}
    assert await tool.handler(task_id="t", wait=True, timeout=5.0) == {"ok": True, "status": "completed", "result": "done"}
    assert ("wait_task", ("t", 5.0)) in client.calls
    timeout = FakeClient(status={"task_id": "t", "status": "timeout", "result": None, "error": "timeout after 5s"})
    timeout_tool = _by_name(build_a2a_tools([_agent("rev")], client_factory=_factory(timeout), identity=IDENTITY), "a2a_rev_status")
    assert await timeout_tool.handler(task_id="t", wait=True, timeout=5.0) == {"ok": False, "error": "timeout after 5s"}
    broken = _by_name(build_a2a_tools([_agent("rev")], client_factory=_factory(FakeClient(raises=RuntimeError("status boom"))), identity=IDENTITY), "a2a_rev_status")
    assert await broken.handler(task_id="t") == {"ok": False, "error": "status boom"}


async def test_per_agent_closures_bind_correct_client() -> None:
    clients = {"alpha": FakeClient(result="from-alpha"), "beta": FakeClient(result="from-beta")}
    tools = build_a2a_tools([_agent("alpha"), _agent("beta")], client_factory=lambda agent: clients[agent.id], identity=IDENTITY)
    assert await _by_name(tools, "a2a_alpha").handler(prompt="x") == {"ok": True, "result": "from-alpha"}
    assert await _by_name(tools, "a2a_beta").handler(prompt="x") == {"ok": True, "result": "from-beta"}


async def test_real_a2a_client_receives_process_identity_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeAsyncClient:
        def __init__(self, *, headers: dict[str, str], timeout: httpx.Timeout, auth: object = None) -> None:
            captured["headers"] = dict(headers)
            captured["timeout"] = timeout
            captured["auth"] = auth

        async def aclose(self) -> None:
            captured["closed"] = True

    class FakeClientConfig:
        def __init__(self, *, httpx_client: object, streaming: bool) -> None:
            captured["httpx_client"] = httpx_client
            captured["streaming"] = streaming

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        captured["agent"] = agent
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(a2a.client, "ClientConfig", FakeClientConfig)
    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://peer.example/a2a", KeySource("ek-peer").auth, identity=IDENTITY)
    await client._ensure_client()
    await client.close()
    assert captured["headers"] == {"x-ach-agent": "classifier", "x-ach-environment": "platform"}
    assert isinstance(captured["auth"], KeyAuth)
    assert captured["agent"] == "https://peer.example/a2a"
    assert captured["streaming"] is False and captured["closed"] is True


async def test_concurrent_first_calls_share_one_discovery_and_close_client(monkeypatch: pytest.MonkeyPatch) -> None:
    allocated: list[Any] = []
    discoveries = 0
    discovery_started = asyncio.Event()
    allow_discovery = asyncio.Event()

    class FakeAsyncClient:
        def __init__(self, *, headers: dict[str, str], timeout: httpx.Timeout, auth: object = None) -> None:
            self.closed = False
            allocated.append(self)

        async def aclose(self) -> None:
            self.closed = True

    class FakeClientConfig:
        def __init__(self, *, httpx_client: object, streaming: bool) -> None:
            pass

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        nonlocal discoveries
        discoveries += 1
        discovery_started.set()
        await allow_discovery.wait()
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(a2a.client, "ClientConfig", FakeClientConfig)
    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://peer.example/a2a", identity=IDENTITY)

    first = asyncio.create_task(client._ensure_client())
    await discovery_started.wait()
    second = asyncio.create_task(client._ensure_client())
    await asyncio.sleep(0)
    allow_discovery.set()
    resolved = await asyncio.gather(first, second)

    assert resolved[0] is resolved[1]
    assert discoveries == 1
    assert len(allocated) == 1
    await client.close()
    assert [http_client.closed for http_client in allocated] == [True]


async def test_failed_discovery_closes_allocation_before_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    allocated: list[Any] = []
    discoveries = 0

    class FakeAsyncClient:
        def __init__(self, *, headers: dict[str, str], timeout: httpx.Timeout, auth: object = None) -> None:
            self.closed = False
            allocated.append(self)

        async def aclose(self) -> None:
            self.closed = True

    class FakeClientConfig:
        def __init__(self, *, httpx_client: object, streaming: bool) -> None:
            pass

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        nonlocal discoveries
        discoveries += 1
        if discoveries == 1:
            raise RuntimeError("discovery failed")
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(a2a.client, "ClientConfig", FakeClientConfig)
    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://peer.example/a2a", identity=IDENTITY)

    with pytest.raises(RuntimeError, match="discovery failed"):
        await client._ensure_client()
    assert [http_client.closed for http_client in allocated] == [True]
    recovered = await client._ensure_client()
    assert recovered is not None
    await client.close()
    assert discoveries == 2
    assert [http_client.closed for http_client in allocated] == [True, True]


async def test_send_task_async_requests_immediate_return(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a.types.a2a_pb2 import StreamResponse

    captured: dict[str, Any] = {}

    class FakeSdkClient:
        async def send_message(self, request: Any):
            captured["return_immediately"] = request.configuration.return_immediately
            response = StreamResponse()
            response.task.id = "peer-task-1"
            yield response

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        return FakeSdkClient()

    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://peer.example/a2a", identity=IDENTITY)
    assert await client.send_task_async("go") == "peer-task-1"
    assert captured["return_immediately"] is True


async def test_real_a2a_client_sends_protobuf_text_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a.types.a2a_pb2 import StreamResponse

    captured: dict[str, Any] = {}

    class FakeSdkClient:
        async def send_message(self, request: Any):
            captured["prompt"] = request.message.parts[0].text
            captured["context_id"] = request.message.context_id
            response = StreamResponse()
            response.message.parts.add().text = "peer response"
            yield response

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        return FakeSdkClient()

    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://peer.example/a2a", identity=IDENTITY)
    assert await client.send_task("review", context_id="ctx-1") == "peer response"
    assert captured == {"prompt": "review", "context_id": "ctx-1"}


async def test_real_a2a_client_status_shape_and_wait_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    from a2a.types.a2a_pb2 import Task, TaskState

    class FakeSdkClient:
        async def get_task(self, request: Any) -> Task:
            task = Task(id=request.id)
            task.status.state = TaskState.Value("TASK_STATE_COMPLETED")
            task.artifacts.add().parts.add().text = "finished"
            return task

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        return FakeSdkClient()

    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://peer.example/a2a", identity=IDENTITY)
    assert await client.get_task_status("task-1") == {
        "task_id": "task-1", "status": "completed", "result": "finished", "error": None
    }
    states = iter([
        {"task_id": "task-2", "status": "working", "result": None, "error": None},
        {"task_id": "task-2", "status": "completed", "result": "done", "error": None},
    ])

    async def next_status(task_id: str) -> dict[str, Any]:
        return next(states)

    async def no_sleep(_: float) -> None:
        return None

    client.get_task_status = next_status  # type: ignore[method-assign]
    monkeypatch.setattr("ach_runtime.facades.a2a.asyncio.sleep", no_sleep)
    assert await client.wait_task("task-2", timeout=1, poll_interval=0) == {
        "task_id": "task-2", "status": "completed", "result": "done", "error": None
    }
    assert await client.wait_task("task-3", timeout=0) == {
        "task_id": "task-3", "status": "timeout", "result": None,
        "error": "timeout after 0s waiting for task",
    }


@pytest.mark.parametrize("token_kind", ["malformed", "wrong-key", "non-owned"])
async def test_a2a_guard_denies_invalid_tokens_before_mcp(token_kind: str) -> None:
    peer = FakeClient()
    facade = A2AEgressFacade(
        agents=(_agent("review"),), auth=None, identity=IDENTITY, key=KEY,
        owned_workspace=_owned, exclude_tools=frozenset(), client_factory=lambda _: peer,
    )
    app = facade.app()
    delegated = _delegation_methods(app)
    tokens = {
        "malformed": "garbage",
        "wrong-key": facade_token(b"different-key", "owned-ws"),
        "non-owned": facade_token(KEY, "someone-else"),
    }
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=FACADE_BASE) as client:
            response = await client.post(f"/t/{tokens[token_kind]}/a2a/", content=b"{}")
        assert response.status_code == 403
    assert delegated == []
    assert peer.calls == []
    assert peer.closed == 1


@pytest.mark.parametrize("base_url", [FACADE_BASE, "http://ach-control-agent.ach.svc:8081"])
async def test_a2a_ownership_flip_denies_post_get_delete_without_peer_calls(base_url: str) -> None:
    state = {"owned": True}

    async def owns(claim: str) -> bool:
        return state["owned"] and claim == "owned-ws"

    peer = FakeClient()
    facade = A2AEgressFacade(
        agents=(_agent("review"),), auth=None, identity=IDENTITY, key=KEY,
        owned_workspace=owns, exclude_tools=frozenset(), client_factory=lambda _: peer,
    )
    app = facade.app()
    delegated = _delegation_methods(app)
    token = facade_token(KEY, "owned-ws")
    async with app.router.lifespan_context(app):
        session_ids: list[str] = []
        async with _client_session(app, token, session_ids=session_ids, base_url=base_url) as session:
            result = await session.call_tool("a2a_review", {"prompt": "work"})
            assert result.isError is False
            peer_calls_before = list(peer.calls)
            delegated_before = list(delegated)
            state["owned"] = False
            headers = {"mcp-session-id": session_ids[0]}
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=base_url
            ) as client:
                post = await client.post(
                    f"/t/{token}/a2a/", headers=headers,
                    content=json.dumps({
                        "jsonrpc": "2.0", "method": "tools/call", "id": 8,
                        "params": {"name": "a2a_review", "arguments": {"prompt": "denied"}},
                    }),
                )
                get = await client.get(f"/t/{token}/a2a/", headers=headers)
                delete = await client.delete(f"/t/{token}/a2a/", headers=headers)
            assert [post.status_code, get.status_code, delete.status_code] == [403, 403, 403]
            assert peer.calls == peer_calls_before
            assert delegated == delegated_before
    assert peer.closed == 1


async def test_peer_close_preserves_cancellation_and_other_cleanup_failure() -> None:
    """M1: a `CancelledError` from one peer close is a `BaseException`, not an `Exception`
    -- raising it through a plain `ExceptionGroup` is a constructor `TypeError` that masks
    both original causes. Every owned client must still be attempted exactly once, and the
    raised group must retain both exact original exceptions."""

    class RaisingClient:
        def __init__(self, exc: BaseException) -> None:
            self._exc = exc
            self.close_calls = 0

        async def close(self) -> None:
            self.close_calls += 1
            raise self._exc

    cancelled = asyncio.CancelledError("peer-a cancelled")
    broken = RuntimeError("peer-b cleanup boom")
    client_a = RaisingClient(cancelled)
    client_b = RaisingClient(broken)
    clients = {"peer-a": client_a, "peer-b": client_b}
    facade = A2AEgressFacade(
        agents=(_agent("peer-a"), _agent("peer-b")), auth=None, identity=IDENTITY, key=KEY,
        owned_workspace=_owned, exclude_tools=frozenset(),
        client_factory=lambda agent: clients[agent.id],
    )

    with pytest.raises(BaseExceptionGroup) as excinfo:
        await facade.aclose()

    assert client_a.close_calls == 1 and client_b.close_calls == 1
    assert set(excinfo.value.exceptions) == {cancelled, broken}

    # Repeat close remains idempotent: no further attempts, no re-raise.
    await facade.aclose()
    assert client_a.close_calls == 1 and client_b.close_calls == 1

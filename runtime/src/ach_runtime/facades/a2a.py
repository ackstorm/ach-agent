# SPDX-License-Identifier: MIT
"""Hydrated peer agents exposed as guarded MCP tools inside the Harness."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast
from uuid import uuid4

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

from ach_runtime.facades._proxy import ProcessIdentity, with_identity_headers
from ach_runtime.facades.memory import _AuthGuard
from ach_runtime.harness.hydrate import A2AAgentEntry
from ach_runtime.observability.runs import redact

log = logging.getLogger(__name__)
OwnedWorkspace = Callable[[str], Awaitable[bool]]


class A2AAgentClient:
    """Protobuf A2A client, with peer credentials and process identity kept in Harness."""

    _TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "cancelled", "rejected"})

    def __init__(
        self,
        url: str,
        auth: httpx.Auth | None = None,
        timeout: float = 120.0,
        *,
        identity: ProcessIdentity,
    ) -> None:
        self._url = url
        self._auth = auth
        self._timeout = timeout
        self._identity = identity
        self._client: Any = None
        self._httpx_client: Any = None
        self._client_lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    from a2a.client import ClientConfig, create_client

                    # The credential comes from `auth` per request, so a rotation reaches
                    # this long-lived client.
                    headers = with_identity_headers({}, self._identity)
                    httpx_client = httpx.AsyncClient(
                        headers=headers,
                        auth=self._auth,
                        timeout=httpx.Timeout(timeout=300.0),
                    )
                    self._httpx_client = httpx_client
                    config = ClientConfig(httpx_client=httpx_client, streaming=False)
                    create = cast(Callable[..., Awaitable[Any]], create_client)
                    try:
                        self._client = await create(agent=self._url, client_config=config)
                    except BaseException:
                        if self._httpx_client is httpx_client:
                            self._httpx_client = None
                        try:
                            await httpx_client.aclose()
                        except BaseException:
                            log.warning("a2a client discovery cleanup failed")
                        raise
        return self._client

    def _build_message(self, prompt: str, context_id: str | None) -> Any:
        from a2a.types.a2a_pb2 import ROLE_USER, Message

        message = Message(message_id=uuid4().hex, role=ROLE_USER)
        if context_id:
            message.context_id = context_id
        message.parts.add().text = prompt
        return message

    @staticmethod
    def _extract_text(obj: Any) -> str:
        parts: list[str] = []
        for artifact in getattr(obj, "artifacts", None) or []:
            for part in getattr(artifact, "parts", []):
                if getattr(part, "text", ""):
                    parts.append(part.text)
        for part in getattr(obj, "parts", []):
            if getattr(part, "text", ""):
                parts.append(part.text)
        status = getattr(obj, "status", None)
        status_message = getattr(status, "message", None) if status is not None else None
        if status_message is not None:
            for part in getattr(status_message, "parts", []):
                if getattr(part, "text", ""):
                    parts.append(part.text)
        return "\n".join(parts)

    @staticmethod
    def _state_name(task: Any) -> str:
        from a2a.types.a2a_pb2 import TaskState

        return TaskState.Name(task.status.state).removeprefix("TASK_STATE_").lower()

    async def send_task(self, prompt: str, context_id: str | None = None) -> str:
        from a2a.types.a2a_pb2 import SendMessageRequest

        client = await self._ensure_client()
        request = SendMessageRequest(message=self._build_message(prompt, context_id))
        async with asyncio.timeout(self._timeout):
            collected: list[str] = []
            async for response in client.send_message(request):
                for field in ("task", "message", "status_update", "artifact_update"):
                    payload = getattr(response, field, None)
                    if payload is not None and response.HasField(field):
                        text = self._extract_text(payload)
                        if text:
                            collected.append(text)
            return "\n".join(collected)

    async def send_task_async(self, prompt: str, context_id: str | None = None) -> str:
        from a2a.types.a2a_pb2 import SendMessageRequest

        client = await self._ensure_client()
        request = SendMessageRequest(message=self._build_message(prompt, context_id))
        request.configuration.return_immediately = True
        async for response in client.send_message(request):
            if response.HasField("task") and response.task.id:
                return cast(str, response.task.id)
        raise RuntimeError("no task id returned from A2A peer")

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        from a2a.types.a2a_pb2 import GetTaskRequest

        client = await self._ensure_client()
        task = await client.get_task(GetTaskRequest(id=task_id))
        if task is None:
            return {"task_id": task_id, "status": "not_found", "result": None, "error": "task not found"}
        state = self._state_name(task)
        result = self._extract_text(task) if state == "completed" else None
        error = self._extract_text(task) if state == "failed" else None
        return {"task_id": task_id, "status": state, "result": result, "error": error}

    async def wait_task(
        self, task_id: str, timeout: float = 300.0, poll_interval: float = 2.0
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            status = await self.get_task_status(task_id)
            if status["status"] in self._TERMINAL_STATES:
                return status
            await asyncio.sleep(poll_interval)
        return {
            "task_id": task_id,
            "status": "timeout",
            "result": None,
            "error": f"timeout after {timeout}s waiting for task",
        }

    async def close(self) -> None:
        async with self._client_lock:
            if self._httpx_client is not None:
                client, self._httpx_client = self._httpx_client, None
                self._client = None
                await client.aclose()


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Awaitable[dict[str, Any]]]


def build_a2a_tools(
    agents: list[A2AAgentEntry],
    auth: httpx.Auth | None = None,
    client_factory: Callable[[A2AAgentEntry], A2AAgentClient] | None = None,
    *,
    identity: ProcessIdentity,
) -> list[ToolSpec]:
    if client_factory is None:
        client_factory = lambda agent: A2AAgentClient(
            url=agent.endpoint, auth=auth, timeout=120.0, identity=identity
        )
    tools: list[ToolSpec] = []
    for agent in agents:
        tools.extend(_make_agent_tools(agent.id, client_factory(agent)))
    return tools


def _make_agent_tools(name: str, client: A2AAgentClient) -> list[ToolSpec]:
    async def blocking(prompt: str, context_id: str | None = None) -> dict[str, Any]:
        try:
            return {"ok": True, "result": await client.send_task(prompt, context_id=context_id)}
        except Exception as exc:
            log.warning("a2a egress blocking call failed agent=%s error=%s", name, exc)
            return {"ok": False, "error": redact(str(exc))}

    async def fire(prompt: str, context_id: str | None = None) -> dict[str, Any]:
        try:
            return {"ok": True, "task_id": await client.send_task_async(prompt, context_id=context_id)}
        except Exception as exc:
            log.warning("a2a egress async call failed agent=%s error=%s", name, exc)
            return {"ok": False, "error": redact(str(exc))}

    async def status(task_id: str, wait: bool = False, timeout: float = 300.0) -> dict[str, Any]:
        try:
            resolved = (
                await client.wait_task(task_id, timeout=timeout)
                if wait else await client.get_task_status(task_id)
            )
            if wait and resolved.get("status") == "timeout":
                return {"ok": False, "error": resolved.get("error")}
            return {"ok": True, "status": resolved.get("status"), "result": resolved.get("result")}
        except Exception as exc:
            log.warning("a2a egress status call failed agent=%s error=%s", name, exc)
            return {"ok": False, "error": redact(str(exc))}

    return [
        ToolSpec(name=f"a2a_{name}",
                 description=f"Send a prompt to peer agent '{name}' and wait for its reply.",
                 handler=blocking),
        ToolSpec(name=f"a2a_{name}_async",
                 description=(f"Send a prompt to peer agent '{name}' without waiting; "
                              "returns a task_id to poll later."),
                 handler=fire),
        ToolSpec(name=f"a2a_{name}_status",
                 description=(f"Check status/result of a task on peer agent '{name}'. "
                              "Set wait=true to block until completion."),
                 handler=status),
    ]


def build_a2a_mcp_server(tools: list[ToolSpec], *, streamable_http_path: str = "/") -> FastMCP:
    # Mounted in the Harness's cluster-facing listener, not a loopback server.
    server: FastMCP = FastMCP("a2a-egress", host="0.0.0.0", streamable_http_path=streamable_http_path)
    for spec in tools:
        server.add_tool(spec.handler, name=spec.name, description=spec.description)
    return server


class A2AEgressFacade:
    """Owns one private A2A client per granted peer and a guarded MCP app."""

    def __init__(
        self,
        *,
        agents: tuple[A2AAgentEntry, ...],
        auth: httpx.Auth | None,
        identity: ProcessIdentity,
        key: bytes,
        owned_workspace: OwnedWorkspace,
        exclude_tools: frozenset[str],
        client_factory: Callable[[A2AAgentEntry], A2AAgentClient] | None = None,
    ) -> None:
        self._key = key
        self._owned_workspace = owned_workspace
        make_client = client_factory or (lambda agent: self._default_client(agent, auth, identity))
        self._clients: list[A2AAgentClient] = []

        def owned_client_factory(agent: A2AAgentEntry) -> A2AAgentClient:
            client = make_client(agent)
            self._clients.append(client)
            return client

        tools = build_a2a_tools(
            list(agents), auth=auth, client_factory=owned_client_factory, identity=identity
        )
        self._tools = [tool for tool in tools if tool.name not in exclude_tools]
        self._mcp = build_a2a_mcp_server(self._tools, streamable_http_path="/")
        self._closed = False

    @staticmethod
    def _default_client(
        agent: A2AAgentEntry, auth: httpx.Auth | None, identity: ProcessIdentity
    ) -> A2AAgentClient:
        return A2AAgentClient(agent.endpoint, auth=auth, timeout=120.0, identity=identity)

    def app(self) -> Starlette:
        mcp_app = self._mcp.streamable_http_app()
        guarded = _AuthGuard(mcp_app, key=self._key, owned_workspace=self._owned_workspace)

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            try:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            finally:
                await self.aclose()

        return Starlette(routes=[Mount("/t/{token}/a2a", app=guarded)], lifespan=lifespan)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for client in self._clients:
            try:
                await client.close()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            # BaseExceptionGroup, not ExceptionGroup: a peer close can raise CancelledError
            # (a BaseException, not an Exception) -- ExceptionGroup's constructor rejects
            # nesting one and would mask every original cause behind a TypeError instead.
            # BaseExceptionGroup narrows to ExceptionGroup automatically when every leaf is
            # an ordinary Exception, so this never weakens the all-Exception case.
            raise BaseExceptionGroup("one or more A2A peer clients failed to close", errors)

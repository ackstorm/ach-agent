# SPDX-License-Identifier: Apache-2.0
"""A2A egress — peer agents exposed as harness-hosted MCP tools.

Ported from ackbot-process (`src/handlers/a2a/{client,notification_store}.py`),
adapted to:
  - the installed protobuf-based a2a-sdk (`a2a.types.a2a_pb2`, `create_client`)
    instead of ackbot's pydantic a2a.types + `ClientFactory.connect`;
  - structlog instead of ackbot's `Logger`;
  - the neutral `ToolSpec` descriptor (replaces ackbot's `ToolDef`).

Each peer agent (`A2AAgent{id, endpoint}`) yields three MCP tools so opencode can
call peers: `a2a_{id}` (blocking), `a2a_{id}_async` (fire + task_id),
`a2a_{id}_status` (poll or wait-for-completion). The ACH `ek_` (when present) is
held in the harness and injected as the peer Authorization header — it NEVER
reaches opencode.

RTR-06: a2a-sdk imports are function-scoped (never module-level), mirroring
        `channels/a2a.py`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

import structlog

if TYPE_CHECKING:
    from ach_agent.engine.hydrate import A2AAgent

log = structlog.get_logger(__name__)

# RTR-06: a2a imports are ONLY inside functions/methods below — never module level.
# Verified by: grep -nE "^import a2a|^from a2a" src/ach_agent/engine/a2a_egress.py
#   → zero results.


# ---------------------------------------------------------------------------
# A2ANotificationStore — in-memory task → completion map (ported verbatim)
# ---------------------------------------------------------------------------


class A2ANotificationStore:
    """Tracks pending async A2A tasks and resolves them via push notifications."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        self._futures: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._registered_at: dict[str, float] = {}
        self._log = log.bind(component="A2ANotificationStore")

    def register_task(self, task_id: str) -> asyncio.Future[dict[str, Any]]:
        """Register a pending task. Returns a Future resolved on notification."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._futures[task_id] = fut
        self._registered_at[task_id] = time.monotonic()
        self._log.info("registered task for push notification", task_id=task_id)
        return fut

    def notify_completion(self, task_id: str, result: dict[str, Any]) -> bool:
        """Notify that a task completed. Returns True if task was registered."""
        fut = self._futures.pop(task_id, None)
        self._registered_at.pop(task_id, None)
        if fut is None:
            self._log.debug("notification for unknown task", task_id=task_id)
            return False
        if not fut.done():
            fut.set_result(result)
            self._log.info("resolved task via push notification", task_id=task_id)
        return True

    async def wait_for_task(self, task_id: str, timeout: float = 300.0) -> dict[str, Any] | None:
        """Wait for a push notification for task_id. None on timeout/unknown."""
        fut = self._futures.get(task_id)
        if fut is None:
            self._log.warning("wait_for_task: task not registered", task_id=task_id)
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
        except TimeoutError:
            self._log.info("timeout waiting for push notification", task_id=task_id)
            return None

    def cleanup_expired(self) -> int:
        """Remove expired tasks. Returns count removed."""
        now = time.monotonic()
        expired = [tid for tid, ts in self._registered_at.items() if now - ts >= self._ttl]
        for tid in expired:
            fut = self._futures.pop(tid, None)
            self._registered_at.pop(tid, None)
            if fut and not fut.done():
                fut.cancel()
        if expired:
            self._log.info("cleaned up expired registrations", count=len(expired))
        return len(expired)


# ---------------------------------------------------------------------------
# A2AAgentClient — a2a-sdk wrapper (ported; adapted to protobuf-based sdk)
# ---------------------------------------------------------------------------


class A2AAgentClient:
    """Client for calling an external A2A peer agent.

    Adapted from ackbot's `A2AAgentClient`. The installed a2a-sdk is the
    protobuf transport (`a2a.types.a2a_pb2` + `a2a.client.create_client`), so the
    construction + request shapes differ from ackbot's pydantic version, but the
    four-method contract (`send_task`, `send_task_async`, `get_task_status`,
    `wait_task`) is preserved. Dropped ackbot's webhook/push-notification HTTP
    receiver complexity (polling-based status only).
    """

    # State-name strings considered terminal (protobuf TaskState enum names lower-cased).
    _TERMINAL_STATES = frozenset({"completed", "failed", "canceled", "cancelled", "rejected"})

    def __init__(self, url: str, api_key: str | None = None, timeout: float = 120.0) -> None:
        self._url = url
        self._api_key = api_key
        self._timeout = timeout
        self._client: Any = None
        self._httpx_client: Any = None
        self._log = log.bind(component="A2AAgentClient", peer=url)

    async def _ensure_client(self) -> Any:
        """Lazy-init: discover agent card and create the a2a client."""
        if self._client is None:
            import httpx
            from a2a.client import ClientConfig, create_client

            headers: dict[str, str] = {}
            if self._api_key:
                # The api_key is the ACH ek_; injected as the ACH `x-ach-key` header
                # (ACH's auth scheme — Authorization: Bearer 401s) so the SECRET STAYS
                # IN THE HARNESS (opencode never sees it). Untested vs a live a2a peer.
                headers["x-ach-key"] = self._api_key
            self._httpx_client = httpx.AsyncClient(
                headers=headers,
                timeout=httpx.Timeout(timeout=300.0),  # slow peer startup
            )
            config = ClientConfig(httpx_client=self._httpx_client, streaming=False)
            _create = cast(Callable[..., Awaitable[Any]], create_client)
            self._client = await _create(agent=self._url, client_config=config)
        return self._client

    def _build_message(self, prompt: str, context_id: str | None) -> Any:
        """Build a protobuf user Message carrying a single text part."""
        from a2a.types.a2a_pb2 import ROLE_USER, Message

        msg = Message(message_id=uuid4().hex, role=ROLE_USER)
        if context_id:
            msg.context_id = context_id
        part = msg.parts.add()
        part.text = prompt
        return msg

    @staticmethod
    def _extract_text(obj: Any) -> str:
        """Collect text from a protobuf Task / Message: artifacts then parts."""
        parts: list[str] = []
        artifacts = getattr(obj, "artifacts", None)
        if artifacts:
            for artifact in artifacts:
                for part in getattr(artifact, "parts", []):
                    text = getattr(part, "text", "")
                    if text:
                        parts.append(text)
        for part in getattr(obj, "parts", []):
            text = getattr(part, "text", "")
            if text:
                parts.append(text)
        # Task carries its terminal message under status.message.
        status = getattr(obj, "status", None)
        status_msg = getattr(status, "message", None) if status is not None else None
        if status_msg is not None:
            for part in getattr(status_msg, "parts", []):
                text = getattr(part, "text", "")
                if text:
                    parts.append(text)
        return "\n".join(parts)

    @staticmethod
    def _state_name(task: Any) -> str:
        """Lower-cased TaskState name for a protobuf Task (e.g. 'completed')."""
        from a2a.types.a2a_pb2 import TaskState

        state = task.status.state
        name: str = TaskState.Name(state)  # e.g. "TASK_STATE_COMPLETED"
        return name.removeprefix("TASK_STATE_").lower()

    async def send_task(self, prompt: str, context_id: str | None = None) -> str:
        """Send a task and wait for completion. Returns aggregated result text."""
        from a2a.types.a2a_pb2 import SendMessageRequest

        client = await self._ensure_client()
        request = SendMessageRequest(message=self._build_message(prompt, context_id))
        async with asyncio.timeout(self._timeout):
            collected: list[str] = []
            async for response in client.send_message(request):
                # response is a StreamResponse oneof: task | message | *_update.
                for field in ("task", "message", "status_update", "artifact_update"):
                    payload = getattr(response, field, None)
                    if payload is not None and response.HasField(field):
                        # status_update wraps status.message; reuse _extract_text.
                        text = self._extract_text(payload)
                        if text:
                            collected.append(text)
            return "\n".join(collected)

    async def send_task_async(self, prompt: str, context_id: str | None = None) -> str:
        """Send a task without waiting. Returns the task_id immediately."""
        from a2a.types.a2a_pb2 import SendMessageRequest

        client = await self._ensure_client()
        request = SendMessageRequest(message=self._build_message(prompt, context_id))
        async for response in client.send_message(request):
            if response.HasField("task") and response.task.id:
                return cast(str, response.task.id)
        raise RuntimeError("no task id returned from A2A peer")

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        """Get current status and (if completed) result text of a task."""
        from a2a.types.a2a_pb2 import GetTaskRequest

        client = await self._ensure_client()
        task = await client.get_task(GetTaskRequest(id=task_id))
        if task is None:
            return {
                "task_id": task_id,
                "status": "not_found",
                "result": None,
                "error": "task not found",
            }
        state = self._state_name(task)
        result = self._extract_text(task) if state == "completed" else None
        error = self._extract_text(task) if state == "failed" else None
        return {
            "task_id": task_id,
            "status": state,
            "result": result,
            "error": error,
        }

    async def wait_task(
        self, task_id: str, timeout: float = 300.0, poll_interval: float = 2.0
    ) -> dict[str, Any]:
        """Poll task until a terminal state or timeout."""
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
        """Close the underlying httpx client."""
        if self._httpx_client is not None:
            await self._httpx_client.aclose()


# ---------------------------------------------------------------------------
# ToolSpec — neutral tool descriptor (replaces ackbot's ToolDef)
# ---------------------------------------------------------------------------


@dataclass
class ToolSpec:
    name: str
    description: str
    handler: Callable[..., Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# build_a2a_tools — three ToolSpecs per peer agent
# ---------------------------------------------------------------------------


def build_a2a_tools(
    agents: list[A2AAgent],
    ek: str | None = None,
    client_factory: Callable[[A2AAgent], A2AAgentClient] | None = None,
    _store: A2ANotificationStore | None = None,
) -> list[ToolSpec]:
    """Build MCP ToolSpecs that let opencode call peer A2A agents.

    For each agent (name = ``agent.id``) three tools are produced:
      - ``a2a_{name}``        blocking: send_task → {"ok", "result"}
      - ``a2a_{name}_async``  fire-and-forget: send_task_async → {"ok", "task_id"}
      - ``a2a_{name}_status`` poll/wait: get_task_status | store.wait_for_task

    Handlers NEVER raise — peer/timeout errors become {"ok": False, "error": ...}.

    Args:
        agents:         peer agents from the hydration manifest.
        ek:             ACH ek_ injected as the peer auth header (stays in harness).
        client_factory: test injection hook; default builds a real A2AAgentClient.
        _store:         shared notification store (one per call); injectable for tests.
    """
    if client_factory is None:

        def client_factory(agent: A2AAgent) -> A2AAgentClient:
            return A2AAgentClient(url=agent.endpoint, api_key=ek, timeout=120.0)

    store = _store if _store is not None else A2ANotificationStore()
    tools: list[ToolSpec] = []

    for agent in agents:
        name = agent.id
        client = client_factory(agent)
        tools.extend(_make_agent_tools(name, client, store))
    return tools


def _make_agent_tools(
    name: str, client: A2AAgentClient, store: A2ANotificationStore
) -> list[ToolSpec]:
    """Build the three ToolSpecs for one peer, binding name/client per closure.

    Mirrors ackbot's ``make_blocking(name)`` factory pattern — defining the
    closures inside this per-agent helper binds ``name``/``client`` correctly and
    avoids the classic late-binding loop bug.
    """

    async def blocking(prompt: str, context_id: str | None = None) -> dict[str, Any]:
        try:
            result = await client.send_task(prompt, context_id=context_id)
            return {"ok": True, "result": result}
        except Exception as exc:  # noqa: BLE001 — tools must never raise
            log.warning("a2a egress blocking call failed", agent=name, error=str(exc))
            return {"ok": False, "error": str(exc)}

    async def fire(prompt: str, context_id: str | None = None) -> dict[str, Any]:
        try:
            task_id = await client.send_task_async(prompt, context_id=context_id)
            store.register_task(task_id)
            return {"ok": True, "task_id": task_id}
        except Exception as exc:  # noqa: BLE001 — tools must never raise
            log.warning("a2a egress async call failed", agent=name, error=str(exc))
            return {"ok": False, "error": str(exc)}

    async def status(task_id: str, wait: bool = False, timeout: float = 300.0) -> dict[str, Any]:
        try:
            if wait:
                resolved = await store.wait_for_task(task_id, timeout=timeout)
                if resolved is None:
                    return {
                        "ok": False,
                        "error": f"timeout waiting for task {task_id}",
                    }
                return {
                    "ok": True,
                    "status": resolved.get("status"),
                    "result": resolved.get("result"),
                }
            polled = await client.get_task_status(task_id)
            return {
                "ok": True,
                "status": polled.get("status"),
                "result": polled.get("result"),
            }
        except Exception as exc:  # noqa: BLE001 — tools must never raise
            log.warning("a2a egress status call failed", agent=name, error=str(exc))
            return {"ok": False, "error": str(exc)}

    return [
        ToolSpec(
            name=f"a2a_{name}",
            description=(f"Send a prompt to peer agent '{name}' and wait for its reply."),
            handler=blocking,
        ),
        ToolSpec(
            name=f"a2a_{name}_async",
            description=(
                f"Send a prompt to peer agent '{name}' without waiting; "
                "returns a task_id to poll later."
            ),
            handler=fire,
        ),
        ToolSpec(
            name=f"a2a_{name}_status",
            description=(
                f"Check status/result of a task on peer agent '{name}'. "
                "Set wait=true to block until completion."
            ),
            handler=status,
        ),
    ]


# ---------------------------------------------------------------------------
# build_a2a_mcp_server — register ToolSpecs on a FastMCP server
# ---------------------------------------------------------------------------


def build_a2a_mcp_server(tools: list[ToolSpec]) -> Any:
    """Build a FastMCP server named 'a2a-egress' with each ToolSpec registered.

    Uses ``FastMCP.add_tool(fn, name=..., description=...)`` (mcp>=1.28).
    """
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("a2a-egress")
    for spec in tools:
        server.add_tool(spec.handler, name=spec.name, description=spec.description)
    return server

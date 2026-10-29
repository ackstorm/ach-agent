# SPDX-License-Identifier: MIT
"""Legacy A2A channel bridge into Harness Admission and its opaque result future."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
from collections.abc import Callable
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from ach_runtime.channels.event import Event
from ach_runtime.channels.metrics import CHANNEL_INBOUND
from ach_runtime.channels.routing import RoutingError, route
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.execution.wire import RunStatus
from ach_runtime.harness.admission import (
    AdmissionClosedError,
    AdmissionQueueFullError,
    AdmissionTicket,
)
from ach_runtime.harness.workspaces import WorkspaceAssemblyUnavailable

log = structlog.get_logger(__name__)

_CARD_PATHS = frozenset({"/.well-known/agent-card.json", "/.well-known/agent.json"})
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)
_OPENER_RE = re.compile(r'\{\s*"action"')

A2A_OUTPUT_INSTRUCTIONS = (
    "<output_format>\n"
    "Do your reasoning and tool work first. Then END your reply with exactly one compact "
    "JSON object on its own line — this object is the ONLY thing delivered to the caller:\n"
    '{"action":"a2a_reply","text":"<RESULT>","thoughts":"<OPTIONAL>"}\n'
    '"text" is what the caller reads — keep it non-empty. Single line, keys in this order, '
    "no code fences.\n"
    "</output_format>"
)


def _find_matching_brace(text: str, start: int) -> int:
    """Return the matching closing brace, respecting nested values and string escapes."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        char = text[i]
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"' and not escape:
            in_string = not in_string
        if not in_string:
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def extract_terminal(accumulated_text: str) -> dict[str, Any] | None:
    """Copy of the legacy last-valid-action extraction algorithm."""
    text = accumulated_text
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    matches = list(_OPENER_RE.finditer(text))
    for match in reversed(matches):
        pos = match.start()
        end = _find_matching_brace(text, pos)
        if end < 0:
            continue
        try:
            result = json.loads(text[pos : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    return None


class A2AReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["a2a_reply"]
    text: str
    thoughts: str = ""


def validate_a2a_reply(value: dict[str, Any] | None) -> dict[str, str] | None:
    """Accept exactly the legacy A2A reply shape, with a nonblank caller-facing text."""
    if value is None or value.get("action") != "a2a_reply":
        return None
    try:
        parsed = A2AReply.model_validate(value)
    except ValidationError:
        return None
    if not parsed.text.strip():
        return None
    return parsed.model_dump()


# Request headers exposed to routing templates as event.headers.*, minus credentials.
_UNROUTABLE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


def _authorized(channel: ChannelConfig, headers: dict[str, str]) -> bool:
    auth = channel.a2a.auth if channel.a2a is not None else None
    expected = os.environ.get(auth.env, "") if auth is not None else ""
    if not expected:
        return False
    presented = headers.get(auth.header.lower(), "")
    return hmac.compare_digest(presented.encode(), expected.encode())


class _AuthGuardMiddleware:
    """Guard every SDK route, including task-store routes that bypass the executor."""

    def __init__(self, app: Any, channel_cfg: ChannelConfig) -> None:
        self._app = app
        self._channel_cfg = channel_cfg

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        from starlette.requests import Request
        from starlette.responses import JSONResponse

        request = Request(scope, receive)
        root_path = scope.get("root_path", "")
        exempt_paths = {root_path + path for path in _CARD_PATHS}
        if request.method in {"GET", "HEAD"} and request.url.path in exempt_paths:
            await self._app(scope, receive, send)
            return
        if not _authorized(self._channel_cfg, dict(request.headers)):
            response = JSONResponse({"error": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _status_event(state: str, text: str | None, task_id: str, context_id: str) -> Any:
    from a2a.types.a2a_pb2 import (
        TASK_STATE_CANCELED,
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        TASK_STATE_REJECTED,
        TASK_STATE_WORKING,
        Message,
        TaskStatus,
        TaskStatusUpdateEvent,
    )

    states = {
        "canceled": TASK_STATE_CANCELED,
        "completed": TASK_STATE_COMPLETED,
        "failed": TASK_STATE_FAILED,
        "rejected": TASK_STATE_REJECTED,
        "working": TASK_STATE_WORKING,
    }
    if text is None:
        status = TaskStatus(state=states[state])
    else:
        message = Message()
        message.parts.add().text = text
        status = TaskStatus(state=states[state], message=message)
    return TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status)


async def _shielded(future: asyncio.Future[Any]) -> Any:
    return await asyncio.shield(future)


async def _wait_or_cancel(future: asyncio.Future[Any], cancelled: asyncio.Event) -> Any:
    # Shield inside a coroutine: asyncio.shield returns a Future, not a coroutine.
    future_waiter = asyncio.create_task(_shielded(future))
    cancel_waiter = asyncio.create_task(cancelled.wait())
    try:
        done, _ = await asyncio.wait(
            (future_waiter, cancel_waiter), return_when=asyncio.FIRST_COMPLETED
        )
        if cancel_waiter in done:
            return _CANCELLED
        return future_waiter.result()
    finally:
        for task in (future_waiter, cancel_waiter):
            if not task.done():
                task.cancel()
        await asyncio.gather(future_waiter, cancel_waiter, return_exceptions=True)


_CANCELLED = object()


class A2AAgentExecutorBridge:
    """Maps SDK task waiters to real admission tickets and their opaque results."""

    def __init__(
        self,
        control: Any,
        channel_cfg: ChannelConfig,
        *,
        ready: Callable[[], bool] | None = None,
    ) -> None:
        self._control = control
        self._channel_cfg = channel_cfg
        self._ready = ready or (lambda: not control._stopping)
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._waiter_counts: dict[str, int] = {}

    @property
    def channel_cfg(self) -> ChannelConfig:
        return self._channel_cfg

    async def shutdown(self) -> None:
        for event in tuple(self._cancel_events.values()):
            event.set()

    async def stop(self) -> None:
        """Source lifecycle alias; the bridge owns no server or separate start step."""
        await self.shutdown()

    async def execute(self, context: Any, event_queue: Any) -> None:
        task_id = getattr(context, "task_id", None) or ""
        context_id = getattr(context, "context_id", None) or ""
        headers: dict[str, str] = {}
        call_context = getattr(context, "call_context", None)
        if call_context is not None:
            headers = call_context.state.get("headers", {})
        if not _authorized(self._channel_cfg, headers):
            await event_queue.enqueue_event(_status_event("failed", "Unauthorized", task_id, context_id))
            return
        if not task_id:
            await event_queue.enqueue_event(
                _status_event("failed", "Missing task identifier", task_id, context_id)
            )
            return
        if not self._ready() or self._control._stopping:
            await event_queue.enqueue_event(
                _status_event("failed", "Control is not ready", task_id, context_id)
            )
            return

        text = context.get_user_input() if hasattr(context, "get_user_input") else ""
        auth = self._channel_cfg.a2a.auth if self._channel_cfg.a2a is not None else None
        hidden = _UNROUTABLE_HEADERS | ({auth.header.lower()} if auth is not None else set())
        event = Event(
            event_id=f"a2a:{task_id}", channel=self._channel_cfg.name, type="a2a", source="a2a",
            payload={"text": text, "task_id": task_id, "context_id": context_id},
            context={
                "a2a": {"taskId": task_id, "contextId": context_id},
                "headers": {k.lower(): v for k, v in headers.items() if k.lower() not in hidden},
            },
        )
        try:
            routed = route(event, self._channel_cfg)
        except RoutingError as exc:
            await event_queue.enqueue_event(_status_event("failed", str(exc), task_id, context_id))
            return
        CHANNEL_INBOUND.labels(channel=self._channel_cfg.name, type="a2a").inc()
        cancel_event = self._cancel_events.setdefault(task_id, asyncio.Event())
        self._waiter_counts[task_id] = self._waiter_counts.get(task_id, 0) + 1
        try:
            ticket = self._control.admission.admit(routed)
            started = await _wait_or_cancel(ticket.started, cancel_event)
            if started is _CANCELLED:
                return
            await event_queue.enqueue_event(_status_event("working", None, task_id, context_id))
            result = await _wait_or_cancel(ticket.completed, cancel_event)
            if result is _CANCELLED:
                return
            if not isinstance(result, RunStatus):
                await event_queue.enqueue_event(
                    _status_event("failed", "Invocation failed", task_id, context_id)
                )
                return
            if result.state == "skipped":
                await event_queue.enqueue_event(
                    _status_event("rejected", "skipped by sessionStart", task_id, context_id)
                )
                return
            if result.state != "completed":
                await event_queue.enqueue_event(
                    _status_event(
                        "failed", result.error or "Invocation failed", task_id, context_id
                    )
                )
                return
            raw_result = result.result
            raw_text = raw_result.get("text") if isinstance(raw_result, dict) else None
            terminal = extract_terminal(raw_text) if isinstance(raw_text, str) else None
            reply = validate_a2a_reply(terminal)
            if reply is None:
                await event_queue.enqueue_event(
                    _status_event("failed", "invalid terminal output", task_id, context_id)
                )
                return
            await event_queue.enqueue_event(
                _status_event("completed", reply["text"], task_id, context_id)
            )
        except (AdmissionClosedError, AdmissionQueueFullError, WorkspaceAssemblyUnavailable, TimeoutError) as exc:
            failure = "Queue full" if isinstance(exc, AdmissionQueueFullError) else str(exc)
            await event_queue.enqueue_event(
                _status_event("failed", failure or "Invocation failed", task_id, context_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("a2a admission or result failed", channel=self._channel_cfg.name)
            await event_queue.enqueue_event(
                _status_event("failed", "Invocation failed", task_id, context_id)
            )
        finally:
            self._waiter_counts[task_id] -= 1
            if self._waiter_counts[task_id] == 0:
                self._waiter_counts.pop(task_id, None)
                self._cancel_events.pop(task_id, None)

    async def cancel(self, context: Any, event_queue: Any) -> None:
        task_id = getattr(context, "task_id", None) or ""
        context_id = getattr(context, "context_id", None) or ""
        waiter = self._cancel_events.get(task_id)
        if waiter is not None:
            waiter.set()
        await event_queue.enqueue_event(_status_event("canceled", None, task_id, context_id))


def make_a2a_agent_card(channel_name: str) -> Any:
    from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard

    return AgentCard(
        name=channel_name,
        description=f"ach-runtime A2A receiver for channel {channel_name}",
        version="1.0.0",
        capabilities=AgentCapabilities(),
        default_input_modes=["text"],
        default_output_modes=["text"],
    )


def build_a2a_app(agent_card: Any, executor: A2AAgentExecutorBridge) -> Any:
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
    from a2a.server.request_handlers.response_helpers import agent_card_to_dict
    from a2a.server.routes import create_jsonrpc_routes, create_rest_routes
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    class _ExecutorAdapter(AgentExecutor):
        async def execute(self, context: Any, event_queue: Any) -> None:
            await executor.execute(context, event_queue)

        async def cancel(self, context: Any, event_queue: Any) -> None:
            await executor.cancel(context, event_queue)

    handler = LegacyRequestHandler(
        agent_executor=_ExecutorAdapter(), task_store=InMemoryTaskStore(), agent_card=agent_card
    )
    sub_app = FastAPI(title="a2a-sub")
    sub_app.add_middleware(_AuthGuardMiddleware, channel_cfg=executor.channel_cfg)

    async def compat_agent_card(request: Request) -> JSONResponse:
        card = agent_card_to_dict(agent_card)
        prefix = request.url.path.split("/.well-known/")[0]
        endpoint = f"{str(request.base_url).rstrip('/')}{prefix}"
        card.setdefault("skills", [])
        card.setdefault("url", endpoint)
        card.setdefault("protocolVersion", "1.0")
        card.setdefault("preferredTransport", "JSONRPC")
        card.setdefault("supportedInterfaces", [
            {"url": endpoint, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ])
        return JSONResponse(card)

    card_routes = [
        Route(path, compat_agent_card, methods=["GET"])
        for path in ("/.well-known/agent-card.json", "/.well-known/agent.json")
    ]
    add_a2a_routes_to_fastapi(
        sub_app,
        agent_card_routes=card_routes,
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
        rest_routes=create_rest_routes(handler),
    )
    return sub_app

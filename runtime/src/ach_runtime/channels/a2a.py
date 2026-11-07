# SPDX-License-Identifier: MIT
"""Legacy A2A channel bridge into Harness Admission and its opaque result future."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import re
import time
from datetime import UTC, datetime, timedelta
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.channels.metrics import CHANNEL_INBOUND
from ach_runtime.channels.routing import RoutingError, route
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.execution.wire import RunStatus
from ach_runtime.harness.admission import (
    AdmissionClosedError,
    AdmissionQueueFullError,
    AdmissionRateLimitedError,
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

A2A_PROGRESS_INSTRUCTIONS = (
    "<progress_updates>\n"
    "The caller waits while you work. Before slow work (a long analysis, a heavy tool run, a "
    "multi-step change), call the `inform_user` tool with one short sentence on what you are "
    "doing. Use it for milestones only, not for every step. It never replaces the final "
    "a2a_reply.\n"
    "</progress_updates>"
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


def _status_event(
    state: str, text: str | None, task_id: str, context_id: str, metadata: dict[str, str] | None = None
) -> Any:
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
    return TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status, metadata=metadata)


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

RESTARTING_RUNNING = (
    "The agent is restarting. Your request keeps running: get its result with tasks/get "
    "on this task, or ask again in this conversation."
)
RESTARTING_QUEUED = "The agent restarted before your request started. Send it again."
# Runtime voice, separate from the agent's inform_user: short, English, tagged in metadata.
STARTING_AFTER_SECONDS = 5.0
HEARTBEAT_SECONDS = 60.0
QUEUED_TEXT = "Queued behind an earlier request in this conversation."
STARTING_TEXT = "Starting the agent."
HEARTBEAT_TEXT = "Still working..."
RUN_LOST = "The agent restarted and this request's run was lost. Send it again."
RESUME_PROMPT = (
    "The ACH control restarted while you were working. Model and tool calls made during "
    "the restart may have failed with connection errors. Continue the task where you "
    "left off, and repeat any tool call whose result you did not get."
)


def _outcome_status(result: Any) -> tuple[str, str]:
    """The A2A task state and text of a finished run."""
    if not isinstance(result, RunStatus):
        return "failed", "Invocation failed"
    if result.state == "skipped":
        return "rejected", "skipped by sessionStart"
    if result.state != "completed":
        return "failed", result.error or "Invocation failed"
    raw_text = result.result.get("text") if isinstance(result.result, dict) else None
    terminal = extract_terminal(raw_text) if isinstance(raw_text, str) else None
    reply = validate_a2a_reply(terminal)
    if reply is None:
        return "failed", "invalid terminal output"
    return "completed", reply["text"]


class _Pulse:
    """Every event one A2A waiter sends, so the watcher knows how long it has been silent."""

    def __init__(self, event_queue: Any, task_id: str, context_id: str) -> None:
        self._queue, self._task_id, self._context_id = event_queue, task_id, context_id
        self.last_sent = time.monotonic()

    async def send(
        self, state: str, text: str | None, kind: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        if kind:
            metadata = {**(metadata or {}), "ach": kind}
        await self._queue.enqueue_event(
            _status_event(state, text, self._task_id, self._context_id, metadata=metadata))
        self.last_sent = time.monotonic()


async def _stop(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


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
        # True when a run outlives this control: the next control adopts it.
        self.runs_outlive_control = False
        self._shutting_down = False

    @property
    def channel_cfg(self) -> ChannelConfig:
        return self._channel_cfg

    async def shutdown(self) -> None:
        self._shutting_down = True
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
        emit: Callable[[str], Awaitable[None]] | None = None
        invocation_id = ""
        watcher: asyncio.Task[None] | None = None
        try:
            ticket = self._control.admission.admit(routed)
            pulse = _Pulse(event_queue, task_id, context_id)
            if isinstance(ticket, AdmissionTicket):
                watcher = asyncio.create_task(self._watch(ticket, pulse))

            async def emit(message: str) -> None:
                await pulse.send("working", message)

            invocation_id = ticket.invocation_id
            self._control.progress.setdefault(invocation_id, []).append(emit)
            started = await _wait_or_cancel(ticket.started, cancel_event)
            if started is _CANCELLED:
                await _stop(watcher)
                if self._shutting_down and self.runs_outlive_control:
                    await event_queue.enqueue_event(
                        _status_event("failed", RESTARTING_QUEUED, task_id, context_id))
                return
            await pulse.send("working", None, metadata={
                "invocationId": invocation_id,
                "workspaceKey": routed.workspace_key,
                "sessionKey": routed.session_key,
            })
            result = await _wait_or_cancel(ticket.completed, cancel_event)
            await _stop(watcher)
            if result is _CANCELLED:
                if self._shutting_down and self.runs_outlive_control:
                    await event_queue.enqueue_event(
                        _status_event("working", RESTARTING_RUNNING, task_id, context_id))
                return
            state, text = _outcome_status(result)
            await event_queue.enqueue_event(_status_event(state, text, task_id, context_id))
        except (AdmissionClosedError, AdmissionQueueFullError, AdmissionRateLimitedError,
                WorkspaceAssemblyUnavailable, TimeoutError) as exc:
            await _stop(watcher)
            failure = (
                "Queue full" if isinstance(exc, AdmissionQueueFullError)
                else "Rate limited" if isinstance(exc, AdmissionRateLimitedError)
                else str(exc)
            )
            await event_queue.enqueue_event(
                _status_event("failed", failure or "Invocation failed", task_id, context_id)
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("a2a admission or result failed", channel=self._channel_cfg.name)
            await _stop(watcher)
            await event_queue.enqueue_event(
                _status_event("failed", "Invocation failed", task_id, context_id)
            )
        finally:
            await _stop(watcher)
            if emit is not None:
                emitters = self._control.progress.get(invocation_id, [])
                emitters.remove(emit)
                if not emitters:
                    self._control.progress.pop(invocation_id, None)
            self._waiter_counts[task_id] -= 1
            if self._waiter_counts[task_id] == 0:
                self._waiter_counts.pop(task_id, None)
                self._cancel_events.pop(task_id, None)

    async def _watch(self, ticket: Any, pulse: _Pulse) -> None:
        """Say so when the run is queued or slow to start, then whenever the waiter is silent."""
        try:
            await asyncio.wait_for(asyncio.shield(ticket.started), STARTING_AFTER_SECONDS)
        except TimeoutError:
            if self._control.admission.session_busy(ticket):
                await pulse.send("working", QUEUED_TEXT, "queued")
            else:
                await pulse.send("working", STARTING_TEXT, "starting")
        except Exception:  # a failed start is execute's to report
            return
        while True:
            await asyncio.sleep(max(0.0, pulse.last_sent + HEARTBEAT_SECONDS - time.monotonic()))
            if time.monotonic() - pulse.last_sent >= HEARTBEAT_SECONDS:
                await pulse.send("working", HEARTBEAT_TEXT, "heartbeat")

    async def recover(self, store: Any) -> None:
        """Write the outcome of runs that outlived the previous control into their tasks."""
        retention = self._control.config.workspace.persistence.retention_days
        await store.prune(older_than=datetime.now(UTC) - timedelta(days=retention))
        await asyncio.gather(*(self._recover_task(store, task) for task in await store.pending()))

    async def _recover_task(self, store: Any, task: Any) -> None:
        try:
            meta = dict(task.metadata)
            if self._control.admission.has_ticket(meta.get("invocationId", "")):
                return  # a request this control admitted after startup, not one to recover
            result = await self._recovered_result(meta.get("invocationId", ""))
            if result is None:
                await store.finish(task, "failed", RUN_LOST)
                return
            if result.state == "failed" and meta.get("resumed") != "1" and "sessionKey" in meta:
                result = await self._resume(store, task, meta)
            if result is None:
                await store.finish(task, "failed", RUN_LOST)
                return
            state, text = _outcome_status(result)
            await store.finish(task, state, text)
        except Exception:
            # The task stays non-terminal: the next control's recovery tries again.
            log.warning("a2a task recovery failed", channel=self._channel_cfg.name, task_id=task.id)

    async def _recovered_result(self, invocation_id: str) -> RunStatus | None:
        run = self._control.admission.recovered_run(invocation_id)
        if run is not None:
            try:
                await asyncio.shield(run.completed)  # resolves to None; the outcome is in finished_runs
            except Exception:
                return None
        return self._control.finished_runs.pop(invocation_id, None)

    async def _resume(self, store: Any, task: Any, meta: dict[str, Any]) -> RunStatus | None:
        """One message into the same session; its outcome becomes the task's."""
        event = Event(
            event_id=f"a2a:{task.id}:resume", channel=self._channel_cfg.name, type="a2a",
            source="a2a",
            payload={"text": RESUME_PROMPT, "task_id": task.id, "context_id": task.context_id},
            context={"a2a": {"taskId": task.id, "contextId": task.context_id}, "headers": {}},
        )
        ticket = self._control.admission.admit(
            RoutedEvent(event=event, workspace_key=meta["workspaceKey"], session_key=meta["sessionKey"]))
        # Stamped before waiting: if the control restarts again, the next recovery follows
        # the resume run and never resumes a second time.
        await store.annotate(task, {"invocationId": ticket.invocation_id, "resumed": "1"})
        try:
            return await asyncio.shield(ticket.completed)
        except Exception:
            return None

    async def cancel(self, context: Any, event_queue: Any) -> None:
        task_id = getattr(context, "task_id", None) or ""
        context_id = getattr(context, "context_id", None) or ""
        waiter = self._cancel_events.get(task_id)
        if waiter is not None:
            waiter.set()
        await event_queue.enqueue_event(_status_event("canceled", None, task_id, context_id))


def make_a2a_agent_card(agent_name: str, channel_name: str) -> Any:
    """The card of one A2A channel. Its interface URL is the mount path: the served card
    replaces it with the absolute URL the caller reached it on."""
    from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

    return AgentCard(
        name=agent_name,
        description=f"Agent {agent_name}, A2A channel {channel_name}: send a text message, get a text answer.",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text"],
        default_output_modes=["text"],
        supported_interfaces=[
            AgentInterface(url=f"/a2a/{channel_name}", protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        skills=[
            AgentSkill(
                id=channel_name,
                name=channel_name,
                description=f"Answers a text message as agent {agent_name}.",
                tags=["chat"],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
    )


def build_a2a_app(agent_card: Any, executor: A2AAgentExecutorBridge, task_store: Any = None) -> Any:
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
        agent_executor=_ExecutorAdapter(), task_store=task_store or InMemoryTaskStore(), agent_card=agent_card
    )
    sub_app = FastAPI(title="a2a-sub")
    sub_app.add_middleware(_AuthGuardMiddleware, channel_cfg=executor.channel_cfg)

    async def compat_agent_card(request: Request) -> JSONResponse:
        card = agent_card_to_dict(agent_card)
        prefix = request.url.path.split("/.well-known/")[0]
        endpoint = f"{str(request.base_url).rstrip('/')}{prefix}"
        card["url"] = endpoint
        card.setdefault("protocolVersion", "1.0")
        card.setdefault("preferredTransport", "JSONRPC")
        card["supportedInterfaces"] = [
            {"url": endpoint, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ]
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

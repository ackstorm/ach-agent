# SPDX-License-Identifier: Apache-2.0
"""A2A channel adapter — a2a-sdk AgentExecutor bridge (CHN-05, D-03/D-05/D-06, §14.6).

Locked decisions:
  - A2AAgentExecutorBridge subclasses AgentExecutor; SDK routes calls to execute()/cancel().
  - Header auth FIRST (spec §14.6): x-a2a-custom-api-key checked via hmac.compare_digest
    BEFORE any executor dispatch; missing/wrong header → failed TaskStatusUpdateEvent,
    handler.handle NEVER called (T-04-13).
  - session_key = context_id (fallback task_id); idempotency = derive_a2a_idempotency_key.
  - A′ gate (D-06): engine not ready → failed TaskStatusUpdateEvent (503-style), not silent.
  - FULL_QUEUE (D-05/RTR-05): failed TaskStatusUpdateEvent, not silent drop.
  - source_trait = "async_no_retry": delivery bridge via signal_completion(session_key, text).
  - Secret read LAZILY from mounted path at use time, NEVER cached or logged (CONTRACT §3).
  - build_a2a_app(agent_card, executor, rpc_prefix): creates InMemoryTaskStore +
    LegacyRequestHandler + wires routes via add_a2a_routes_to_fastapi on a FastAPI sub-app.

RTR-06: a2a.* imports ONLY inside this file — function-scoped, never at module level.
        Never imported in seam.py, router.*, engine.*.
Boot-order: imported after configure_logging().
"""

from __future__ import annotations

import asyncio
import hmac
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.dedup import derive_a2a_idempotency_key
from ach_agent.router.metrics import CHANNEL_INBOUND, COLD_START_DROPS
from ach_agent.router.router import RouterAdmitResult

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig

log = structlog.get_logger(__name__)

# RTR-06: a2a imports are ONLY inside functions/methods below — never at module level.
# Verified by: grep -nE "^import a2a|^from a2a" src/ach_agent/channels/a2a.py → zero results.


def _read_secret(secret_path: str) -> str | None:
    """Read secret value from mounted path at call time. NEVER cache or log (CONTRACT §3).

    Returns None if the path is empty or the file cannot be read.
    The caller MUST treat None as an auth failure (fail-closed — CR-02).
    """
    if not secret_path:
        return None
    try:
        return Path(secret_path).read_text(encoding="utf-8").strip()
    except OSError:
        log.error("a2a: secret path not readable — rejecting all requests", path=secret_path)
        return None


def _make_failed_status(message: str) -> Any:
    """Build a TaskStatusUpdateEvent with state=FAILED, final semantics."""
    from a2a.types.a2a_pb2 import (
        TASK_STATE_FAILED,
        Message,
        TaskStatus,
        TaskStatusUpdateEvent,
    )

    msg = Message()
    part = msg.parts.add()
    part.text = message
    status = TaskStatus(state=TASK_STATE_FAILED, message=msg)
    return TaskStatusUpdateEvent(status=status)


def _make_canceled_status() -> Any:
    """Build a TaskStatusUpdateEvent with state=CANCELED."""
    from a2a.types.a2a_pb2 import TASK_STATE_CANCELED, TaskStatus, TaskStatusUpdateEvent

    status = TaskStatus(state=TASK_STATE_CANCELED)
    return TaskStatusUpdateEvent(status=status)


def _make_completed_status(reply_text: str) -> Any:
    """Build a TaskStatusUpdateEvent with state=COMPLETED and reply text."""
    from a2a.types.a2a_pb2 import (
        TASK_STATE_COMPLETED,
        Message,
        TaskStatus,
        TaskStatusUpdateEvent,
    )

    msg = Message()
    part = msg.parts.add()
    part.text = reply_text
    status = TaskStatus(state=TASK_STATE_COMPLETED, message=msg)
    return TaskStatusUpdateEvent(status=status)


class A2AAgentExecutorBridge:
    """Bridges the a2a-sdk AgentExecutor interface to the ach_agent router seam.

    RTR-06: a2a-sdk types used only inside methods; no module-level a2a imports.

    The bridge acts as the AgentExecutor (subclassed within mount_a2a_app where
    the a2a import is function-scoped). Tests call execute() directly with a
    MockEventQueue rather than going through the SDK machinery.
    """

    def __init__(
        self,
        handler: MessageHandler | None,
        pool: Any,
        channel_cfg: ChannelConfig,
    ) -> None:
        self._handler: MessageHandler | None = handler
        self._pool = pool
        self._channel_cfg = channel_cfg
        # Maps session_key → (event_queue, completion_event)
        self._pending: dict[str, tuple[Any, asyncio.Event]] = {}

    async def execute(self, context: Any, event_queue: Any) -> None:
        """AgentExecutor.execute implementation.

        Order:
          (1) Header auth (spec §14.6) — BEFORE any dispatch.
          (2) A′ gate — engine not ready → failed event (D-06).
          (3) Route to handler; on FULL_QUEUE → failed event (D-05/RTR-05).
          (4) Await completion signal (set by signal_completion via on_complete callback).
        """
        # (1) HEADER AUTH — spec §14.6 / T-04-13
        # Fail-closed (CR-01): if no a2a sub-block or no secretPath configured,
        # reject the request rather than admitting unauthenticated callers.
        a2a_cfg = self._channel_cfg.a2a
        if a2a_cfg is None or not a2a_cfg.auth.secret_path:
            log.warning(
                "a2a: request rejected — no auth secret configured (fail-closed §14.6)",
                channel=self._channel_cfg.name,
            )
            await event_queue.enqueue_event(_make_failed_status("Unauthorized"))
            return

        expected_header = a2a_cfg.auth.header  # e.g. "x-a2a-custom-api-key"
        expected_value = _read_secret(a2a_cfg.auth.secret_path)
        if expected_value is None:
            # CR-02: unreadable/empty secret — never admit (empty-vs-empty must fail)
            await event_queue.enqueue_event(_make_failed_status("Unauthorized"))
            return

        # Retrieve presented header from call_context.state['headers'] (dict from
        # DefaultServerCallContextBuilder). Key is lowercase (HTTP headers are
        # case-insensitive; Starlette normalises to lowercase).
        headers: dict[str, str] = {}
        if hasattr(context, "call_context") and context.call_context is not None:
            headers = context.call_context.state.get("headers", {})
        presented = headers.get(expected_header.lower(), "")
        # Constant-time compare; NEVER log expected or presented value (SEC / T-04-13).
        if not hmac.compare_digest(presented.encode(), expected_value.encode()):
            log.warning(
                "a2a: request rejected — missing or invalid auth header",
                channel=self._channel_cfg.name,
                header=expected_header,
            )
            await event_queue.enqueue_event(_make_failed_status("Unauthorized"))
            return

        # (2) A′ gate (D-06) — engine not ready-once
        if self._pool is not None and not self._pool.engine_has_been_ready_once:
            log.warning(
                "a2a: request rejected — engine not ready (A′ cold-start gate)",
                channel=self._channel_cfg.name,
            )
            COLD_START_DROPS.labels(channel=self._channel_cfg.name).inc()
            await event_queue.enqueue_event(_make_failed_status("Service warming up"))
            return

        # (3) Build MessageEvent and dispatch to router seam
        task_id: str = getattr(context, "task_id", None) or ""
        context_id: str = getattr(context, "context_id", None) or ""
        session_key = context_id or task_id
        # CR-04: reject when both identifiers are empty — prevents _pending[""] collision
        # where a second concurrent call overwrites the first coroutine's Event.
        if not session_key:
            log.warning(
                "a2a: request rejected — both context_id and task_id are empty",
                channel=self._channel_cfg.name,
            )
            await event_queue.enqueue_event(_make_failed_status("Missing task/context identifier"))
            return
        idempotency_key = derive_a2a_idempotency_key(task_id)

        # Extract text from RequestContext (get_user_input is available if SDK is present)
        if hasattr(context, "get_user_input"):
            text = context.get_user_input()
        else:
            text = ""

        CHANNEL_INBOUND.labels(channel=self._channel_cfg.name, type="a2a").inc()

        # Register pending BEFORE dispatch so signal_completion can find it
        completion = asyncio.Event()
        self._pending[session_key] = (event_queue, completion)

        event = MessageEvent(
            idempotency_key=idempotency_key,
            session_key=session_key,
            channel_name=self._channel_cfg.name,
            payload={"text": text, "task_id": task_id, "context_id": context_id},
            source_trait="async_no_retry",  # HTTP-delivered, but completion is out-of-band
        )

        assert self._handler is not None, "_handler not wired before execute()"
        result = await self._handler.handle(event)
        if result == RouterAdmitResult.FULL_QUEUE:
            log.warning(
                "a2a: request rejected — queue full (D-05/RTR-05)",
                channel=self._channel_cfg.name,
            )
            self._pending.pop(session_key, None)
            await event_queue.enqueue_event(_make_failed_status("Queue full"))
            return
        if result == RouterAdmitResult.DUPLICATE:
            log.info("a2a: duplicate task_id — deduplicated", channel=self._channel_cfg.name)
            self._pending.pop(session_key, None)
            return

        # (4) Await out-of-band completion from engine via signal_completion
        await completion.wait()

    async def cancel(self, context: Any, event_queue: Any) -> None:
        """AgentExecutor.cancel — enqueue a canceled event."""
        task_id: str = getattr(context, "task_id", None) or ""
        context_id: str = getattr(context, "context_id", None) or ""
        session_key = context_id or task_id or ""
        # Remove from pending if present
        self._pending.pop(session_key, None)
        await event_queue.enqueue_event(_make_canceled_status())
        log.info("a2a: task canceled", channel=self._channel_cfg.name, session_key=session_key)

    def signal_completion(self, session_key: str, reply_text: str) -> None:
        """Called by the on_complete closure (boot module) after engine_runner delivers.

        Pops pending, enqueues completed event, sets the asyncio.Event so execute() unblocks.
        This is the delivery seam callback (Pitfall 5 — executor must not hang forever).
        """
        entry = self._pending.pop(session_key, None)
        if entry is None:
            log.warning(
                "a2a: signal_completion called for unknown session_key",
                session_key=session_key,
            )
            return
        event_queue, completion = entry
        # Schedule completed event enqueue + Event.set() as an async task.
        # signal_completion is called from a synchronous context (on_complete closure).
        loop = asyncio.get_running_loop()
        loop.create_task(_complete_async(event_queue, completion, reply_text))

    def lookup_session_key_for_event(self, session_key: str) -> bool:
        """Test helper: check if a session_key is currently pending."""
        return session_key in self._pending


async def _complete_async(event_queue: Any, completion: asyncio.Event, reply_text: str) -> None:
    """Schedule completed event and set the completion event from an async context."""
    await event_queue.enqueue_event(_make_completed_status(reply_text))
    completion.set()


def make_a2a_agent_card(channel_name: str) -> Any:
    """Build a minimal AgentCard for the given channel name.

    RTR-06: a2a imports are function-scoped inside this function.
    Keeps a2a.types imports OUT of main.py (RTR-06 fence).
    """
    from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard

    # NOTE: a2a.types.a2a_pb2.AgentCard has no top-level `url` field — the protobuf
    # schema advertises service endpoints via `supported_interfaces`. Passing url=
    # raised ValueError at runtime (untested path). Receiver-only v1 is served at a
    # known mount prefix, so the endpoint URL is not advertised on the card here.
    # TODO(a2a): advertise the endpoint via supported_interfaces in a follow-up.
    return AgentCard(
        name=channel_name,
        description=f"ach-agent A2A receiver for channel {channel_name}",
        version="1.0.0",
        capabilities=AgentCapabilities(),
    )


def build_a2a_app(
    agent_card: Any,
    executor: A2AAgentExecutorBridge,
    rpc_prefix: str = "/",
) -> Any:
    """Build a FastAPI sub-app with A2A routes mounted.

    RTR-06: all a2a imports are function-scoped inside this function.

    Args:
        agent_card:  a2a-sdk AgentCard (or None to skip agent card route).
        executor:    A2AAgentExecutorBridge instance.
        rpc_prefix:  URL prefix for the JSON-RPC endpoint within the sub-app.

    Returns:
        A FastAPI application with A2A routes registered (mount it via app.mount).
    """
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
    from a2a.server.routes import (
        create_agent_card_routes,
        create_jsonrpc_routes,
        create_rest_routes,
    )
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from fastapi import FastAPI

    # A2AAgentExecutorBridge is NOT a real AgentExecutor subclass (avoids module-level
    # a2a imports). Wrap it in a thin adapter that the SDK accepts.
    class _ExecutorAdapter(AgentExecutor):
        async def execute(self, context: Any, event_queue: Any) -> None:
            await executor.execute(context, event_queue)

        async def cancel(self, context: Any, event_queue: Any) -> None:
            await executor.cancel(context, event_queue)

    task_store = InMemoryTaskStore()
    request_handler = LegacyRequestHandler(
        agent_executor=_ExecutorAdapter(),
        task_store=task_store,
        agent_card=agent_card,
    )

    sub_app = FastAPI(title="a2a-sub")
    agent_card_routes = create_agent_card_routes(agent_card) if agent_card is not None else []
    jsonrpc_routes = create_jsonrpc_routes(request_handler, rpc_url=rpc_prefix)
    rest_routes = create_rest_routes(request_handler)

    add_a2a_routes_to_fastapi(
        sub_app,
        agent_card_routes=agent_card_routes,
        jsonrpc_routes=jsonrpc_routes,
        rest_routes=rest_routes,
    )

    return sub_app

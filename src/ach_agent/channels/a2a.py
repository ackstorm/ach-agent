# SPDX-License-Identifier: Apache-2.0
"""A2A channel adapter — a2a-sdk AgentExecutor bridge (CHN-05, D-03/D-05/D-06, §14.6).

Locked decisions:
  - A2AAgentExecutorBridge subclasses AgentExecutor; SDK routes calls to execute()/cancel().
  - Header auth FIRST (spec §14.6): x-a2a-custom-api-key checked via hmac.compare_digest
    BEFORE any executor dispatch; missing/wrong header → failed TaskStatusUpdateEvent,
    handler.handle NEVER called (T-04-13).
  - session_key = context_id (fallback task_id); idempotency = derive_a2a_idempotency_key.
  - Acceptance is decoupled from engine readiness: no engine-readiness gate here — the
    engine starts lazily per session_key inside the lane (pool.acquire in engine_runner).
  - FULL_QUEUE (D-05/RTR-05): failed TaskStatusUpdateEvent, not silent drop.
  - source_trait = "async_no_retry": delivery bridge via signal_completion(session_key, text).
  - Secret resolved LAZILY (SecretSource: env) at use time, NEVER cached or logged
    (CONTRACT §3).
  - build_a2a_app(agent_card, executor): creates InMemoryTaskStore +
    LegacyRequestHandler + wires routes via add_a2a_routes_to_fastapi on a FastAPI sub-app.

RTR-06: a2a.* imports ONLY inside this file — function-scoped, never at module level.
        Never imported in seam.py, router.*, engine.*.
Boot-order: imported after configure_logging().
"""

from __future__ import annotations

import asyncio
import hmac
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import resolve_secret
from ach_agent.router.dedup import derive_a2a_idempotency_key
from ach_agent.router.metrics import CHANNEL_INBOUND
from ach_agent.router.router import RouterAdmitResult

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig

log = structlog.get_logger(__name__)

# RTR-06: a2a imports are ONLY inside functions/methods below — never at module level.
# Verified by: grep -nE "^import a2a|^from a2a" src/ach_agent/channels/a2a.py → zero results.


def _status_event(
    state: str, text: str | None = None, task_id: str = "", context_id: str = ""
) -> Any:
    """Build a TaskStatusUpdateEvent for a terminal a2a state.

    state: "failed" | "canceled" | "completed" — maps to the a2a TASK_STATE_* enum.
    text: single-part message text for FAILED/COMPLETED; CANCELED carries no message
    (omitted entirely, matching the prior per-state builders).
    task_id/context_id: MUST match the TaskManager's ids or save_task_event raises
    InvalidParamsError("Context in event doesn't match TaskManager ...") — the event's
    ids are validated against the ids the manager captured from the inbound message.
    """
    from a2a.types.a2a_pb2 import (
        TASK_STATE_CANCELED,
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        Message,
        TaskStatus,
        TaskStatusUpdateEvent,
    )

    state_map = {
        "failed": TASK_STATE_FAILED,
        "canceled": TASK_STATE_CANCELED,
        "completed": TASK_STATE_COMPLETED,
    }
    if text is None:
        status = TaskStatus(state=state_map[state])
    else:
        msg = Message()
        part = msg.parts.add()
        part.text = text
        status = TaskStatus(state=state_map[state], message=msg)
    return TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status)


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
        channel_cfg: ChannelConfig,
    ) -> None:
        self._handler: MessageHandler | None = handler
        self._channel_cfg = channel_cfg
        # Maps session_key → (event_queue, completion_event, task_id, context_id).
        # task_id/context_id are kept so the terminal event enqueued out-of-band by
        # signal_completion/signal_failure matches the TaskManager's ids (save_task_event
        # rejects a mismatch).
        self._pending: dict[str, tuple[Any, asyncio.Event, str, str]] = {}

    async def execute(self, context: Any, event_queue: Any) -> None:
        """AgentExecutor.execute implementation.

        Order:
          (1) Header auth (spec §14.6) — BEFORE any dispatch.
          (2) Route to handler (decoupled from engine readiness); on FULL_QUEUE →
              failed event (D-05/RTR-05).
          (3) Await completion signal (set by signal_completion via on_complete callback).
        """
        # Extract task/context ids up front so EVERY terminal event we enqueue (including
        # the early auth-reject paths) carries ids matching the TaskManager.
        task_id: str = getattr(context, "task_id", None) or ""
        context_id: str = getattr(context, "context_id", None) or ""

        # (1) HEADER AUTH — spec §14.6 / T-04-13
        # Fail-closed (CR-01): if no a2a sub-block or no secret configured,
        # reject the request rather than admitting unauthenticated callers.
        a2a_cfg = self._channel_cfg.a2a
        if a2a_cfg is None or a2a_cfg.auth.secret is None:
            log.warning(
                "a2a: request rejected — no auth secret configured (fail-closed §14.6)",
                channel=self._channel_cfg.name,
            )
            await event_queue.enqueue_event(
                _status_event("failed", "Unauthorized", task_id, context_id)
            )
            return

        expected_header = a2a_cfg.auth.header  # e.g. "x-a2a-custom-api-key"
        expected_value = resolve_secret(a2a_cfg.auth.secret)
        if not expected_value:
            # CR-02: unresolvable/empty secret — never admit (empty-vs-empty must fail)
            log.error(
                "a2a: secret unresolvable — rejecting all requests",
                channel=self._channel_cfg.name,
            )
            await event_queue.enqueue_event(
                _status_event("failed", "Unauthorized", task_id, context_id)
            )
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
            await event_queue.enqueue_event(
                _status_event("failed", "Unauthorized", task_id, context_id)
            )
            return

        # (2) Build MessageEvent and dispatch to router seam. Decoupled from engine
        # readiness (no gate here) — the engine starts lazily per session_key.
        session_key = context_id or task_id
        # CR-04: reject when both identifiers are empty — prevents _pending[""] collision
        # where a second concurrent call overwrites the first coroutine's Event.
        if not session_key:
            log.warning(
                "a2a: request rejected — both context_id and task_id are empty",
                channel=self._channel_cfg.name,
            )
            await event_queue.enqueue_event(
                _status_event("failed", "Missing task/context identifier", task_id, context_id)
            )
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
        self._pending[session_key] = (event_queue, completion, task_id, context_id)

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
            await event_queue.enqueue_event(
                _status_event("failed", "Queue full", task_id, context_id)
            )
            return
        if result == RouterAdmitResult.DUPLICATE:
            log.info("a2a: duplicate task_id — deduplicated", channel=self._channel_cfg.name)
            self._pending.pop(session_key, None)
            return

        # (3) Await out-of-band completion from engine via signal_completion
        await completion.wait()

    async def cancel(self, context: Any, event_queue: Any) -> None:
        """AgentExecutor.cancel — enqueue a canceled event."""
        task_id: str = getattr(context, "task_id", None) or ""
        context_id: str = getattr(context, "context_id", None) or ""
        session_key = context_id or task_id or ""
        # Remove from pending if present
        self._pending.pop(session_key, None)
        await event_queue.enqueue_event(_status_event("canceled", None, task_id, context_id))
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
        event_queue, completion, task_id, context_id = entry
        # Schedule completed event enqueue + Event.set() as an async task.
        # signal_completion is called from a synchronous context (on_complete closure).
        loop = asyncio.get_running_loop()
        loop.create_task(
            _signal_async("completed", reply_text, event_queue, completion, task_id, context_id)
        )

    def signal_failure(self, session_key: str, reason: str) -> None:
        """Called by the on_fail closure (boot) when the terminal output is unusable.

        Pops pending, enqueues a FAILED TaskStatusUpdateEvent, sets the Event so execute()
        unblocks (mirror of signal_completion — the executor must never hang, Pitfall 5).
        """
        entry = self._pending.pop(session_key, None)
        if entry is None:
            log.warning(
                "a2a: signal_failure called for unknown session_key",
                session_key=session_key,
            )
            return
        event_queue, completion, task_id, context_id = entry
        loop = asyncio.get_running_loop()
        loop.create_task(
            _signal_async("failed", reason, event_queue, completion, task_id, context_id)
        )


async def _signal_async(
    state: str,
    text: str,
    event_queue: Any,
    completion: asyncio.Event,
    task_id: str = "",
    context_id: str = "",
) -> None:
    """Schedule a terminal status event and set the completion event from an async context."""
    await event_queue.enqueue_event(_status_event(state, text, task_id, context_id))
    completion.set()


def make_a2a_agent_card(channel_name: str) -> Any:
    """Build a minimal AgentCard for the given channel name.

    RTR-06: a2a imports are function-scoped inside this function.
    Keeps a2a.types imports OUT of main.py (RTR-06 fence).
    """
    from a2a.types.a2a_pb2 import AgentCapabilities, AgentCard

    # NOTE: a2a.types.a2a_pb2.AgentCard has no top-level `url` field — the protobuf
    # schema advertises service endpoints via `supported_interfaces`. Passing url=
    # raised ValueError at runtime (untested path). The legacy top-level `url` (required
    # by a2a-sdk 0.3.x consumers) is injected in the served dict by build_a2a_app, not here.
    # defaultInputModes/defaultOutputModes ARE real 1.x fields and required by 0.3.x, so we
    # set them on the object (they serialize; empty repeated fields would be dropped).
    return AgentCard(
        name=channel_name,
        description=f"ach-agent A2A receiver for channel {channel_name}",
        version="1.0.0",
        capabilities=AgentCapabilities(),
        default_input_modes=["text"],
        default_output_modes=["text"],
    )


def build_a2a_app(
    agent_card: Any,
    executor: A2AAgentExecutorBridge,
) -> Any:
    """Build a FastAPI sub-app with A2A routes mounted.

    RTR-06: all a2a imports are function-scoped inside this function.

    Args:
        agent_card:  a2a-sdk AgentCard.
        executor:    A2AAgentExecutorBridge instance.

    Returns:
        A FastAPI application with A2A routes registered (mount it via app.mount).
    """
    from a2a.server.agent_execution.agent_executor import AgentExecutor
    from a2a.server.request_handlers.default_request_handler import LegacyRequestHandler
    from a2a.server.request_handlers.response_helpers import agent_card_to_dict
    from a2a.server.routes import (
        create_jsonrpc_routes,
        create_rest_routes,
    )
    from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
    from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.responses import JSONResponse
    from starlette.routing import Route

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

    # Cross-version-compatible card. The 1.x AgentCard drops top-level `url` (advertises
    # endpoints via supported_interfaces) and omits empty `skills`; a2a-sdk 0.3.x consumers
    # (e.g. LiteLLM proxy <= v1.90.x, which pins a2a-sdk<1.0) REQUIRE `url` and `skills`
    # and fail card validation without them. defaultInputModes/defaultOutputModes (also
    # required by 0.3.x) are set on the AgentCard itself in make_a2a_agent_card. The
    # endpoint url is per-request (base_url), so it — and the interface advertisement — must
    # be injected here, not on the object. Served at BOTH the canonical agent-card.json and
    # the legacy agent.json, since create_agent_card_routes serves only the former.
    #
    # supportedInterfaces advertises a NATIVE 1.x JSON-RPC interface at protocolVersion 1.0.
    # Without it, a 1.x client's parse_agent_card synthesizes an interface from the top-level
    # `url` and defaults its protocolVersion to 0.3.0 → the client factory selects the legacy
    # CompatJsonRpcTransport and sends the JSON-RPC method `message/send`, which our 1.x
    # handler rejects with -32601 (it speaks `SendMessage`). Advertising the 1.0 interface
    # makes the client pick JsonRpcTransport → `SendMessage`. 0.3.x consumers ignore the
    # unknown supportedInterfaces/preferredTransport/protocolVersion and read url + skills.
    async def _compat_agent_card(request: Request) -> JSONResponse:
        card = agent_card_to_dict(agent_card)
        prefix = request.url.path.split("/.well-known/")[0]  # -> /a2a/<channel>
        endpoint = f"{str(request.base_url).rstrip('/')}{prefix}"
        card.setdefault("skills", [])
        card.setdefault("url", endpoint)
        card.setdefault("protocolVersion", "1.0")
        card.setdefault("preferredTransport", "JSONRPC")
        card.setdefault(
            "supportedInterfaces",
            [{"url": endpoint, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        )
        return JSONResponse(card)

    agent_card_routes = [
        Route(p, _compat_agent_card, methods=["GET"])
        for p in ("/.well-known/agent-card.json", "/.well-known/agent.json")
    ]
    jsonrpc_routes = create_jsonrpc_routes(request_handler, rpc_url="/")
    rest_routes = create_rest_routes(request_handler)

    add_a2a_routes_to_fastapi(
        sub_app,
        agent_card_routes=agent_card_routes,
        jsonrpc_routes=jsonrpc_routes,
        rest_routes=rest_routes,
    )

    return sub_app

# SPDX-License-Identifier: Apache-2.0
"""Harness-side invocation runner backed by the mini-harness ExecutionClient."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

import structlog

from ach_agent.boot.completions import CompletionRegistry
from ach_agent.boot.conversations import ConversationLocks
from ach_agent.boot.execution_client import WorkspaceOperationFailed
from ach_agent.boot.prepare import PrepareFailed, run_prepare, run_webhook_script
from ach_agent.boot.prompt import (
    build_engine_prompt,
    build_output_instructions,
    terminal_action_for,
)
from ach_agent.boot.tooling import log_engine_tool, make_tool_recorder
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import (
    AchMemoryMemory,
    ChannelConfig,
    CodememMemory,
    Memory,
)
from ach_agent.engine import trace
from ach_agent.engine.cost import CostAccountant
from ach_agent.engine.metrics import ENGINE_LAUNCH_FAILURES
from ach_agent.execution.wire import PublicEngineConfig
from ach_agent.memory.ach_memory import prepare_ach_memory
from ach_agent.stats.sink import StatsSink
from ach_agent.templating import build_template_context, render_template

if TYPE_CHECKING:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.boot.private_prepare import PrivateCleanupRegistry

log = structlog.get_logger(__name__)


async def select_memory_wiring_async(
    memory_cfg: Memory | None,
    facade_url: str | None,
    memory_project: str = "",
    memory_auth_headers: dict[str, str] | None = None,
    memory_endpoint: str = "",
) -> tuple[dict[str, str], str]:
    """Load standing memory context and return only the public facade wiring."""
    if not isinstance(memory_cfg, AchMemoryMemory):
        return {}, ""
    mem_available, memory_prompt = await prepare_ach_memory(
        memory_endpoint, memory_project, memory_auth_headers or {}
    )
    mcp_servers = {"memory": facade_url} if (mem_available and facade_url) else {}
    return mcp_servers, memory_prompt


def make_engine_runner(
    client: ExecutionClient,
    engine_cfg: PublicEngineConfig,
    max_invocation_seconds: float,
    terminal_output_retries: int = 1,
    max_tool_calls: int = 0,
    memory_cfg: Memory | None = None,
    channel_ttl: dict[str, float] | None = None,
    channels_by_name: dict[str, ChannelConfig] | None = None,
    agent_name: str = "",
    memory_bank: str = "",
    memory_project: str = "",
    memory_auth_headers: dict[str, str] | None = None,
    memory_endpoint: str = "",
    stats_sink: StatsSink | None = None,
    tool_sink: StatsSink | None = None,
    memory_facade_url: str | None = None,
    a2a_facade_url: str | None = None,
    accountant: CostAccountant | None = None,
    cost_source: str = "engine",
    completion_registry: CompletionRegistry | None = None,
    conversation_locks: ConversationLocks | None = None,
    private_cleanup_registry: PrivateCleanupRegistry | None = None,
) -> Callable[..., Any]:
    """Build the router runner using one concrete ExecutionClient."""
    from ach_agent.boot.paths import private_scratch_dir
    from ach_agent.boot.private_prepare import (
        PrivateCleanupRegistry,
    )
    from ach_agent.engine.base.terminal import run_contract_turn
    from ach_agent.engine.workspace import workspace_dir
    from ach_agent.execution.wire import (
        ReleaseRequest,
        SessionOperation,
        WorkspacePrepareRequest,
    )
    from ach_agent.stats.sink import build_session_stat

    ttl_by_channel = channel_ttl or {}
    channels_by_name = channels_by_name or {}
    if conversation_locks is None:
        conversation_locks = ConversationLocks()
    if not isinstance(engine_cfg, PublicEngineConfig):
        raise TypeError("make_engine_runner requires credential-free PublicEngineConfig")
    cleanup_registry = private_cleanup_registry or PrivateCleanupRegistry()
    cleanup_pump: asyncio.Task[None] | None = None

    async def cleanup_events() -> None:
        while True:
            event = await client.next_controller_event()
            await cleanup_registry.handle_event(event, client.ack_workspace_cleanup)

    async def ensure_cleanup_pump() -> None:
        nonlocal cleanup_pump
        if cleanup_pump is None:
            cleanup_pump = asyncio.create_task(cleanup_events())

    async def close_runner() -> None:
        nonlocal cleanup_pump
        if cleanup_pump is not None:
            cleanup_pump.cancel()
            await asyncio.gather(cleanup_pump, return_exceptions=True)
            cleanup_pump = None
        await cleanup_registry.close()

    async def engine_runner(
        event: MessageEvent, on_kill: Callable[[], None]
    ) -> dict[str, object] | None:
        del on_kill
        deadline = asyncio.get_running_loop().time() + max_invocation_seconds
        ref = completion_registry.ref_for(event) if completion_registry is not None else None
        invocation_id = uuid4().hex
        if completion_registry is not None and ref is not None:
            completion = completion_registry.lookup(ref)
            if completion.invocation_id:
                invocation_id = completion.invocation_id
            await completion_registry.mark_running(ref)

        ch_cfg: ChannelConfig | None = channels_by_name.get(event.channel_name)
        if ch_cfg is not None and ch_cfg.type == "webhook-script":
            assert ch_cfg.script is not None
            await run_webhook_script(ch_cfg.script, event, engine_cfg.work_dir)
            if completion_registry is not None and ref is not None:
                await completion_registry.finish(ref, {"status": "completed"})
            return {"status": "completed"}

        ctx = build_template_context(
            event.payload,
            channel_name=event.channel_name,
            channel_type=ch_cfg.type if ch_cfg is not None else "",
            channel_source=(ch_cfg.source if ch_cfg is not None else None) or "",
            agent_name=agent_name,
            memory_bank=memory_bank,
            event_id=event.idempotency_key,
            session_key=event.session_key,
        )
        mcp_servers, memory_prompt = await select_memory_wiring_async(
            memory_cfg, memory_facade_url, memory_project, memory_auth_headers, memory_endpoint
        )
        if a2a_facade_url:
            mcp_servers = {**mcp_servers, "a2a": a2a_facade_url}
        invocation_engine_cfg = engine_cfg.model_copy(update={"mcp_servers": mcp_servers})
        if isinstance(memory_cfg, CodememMemory) and "{{" in memory_cfg.codemem.project:
            invocation_engine_cfg = invocation_engine_cfg.model_copy(
                update={"codemem_project": render_template(memory_cfg.codemem.project, ctx)}
            )

        session_cfg = ch_cfg.session if ch_cfg is not None else None
        conv_key = event.session_key
        reuse = session_cfg is None or session_cfg.type != "none"
        if session_cfg is not None and session_cfg.type == "custom":
            rendered = render_template(session_cfg.key or "", ctx).strip()
            if rendered:
                conv_key = rendered
            else:
                log.warning("session: template rendered empty — falling back to none")
                reuse = False
        lock_context = conversation_locks.hold(
            getattr(invocation_engine_cfg, "engine_type", "opencode"), conv_key if reuse else None
        )
        lock_entered = False
        handle: Any = None
        reservation_active = False
        invocation_failed = False
        private_registered = False

        def remaining() -> float:
            value = deadline - asyncio.get_running_loop().time()
            if value <= 0:
                raise TimeoutError("invocation deadline expired")
            return value

        try:
            async with asyncio.timeout_at(deadline):
                await lock_context.__aenter__()
                lock_entered = True
                prepare_cfg = getattr(ch_cfg, "prepare", None) if ch_cfg is not None else None
                cleanup_cfg = getattr(ch_cfg, "cleanup", None) if ch_cfg is not None else None
                workspace = Path(invocation_engine_cfg.work_dir)
                expected_workspace = workspace_dir(
                    invocation_engine_cfg.work_dir, event.session_key
                )
                if cleanup_cfg is not None:
                    await ensure_cleanup_pump()
                    await cleanup_registry.register(
                        invocation_id,
                        event,
                        expected_workspace,
                        private_scratch_dir(),
                        cleanup_cfg,
                    )
                    private_registered = True
                if prepare_cfg is not None or cleanup_cfg is not None:
                    prep_request = WorkspacePrepareRequest(
                        controller_id=client.controller_id,
                        invocation_id=invocation_id,
                        session_key=event.session_key,
                        event_id=event.idempotency_key,
                        home=str(invocation_engine_cfg.home),
                        work_dir=str(invocation_engine_cfg.work_dir),
                        notify_on_stop=cleanup_cfg is not None,
                        cleanup_ack_required=cleanup_cfg is not None,
                        cleanup_timeout_seconds=float(
                            cleanup_cfg.timeout_seconds if cleanup_cfg is not None else 120
                        ),
                        remaining_seconds=remaining(),
                    )
                    try:
                        result = await client.prepare_workspace(prep_request)
                    except WorkspaceOperationFailed as exc:
                        if private_registered and exc.confirmed and not reservation_active:
                            cleanup_registry.retire(invocation_id)
                            private_registered = False
                        raise
                    # ExecutionClient validates this deterministic path. Keep the path used
                    # by private preparation derived from harness inputs, never engine data.
                    if result.get("workspace") != str(expected_workspace):
                        raise RuntimeError("execution returned an unexpected workspace path")
                    workspace = expected_workspace
                    reservation_active = True
                    if prepare_cfg is not None:
                        await run_prepare(prepare_cfg, event, workspace)
                    if private_registered:
                        # Keep the context until the held-controller stop event is
                        # acknowledged; commit retires only superseded same-lane contexts.
                        cleanup_registry.commit(invocation_id)

                wire_cfg = invocation_engine_cfg.model_copy(update={"work_dir": str(workspace)})
                from ach_agent.execution.wire import AcquireRequest

                handle = await client.acquire(
                    AcquireRequest(
                        controller_id=client.controller_id,
                        invocation_id=invocation_id,
                        lane_key=event.session_key,
                        conversation_key=conv_key,
                        reuse=reuse,
                        remaining_seconds=remaining(),
                        config=wire_cfg,
                    )
                )
                trace.begin(
                    handle.proxy_route,
                    agent_name,
                    event.channel_name,
                    event.idempotency_key,
                )
                if accountant is not None:
                    accountant.adopt_token(handle.proxy_route)
                    accountant.begin_turn(handle.proxy_route)
                base_prompt = build_engine_prompt(
                    event, channel_cfg=ch_cfg, agent_name=agent_name, memory_bank=memory_bank
                )
                full_prompt = f"{base_prompt}\n\n{memory_prompt}" if memory_prompt else base_prompt
                free_form = event.free_form
                terminal_action = terminal_action_for(ch_cfg, free_form)
                output_instructions = build_output_instructions(ch_cfg, free_form)
                if output_instructions:
                    full_prompt = f"{full_prompt}\n\n{output_instructions}"
                on_text = None
                on_tool = None
                if completion_registry is not None and ref is not None:
                    on_text, on_tool = completion_registry.sinks(ref)
                if on_tool is None:
                    on_tool = log_engine_tool
                if tool_sink is not None:
                    on_tool = make_tool_recorder(
                        on_tool, tool_sink, event, invocation_engine_cfg.model
                    )
                log.info(
                    "engine: prompt",
                    channel=event.channel_name,
                    session_key=event.session_key,
                    prompt=full_prompt,
                )
                turn_stats: dict[str, Any] = {}
                obj = await run_contract_turn(
                    client.turn_callable(handle),
                    prompt=full_prompt,
                    free_form=free_form,
                    terminal_action=terminal_action,
                    terminal_retries=terminal_output_retries,
                    on_text=on_text,
                    on_tool=on_tool,
                    max_tool_calls=max_tool_calls,
                    stats=turn_stats,
                )
                text = str(obj.get("text", ""))
                log.info(
                    "engine: response",
                    channel=event.channel_name,
                    session_key=event.session_key,
                    action=obj.get("action"),
                    text=text,
                )
                usage = turn_stats.get("usage")
                if accountant is not None:
                    usage = accountant.end_turn(handle.proxy_route, usage)
                elif cost_source == "none" and usage is not None:
                    usage = dataclasses.replace(usage, cost=0.0)
                turn_stats["usage"] = usage
                log.info(
                    "engine: summary",
                    channel=event.channel_name,
                    session_key=event.session_key,
                    tools=turn_stats.get("tool_count", 0),
                    input_tokens=getattr(usage, "input_tokens", 0),
                    output_tokens=getattr(usage, "output_tokens", 0),
                    cost_usd=getattr(usage, "cost", 0.0),
                    duration_ms=getattr(usage, "duration_ms", 0),
                )
                session_ref = str(turn_stats.get("session_ref", ""))
                operation_names: list[Literal["discard", "compact", "forget"]]
                if session_ref and not reuse:
                    operation_names = ["discard"]
                elif (
                    session_ref
                    and session_cfg is not None
                    and session_cfg.max_tokens is not None
                    and getattr(usage, "input_tokens", 0) > session_cfg.max_tokens
                ):
                    operation_names = [
                        "compact" if session_cfg.overflow == "compact" else "discard"
                    ]
                    if session_cfg.overflow == "rotate":
                        operation_names.append("forget")
                else:
                    operation_names = []
                for operation in operation_names:
                    if operation == "compact":
                        log.info(
                            "session: maxTokens exceeded — compacting",
                            session_key=event.session_key,
                            session_ref=session_ref,
                            input_tokens=getattr(usage, "input_tokens", 0),
                            max_tokens=session_cfg.max_tokens if session_cfg is not None else None,
                        )
                    elif (
                        operation == "discard"
                        and session_cfg is not None
                        and session_cfg.overflow == "rotate"
                    ):
                        log.info(
                            "session: maxTokens exceeded — rotating",
                            session_key=event.session_key,
                            session_ref=session_ref,
                            input_tokens=getattr(usage, "input_tokens", 0),
                            max_tokens=session_cfg.max_tokens,
                        )
                    await client.session_op(
                        SessionOperation(
                            controller_id=handle.controller_id,
                            execution_id=handle.execution_id,
                            invocation_id=handle.invocation_id,
                            operation=operation,
                        )
                    )
                if stats_sink is not None:
                    stats_sink.record(
                        build_session_stat(
                            event,
                            obj,
                            turn_stats,
                            model=invocation_engine_cfg.model,
                            ts_ms=int(time.time() * 1000),
                        )
                    )
                if completion_registry is not None and ref is not None:
                    await completion_registry.finish(
                        ref, {"text": text, "action": obj.get("action")}
                    )
                return {"text": text, "action": obj.get("action")}
        except asyncio.CancelledError:
            invocation_failed = True
            if completion_registry is not None and ref is not None:
                await completion_registry.finish(
                    ref, error=f"invocation timed out after {max_invocation_seconds}s"
                )
            raise
        except TimeoutError:
            invocation_failed = True
            if completion_registry is not None and ref is not None:
                await completion_registry.finish(
                    ref, error=f"invocation timed out after {max_invocation_seconds}s"
                )
            raise
        except Exception as exc:
            invocation_failed = True
            if handle is None:
                if isinstance(exc, (PrepareFailed, WorkspaceOperationFailed)):
                    log.warning(
                        "workspace: preparation failed",
                        session_key=event.session_key,
                        error=str(exc),
                    )
                else:
                    ENGINE_LAUNCH_FAILURES.inc()
                    log.warning(
                        "engine: launch failed", session_key=event.session_key, error=str(exc)
                    )
            if completion_registry is not None and ref is not None:
                await completion_registry.finish(ref, error=f"engine failure: {exc}")
            raise
        finally:
            try:
                if handle is not None:
                    trace.end(handle.proxy_route)
                    if accountant is not None:
                        accountant.discard_turn(handle.proxy_route)
                    try:
                        try:
                            if invocation_failed:
                                await client.cancel_handle(handle)
                            else:
                                ttl = ttl_by_channel.get(event.channel_name, 0.0)
                                await client.release(
                                    ReleaseRequest(
                                        controller_id=handle.controller_id,
                                        execution_id=handle.execution_id,
                                        invocation_id=handle.invocation_id,
                                        idle_ttl_seconds=ttl,
                                    )
                                )
                        except BaseException as cleanup_error:
                            if not invocation_failed:
                                raise
                            # Preserve the original terminal/error result. The concrete
                            # client marks itself failed when cancellation is uncertain,
                            # so later invocations fail closed until the service is rebuilt.
                            log.error(
                                "engine: invocation cleanup failed",
                                invocation_id=handle.invocation_id,
                                error=str(cleanup_error),
                            )
                    finally:
                        if accountant is not None:
                            accountant.drop_token(handle.proxy_route)
                elif reservation_active:
                    try:
                        await client.cancel(client.controller_id, invocation_id)
                    except BaseException as cleanup_error:
                        if not invocation_failed:
                            raise
                        log.error(
                            "engine: workspace cancellation failed",
                            invocation_id=invocation_id,
                            error=str(cleanup_error),
                        )
            finally:
                if lock_entered:
                    await lock_context.__aexit__(None, None, None)

    setattr(engine_runner, "close", close_runner)
    return engine_runner

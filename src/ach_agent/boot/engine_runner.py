# SPDX-License-Identifier: Apache-2.0
"""The engine runner: the hot-path closure the router's lane drives for every invocation.

`make_engine_runner` builds the callable injected into the Router as `engine_runner`:
acquire an engine from the keyed pool, build the per-invocation config, run the turn,
record stats, and resolve the reply future / a2a completion callback / async no-op.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.boot.prepare import PrepareFailed, prepare_workspace, run_prepare
from ach_agent.boot.prompt import (
    build_engine_prompt,
    build_output_instructions,
    terminal_action_for,
)
from ach_agent.boot.tooling import log_engine_tool, make_tool_recorder
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig, CodememMemory, HindsightMemory, Memory
from ach_agent.engine import trace
from ach_agent.engine.cost import CostAccountant
from ach_agent.engine.metrics import ENGINE_LAUNCH_FAILURES
from ach_agent.memory.hindsight import prepare_memory
from ach_agent.stats.sink import StatsSink
from ach_agent.templating import build_template_context, render_template

if TYPE_CHECKING:
    from ach_agent.engine.base.driver import EngineConfig, EngineDriver
    from ach_agent.engine.base.pool import EnginePool

log = structlog.get_logger(__name__)


async def select_memory_wiring_async(
    memory_cfg: Memory | None,
    facade_url: str | None,
) -> tuple[list[str], str]:
    """Probe memory + build the prompt section; return the FACADE url (not the raw endpoint).

    The agent only ever reaches Hindsight through the harness facade, so the mcp_servers list
    carries the facade URL. Gated by prepare_memory's probe (D-02 fail-open) AND by the facade
    actually being up. codemem is NOT handled here — it is static per-agent and resolved once
    at boot (resolve_codemem_wiring → engine_cfg).
    """
    if not isinstance(memory_cfg, HindsightMemory):
        return [], ""

    mem_available, memory_prompt = await prepare_memory(memory_cfg)
    mcp_servers = [facade_url] if (mem_available and facade_url) else []
    return mcp_servers, memory_prompt


def make_engine_runner(
    pool: EnginePool,
    driver: EngineDriver,
    engine_cfg: EngineConfig,
    max_invocation_seconds: int,
    terminal_output_retries: int = 1,
    max_tool_calls: int = 0,
    memory_cfg: Memory | None = None,
    channel_ttl: dict[str, float] | None = None,
    channels_by_name: dict[str, ChannelConfig] | None = None,
    agent_name: str = "",
    memory_bank: str = "",
    stats_sink: StatsSink | None = None,
    tool_sink: StatsSink | None = None,
    memory_facade_url: str | None = None,
    repo_facade_url: str | None = None,
    a2a_facade_url: str | None = None,
    accountant: CostAccountant | None = None,
    cost_source: str = "engine",
) -> Callable[..., Any]:
    """Build the engine_runner callable injected into the Router.

    The runner is called by Lane as: engine_runner(event, on_kill).
    It acquires a ManagedServer from the pool, calls run_contract_turn(driver, ...)
    (which returns the single terminal object), then relays the terminal `text`:

    - reply mode (event.reply_future is not None):
        set_result(text) on the future. The route is awaiting this future to return
        200 + body to the client.
        CRITICAL: the future MUST always be resolved (set_result or set_exception)
        even on error, otherwise the route hangs indefinitely. A try/except sets the
        exception on error before re-raising.

    - on_complete mode (event.delivery_context['on_complete'] present, e.g. a2a):
        call on_complete(session_key, text) — the channel wiring relays the reply.

    - async mode (neither): nothing to deliver. Egress already happened via the
        agent's external MCP tool calls — the harness never posts on the model's behalf.

    The subprocess launch env is built by build_opencode_env (SEC-01): the
    engine_cfg carries paths, never ek_ values.

    memory_cfg (MEM-01/MEM-02/D-02): optional MemoryBlock from config.memory.
    When present, prepare_memory is called BEFORE pool.acquire so the opencode.json
    written for that server includes or excludes the memory MCP server (Pitfall 3).
    Fail-open: unreachable backend → exclude MCP server, log WARN + metric, run anyway.

    channel_ttl: {channel_name: idle_ttl_seconds} — the wait after a conversation ends
    before the opencode server is stopped, built at boot from engine.idle_ttl_seconds.
    Unknown channels (e.g. the --tui console) default to 0 = stop immediately. --tui pins a
    held ref so 0 never actually stops it mid-session (see the console-mode pre-warm).
    """
    from ach_agent.engine.base.terminal import run_contract_turn
    from ach_agent.engine.events import InvocationTimeout
    from ach_agent.stats.sink import build_session_stat

    ttl_by_channel = channel_ttl or {}
    channels_by_name = channels_by_name or {}

    async def engine_runner(event: MessageEvent, on_kill: Callable[[], None]) -> None:
        # Resolve channel cfg early so ctx can be built before the memory probe.
        ch_cfg: ChannelConfig | None = channels_by_name.get(event.channel_name)
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

        # MEM-01/MEM-02/D-02: probe memory backend BEFORE pool.acquire (Pitfall 3).
        # prepare_memory never raises (fail-open contract).
        # When unavailable: MEMORY_DEGRADED incremented + WARN logged inside prepare_memory.
        # bank is static (T-04-03, schema-enforced: no {{ }}) — the mental-model fetch, the
        # boot-started facade, and the prompt's {{ memory.bank }} all use the SAME value, so
        # there is no per-event bank rendering to keep them in sync.
        mcp_servers, memory_prompt = await select_memory_wiring_async(memory_cfg, memory_facade_url)
        # The repo-checkout facade (if enabled) is a static localhost MCP server; append it to
        # every invocation alongside the (dynamic) memory facade — the agent reaches gitlab-mcp's
        # archive resource ONLY through it (ek injected harness-side).
        if repo_facade_url:
            mcp_servers = [*mcp_servers, repo_facade_url]
        # a2a egress facade (SP1 §6): a static localhost MCP server carried on every invocation,
        # so the agent can call peer agents. Same wiring as the memory/repo facades.
        if a2a_facade_url:
            mcp_servers = [*mcp_servers, a2a_facade_url]

        # Build per-invocation engine config with the (dynamic) hindsight MCP server iff
        # reachable (D-02). codemem fields are static per-agent and already on engine_cfg from
        # boot — dataclasses.replace preserves them. Original engine_cfg is not mutated.
        import dataclasses

        if dataclasses.is_dataclass(engine_cfg) and not isinstance(engine_cfg, type):
            invocation_engine_cfg = dataclasses.replace(engine_cfg, mcp_servers=mcp_servers)
        else:
            # Non-dataclass (e.g. MagicMock in tests) — attach attribute directly.
            invocation_engine_cfg = engine_cfg
            invocation_engine_cfg.mcp_servers = mcp_servers

        # Project (codemem) — render after mcp_servers replace, before acquire. Keyed pool reuses
        # one agente per session_key, so codemem_project is fixed by the first event — correct
        # for a session-invariant template.
        if (
            isinstance(memory_cfg, CodememMemory)
            and "{{" in memory_cfg.codemem.project
            and dataclasses.is_dataclass(engine_cfg)
            and not isinstance(engine_cfg, type)
        ):
            rendered_project = render_template(memory_cfg.codemem.project, ctx)
            invocation_engine_cfg = dataclasses.replace(
                invocation_engine_cfg, codemem_project=rendered_project
            )

        # CR-01: in reply mode the future MUST always be resolved (set_result or
        # set_exception), otherwise the awaiting route hangs forever. The except branches
        # below resolve it on every failure path.
        future = event.reply_future
        # on_fail (a2a) MUST be signalled on every failure path too — otherwise the a2a
        # executor's completion.wait() (no timeout) hangs forever. Read it here so the
        # success branch AND the except branches below all resolve it.
        on_fail = event.delivery_context.get("on_fail")
        server = None
        timed_out = False
        acquired = False
        try:
            # channel.prepare: build this session's workspace on the LANE — after dedup and
            # backpressure admitted the event, before the agente exists. Its cwd becomes the
            # engine's cwd, so it must be ready (and fixed) before acquire. Fail-CLOSED: a
            # PrepareFailed takes the except path below and nothing is posted.
            # getattr: tests inject a SimpleNamespace channel cfg, as elsewhere in this runner.
            prepare_cfg = getattr(ch_cfg, "prepare", None) if ch_cfg is not None else None
            if prepare_cfg is not None:
                workspace = prepare_workspace(
                    engine_cfg.home, engine_cfg.work_dir, event.session_key
                )
                await run_prepare(prepare_cfg, event, workspace)
                if dataclasses.is_dataclass(invocation_engine_cfg) and not isinstance(
                    invocation_engine_cfg, type
                ):
                    invocation_engine_cfg = dataclasses.replace(
                        invocation_engine_cfg, work_dir=str(workspace)
                    )
            server = await pool.acquire(event.session_key, invocation_engine_cfg)
            acquired = True
            # Correlation for this invocation: every model call the engine makes
            # from here on carries the same traceparent (one Langfuse trace) and
            # the server's session id (Langfuse sessionId). Unconditional —
            # unlike cost accounting, this does not depend on cost.source.
            trace.begin(server.proxy_token, agent_name, event.channel_name, event.idempotency_key)
            if accountant is not None:
                accountant.begin_turn(server.proxy_token)
            # MEM-01: append ## Memory section (summaries or unavailable note) to prompt.
            base_prompt = build_engine_prompt(
                event,
                channel_cfg=ch_cfg,
                agent_name=agent_name,
                memory_bank=memory_bank,
                # Advertise checkout_repo only when the facade is actually wired (started),
                # not merely config-enabled — else we'd hint a tool the agent can't call.
                repo_checkout_enabled=repo_facade_url is not None,
            )
            full_prompt = f"{base_prompt}\n\n{memory_prompt}" if memory_prompt else base_prompt
            # Free-form channels (--tui console) carry no terminal contract: return
            # the raw reply, no terminal extraction/repair (delivery_context marker).
            free_form = bool(event.delivery_context.get("free_form"))
            # Harness-owned terminal-contract directive, per channel class. Appended LAST
            # (after the message + any memory block) so it wins on recency; tui gets none.
            # The same action drives the lifecycle repair/wrap turns (terminal_action below)
            # so an a2a repair turn never re-exposes 'none'.
            _terminal_action = terminal_action_for(ch_cfg, free_form)
            _output_instructions = build_output_instructions(ch_cfg, free_form)
            if _output_instructions:
                full_prompt = f"{full_prompt}\n\n{_output_instructions}"
            # Optional live-text sink (the --debug console sets this to stream the reply
            # as it's produced, so a slow trailing tool call doesn't hide the text).
            on_text = event.delivery_context.get("on_text")
            # Optional tool-lifecycle sink (the --debug console shows "⚙ running <tool>"
            # so a long-blocking tool call isn't dead air).
            on_tool = event.delivery_context.get("on_tool")
            # Default observability sink: channels wire no on_tool (only --debug does), so
            # without this a channel turn shows nothing about the tools it ran.
            if on_tool is None:
                on_tool = log_engine_tool
            # Tier 1 agent trace: record one ToolStat per tool call (metrics always; ach:tools
            # stream when ACH_STATS_REDIS_URL is set). Wraps whatever on_tool renders/logs.
            if tool_sink is not None:
                on_tool = make_tool_recorder(on_tool, tool_sink, event, engine_cfg.model)
            # Conversation identity (session block). The router lane key
            # (event.session_key) is NOT affected — only which opencode session
            # this turn reuses. No ch_cfg (--tui console) → auto: REPL continuity.
            session_cfg = ch_cfg.session if ch_cfg is not None else None
            conv_key = event.session_key
            if session_cfg is None or session_cfg.type == "auto":
                reuse = True
            elif session_cfg.type == "none":
                reuse = False
            else:  # custom: render the key template per event (validator guarantees key set)
                tmpl = session_cfg.key or ""
                rendered = render_template(tmpl, ctx).strip()
                if rendered:
                    conv_key, reuse = rendered, True
                else:
                    log.warning(
                        "session: template rendered empty — falling back to none",
                        channel=event.channel_name,
                        template=tmpl,
                    )
                    reuse = False
            log.info(
                "engine: prompt",
                channel=event.channel_name,
                session_key=event.session_key,
                prompt=full_prompt,
            )
            turn_stats: dict[str, Any] = {}
            obj = await run_contract_turn(
                driver,
                server,
                conv_key=conv_key,
                prompt=full_prompt,
                reuse=reuse,
                sessions=pool.sessions,
                free_form=free_form,
                terminal_action=_terminal_action,
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
            _usage = turn_stats.get("usage")
            if accountant is not None:
                _usage = accountant.end_turn(server.proxy_token, _usage)
            elif cost_source == "none" and _usage is not None:
                _usage = dataclasses.replace(_usage, cost=0.0)
            turn_stats["usage"] = _usage
            log.info(
                "engine: summary",
                channel=event.channel_name,
                session_key=event.session_key,
                tools=turn_stats.get("tool_count", 0),
                input_tokens=getattr(_usage, "input_tokens", 0),
                output_tokens=getattr(_usage, "output_tokens", 0),
                cost_usd=getattr(_usage, "cost", 0.0),
                duration_ms=getattr(_usage, "duration_ms", 0),
            )
            # Post-turn session hygiene. Skipped on timeout (this code is not reached
            # when the lane cancels the turn) — that orphan is accepted, the
            # server is force-killed anyway.
            _sid = turn_stats.get("session_ref", "")
            if _sid and not reuse:
                # key='none' (or empty template render): stateless turn leaves no residue.
                await driver.discard_session(server, _sid)
            elif (
                _sid
                and session_cfg is not None
                and session_cfg.max_tokens is not None
                and getattr(_usage, "input_tokens", 0) > session_cfg.max_tokens
            ):
                if session_cfg.overflow == "compact":
                    log.info(
                        "session: maxTokens exceeded — compacting",
                        session_key=event.session_key,
                        session_ref=_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    await driver.compact_session(server, _sid)
                else:  # rotate: drop the map entry + delete the old session (clean)
                    log.info(
                        "session: maxTokens exceeded — rotating",
                        session_key=event.session_key,
                        session_ref=_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    pool.sessions.pop(conv_key, None)
                    await driver.discard_session(server, _sid)
            if stats_sink is not None:
                stats_sink.record(
                    build_session_stat(
                        event,
                        obj,
                        turn_stats,
                        model=engine_cfg.model,
                        ts_ms=int(time.time() * 1000),
                    )
                )

            if future is not None:
                # Reply mode: resolve the future the route is awaiting.
                if not future.done():
                    future.set_result(text)
                return

            # A2A completion path (W9 — engine_runner does NOT import channels.a2a):
            # The on_complete callable is injected by the A2A wiring closure in main.py
            # into event.delivery_context['on_complete'] before handler.handle() is called.
            on_complete = event.delivery_context.get("on_complete")
            if on_complete is not None or on_fail is not None:
                action = obj.get("action")
                if action == "a2a_reply" and text.strip():
                    if on_complete is not None:
                        on_complete(event.session_key, text)
                else:
                    reason = (
                        f"invalid terminal output (action={action!r}, "
                        f"empty_text={not text.strip()})"
                    )
                    if on_fail is not None:
                        on_fail(event.session_key, reason)
                return

            # Async mode: nothing to deliver. Egress already happened via the agent's
            # external MCP tool calls — the harness never posts on the model's behalf.
            return
        except asyncio.CancelledError:
            # The lane's maxInvocationSeconds deadline (or a shutdown) cancelled us.
            # Force-kill the runaway (finally releases with ttl=0) so a warm TTL is never
            # armed on a timed-out server, and release the awaiting caller so it can't hang.
            timed_out = True
            if future is not None and not future.done():
                future.set_exception(InvocationTimeout(max_invocation_seconds))
            if on_fail is not None:
                on_fail(event.session_key, f"invocation timed out after {max_invocation_seconds}s")
            raise
        except Exception as exc:
            if not acquired and not isinstance(exc, PrepareFailed):
                # pool.acquire itself failed — the agente could not be launched for
                # this session_key. Explicit metric + WARN (no silent drop): acceptance
                # is decoupled from engine readiness, so this is where a launch failure
                # first surfaces. Never log ek_/tokens — session_key + error string only.
                ENGINE_LAUNCH_FAILURES.inc()
                log.warning(
                    "engine: launch failed (pool.acquire)",
                    session_key=event.session_key,
                    task_id=event.task_id,
                    error=str(exc),
                )
            if isinstance(exc, PrepareFailed):
                # Its own metric is already counted (with a reason label) in run_prepare.
                log.warning(
                    "prepare: workspace hook failed — invocation abandoned",
                    session_key=event.session_key,
                    task_id=event.task_id,
                    error=str(exc),
                )
            if future is not None and not future.done():
                future.set_exception(exc)
            if on_fail is not None:
                on_fail(event.session_key, f"engine failure: {exc}")
            raise
        finally:
            # Return the engine server to the pool. Slot release is owned by the lane:
            # its `async with` blocks free the semaphores and its finally calls on_kill
            # for queued_total. A timed-out invocation ALWAYS releases with ttl=0 (force
            # kill of the runaway); otherwise the channel's warm idle TTL is applied so
            # session:auto persists the server across events. `if server is not None`
            # guards a cancel during a cold-start acquire.
            if server is not None:
                ttl = 0.0 if timed_out else ttl_by_channel.get(event.channel_name, 0.0)
                try:
                    # Close the correlation window with the cost turn: a warm
                    # pooled server must not stamp this invocation's traceparent
                    # on whatever the engine does between turns.
                    trace.end(server.proxy_token)
                    if accountant is not None:
                        accountant.discard_turn(server.proxy_token)
                    await pool.release(event.session_key, ttl_seconds=ttl)
                except Exception as exc:  # noqa: BLE001
                    log.warning("pool release error", task_id=event.task_id, error=str(exc))

    return engine_runner

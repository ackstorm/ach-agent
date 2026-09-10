# SPDX-License-Identifier: Apache-2.0
"""ach-agent entrypoint — bootstrap wiring.

Boot order (CRITICAL — Pitfall 8: configure_logging FIRST, before any import
that may emit a log line):
  1. configure_logging()        <- SEC-01: redact_ek_processor installed first
  2. load_config(path)          <- hard-fail on schema mismatch (CFG-02)
  3. D-02 gate: reject unwired channel types (hard-fail, non-zero exit)
  4. Write PID file             <- Pitfall 11: single-replica guard
  5. Construct Router
  6b. Build engine_runner (CR-01: branches on event.reply_future for reply mode;
      relays the terminal text — egress is the agent's via external MCP tools)
  6c. Create FastAPI app via create_app(channels, router)
  7. asyncio.run(main()) — starts uvicorn + cron tasks on the SAME event loop

RTR-06: router must not import from hermes_agent.*; engine injected as callable.
D-08: deliver.type: reply → event.reply_future resolved by engine_runner on the lane,
      awaited by the route (CR-01: exactly one engine execution per event).
      async channels → engine_runner relays nothing; the agent already acted via MCP.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn

if TYPE_CHECKING:
    from ach_agent.engine.base.driver import EngineDriver
    from ach_agent.engine.hydrate import McpServer

from ach_agent.boot.engine_runner import make_engine_runner
from ach_agent.boot.health import HealthState
from ach_agent.boot.paths import (
    harness_log_dir,
    link_ach_state,
    resolve_engine_paths,
    write_pid_file,
)
from ach_agent.boot.prompt import resolve_system_prompt
from ach_agent.boot.secrets import (
    collect_secret_env_names,
    resolve_model_upstream,
    strip_forwarded_secrets,
)
from ach_agent.boot.stores import open_dedup_store, open_session_store
from ach_agent.channels.a2a import A2AAgentExecutorBridge, build_a2a_app, make_a2a_agent_card
from ach_agent.channels.cron import CronScheduler
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.queue import QueueConsumer
from ach_agent.channels.tui import run_one_shot, run_tui_console
from ach_agent.config import load_config
from ach_agent.config.schema import (
    AchMemoryMemory,
    CodememMemory,
    LocalMcpServer,
    McpServerConfig,
    RemoteMcpServer,
    RepoCheckoutParams,
    RepoCheckoutServer,
)
from ach_agent.engine import trace
from ach_agent.engine.context import fetch_context
from ach_agent.engine.cost import (
    CostAccountant,
    PriceTable,
    report_price_load_result,
    validate_cost_source,
)
from ach_agent.engine.hydrate import hydrate, resolve_model
from ach_agent.engine.mcp_passthrough import to_engine_entry
from ach_agent.engine.mcp_proxy import McpProxy, start_model_proxy, stop_model_proxies
from ach_agent.engine.metrics import DRAIN_COMPLETED
from ach_agent.engine.sanitized_env import add_secret_redaction, configure_logging
from ach_agent.http.app import create_app
from ach_agent.memory.ach_memory_facade import AchMemoryFacade
from ach_agent.router import Router
from ach_agent.security.preflight import run_preflight
from ach_agent.templating import build_template_context, render_template

# configure_logging() is called at module TOP (not in main()) so that any
# log emission during import (e.g. validation warnings) is already redacted.
# Must be the FIRST executable statement (Pitfall 8 / SEC-01).
configure_logging()

log = structlog.get_logger(__name__)

# D-02: only channel types wired in this build
WIRED_CHANNEL_TYPES: frozenset[str] = frozenset(
    {"cron", "webhook", "webhook-script", "a2a", "queue"}
)

# model.type → ACH compat-endpoint path prefix fronted by the model proxy. opencode's
# provider baseURL becomes "http://127.0.0.1:<port>/<prefix>". Each type hits its NATIVE wire:
# openai → /v1 (chat/completions), gemini → /gemini/v1beta (generateContent), anthropic →
# /anthropic (messages). The type is authoritative — the harness does NOT round-trip a gemini
# model through the OpenAI wire (that leaks gemini thought-signatures into tool_call ids).
_MODEL_ENDPOINT_PREFIX: dict[str, str] = {
    "openai": "v1",
    "gemini": "gemini/v1beta",
    "anthropic": "anthropic",
}

CONFIG_PATH_ENV = "ACH_CONFIG_PATH"
DEFAULT_CONFIG_PATH = "/etc/ach-agent/config.json"
PID_FILE = Path("/tmp/ach-agent.pid")


# resolve_codemem_wiring has moved to ach_agent.memory.codemem; re-exported here for
# back-compat with existing callers (tests/integration/test_codemem_wiring.py, etc.).
from ach_agent.memory.codemem import resolve_codemem_wiring as resolve_codemem_wiring  # noqa: E402


def collect_passthrough_mcp(
    mcp_servers: dict[str, McpServerConfig],
) -> dict[str, dict[str, object]]:
    """Normalize every local/remote entry to an opencode.json mcp.<name> value.

    repoCheckout entries are skipped — the harness hosts those itself (facade), they are not
    passed through to opencode.
    """
    out: dict[str, dict[str, object]] = {}
    for name, spec in mcp_servers.items():
        if isinstance(spec, (LocalMcpServer, RemoteMcpServer)):
            out[name] = to_engine_entry(spec)
    return out


def find_repo_checkout(
    mcp_servers: dict[str, McpServerConfig],
) -> tuple[str, RepoCheckoutParams] | None:
    """The (name, params) of the repoCheckout entry, or None.

    ponytail: one repoCheckout facade per agent (the only real case). If several are declared,
    take the first and WARN — supporting N facades is unneeded plumbing until asked.
    """
    found: tuple[str, RepoCheckoutParams] | None = None
    for name, spec in mcp_servers.items():
        if isinstance(spec, RepoCheckoutServer):
            if found is not None:
                log.warning("multiple repoCheckout mcpServers — using first", ignored=name)
                continue
            found = (name, spec.repo_checkout)
    return found


def resolve_repo_archive_endpoint(mcp_servers: list[McpServer], server_id: str) -> str | None:
    """The endpoint of the hydrated McpServer whose id == server_id, or None."""
    for s in mcp_servers:
        if s.id == server_id:
            return s.endpoint
    return None


async def _build_cost_accounting(
    *,
    source: str,
    wire: str,
    model_name: str,
    model_up_base: str,
    model_up_token: str,
) -> tuple[PriceTable | None, CostAccountant | None]:
    """Build optional accounting after model-proxy auth is resolved."""
    if source == "litellm_usage":
        price_table = PriceTable(model_up_base, model_up_token)
        price_failure = await price_table.load(model_name)
        report_price_load_result(price_failure, model_name)
        return price_table, CostAccountant(
            source=source,
            wire=wire,
            prices=price_table,
            model_name=model_name,
        )
    if source == "litellm_headers":
        return None, CostAccountant(
            source=source,
            wire=wire,
            prices=None,
            model_name=model_name,
        )
    return None, None


async def _drain(
    state: HealthState,
    uv_server: Any,
    cron_scheduler: CronScheduler | None,
    router: Any,
    dedup_store: Any,
) -> None:
    """D-09/D-10/D-11: graceful drain sequence on SIGTERM.

    1. Flip draining + readyz NotReady (D-09 step 2, D-12).
    2. Stop uvicorn (no new HTTP connections).
    3. Stop CronScheduler (D-06 intake-stop); cancels its single asyncio task.
    4. Drain queued + in-flight lane work (D-11):
       Lane tasks blocked on empty queue.get() are cancelled via Lane.cancel()
       (RESEARCH Pitfall 4). In-flight runs bounded by maxInvocationSeconds watchdog (D-10).
    5. Cleanup: close dedup store, inc DRAIN_COMPLETED, sys.exit(0).

    No grace-deadline timer (D-10): maxInvocationSeconds watchdog + K8s SIGKILL backstop.
    Never logs ek_/GITLAB_TOKEN (T-03-07): log emits only path/count/reason fields.
    """
    # 1. Flip draining flag + readyz NotReady (D-09, D-12 straggler gate)
    state.draining = True
    state.ready = False
    log.info("drain: readyz flipped NotReady, intake stopped")

    # 2. Signal uvicorn to stop accepting new connections
    if uv_server is not None:
        uv_server.should_exit = True

    # 3. Stop CronScheduler (D-06 intake-stop): cancels its single asyncio task cleanly.
    if cron_scheduler is not None:
        await cron_scheduler.stop()

    # 4. Drain queued + in-flight lane work (D-11)
    # Step 4a: wait for in-flight + queued events to finish processing.
    # Lane._queue.task_done() fires in Lane._consume finally after each event.
    # asyncio.Queue.join() blocks until all task_done() calls match put() calls.
    # This preserves in-flight work — we only cancel AFTER queues drain.
    #
    # Step 4b: cancel idle lane tasks (stuck on queue.get() — RESEARCH Pitfall 4).
    # No new events enter lanes after draining=True, so after join() the lanes
    # are idle; cancel() unblocks the empty queue.get() await.
    lanes_snapshot = list(router.lanes.values())
    if lanes_snapshot:
        log.info("drain: waiting for lane queues to drain", count=len(lanes_snapshot))
        # Wait for all queued + in-flight events to complete (D-11)
        await asyncio.gather(*(lane.join() for lane in lanes_snapshot), return_exceptions=True)
        # Now cancel idle lane tasks (they are blocked on empty queue.get())
        for lane in lanes_snapshot:
            lane.cancel()
        await asyncio.gather(
            *(lane.wait_closed() for lane in lanes_snapshot), return_exceptions=True
        )

    # 5. Cleanup: close dedup store, increment metric, return cleanly.
    # Do NOT sys.exit(0) here: this runs inside an asyncio task, so SystemExit
    # force-cancels the still-pending uvicorn serve task and dumps a CancelledError
    # traceback to stderr (even though the exit code is 0). Instead we return; main()
    # then awaits uvicorn's own graceful shutdown (should_exit was set above) and the
    # process exits 0 naturally with no traceback.
    if hasattr(dedup_store, "close"):
        dedup_store.close()
    DRAIN_COMPLETED.inc()
    log.info("drain: complete")


class _A2AHandler:
    """Router wrapper injecting on_complete/on_fail into delivery_context (W9 pattern).

    finding 5: the bridge's signal_completion/signal_failure are keyed by task_id,
    not the router's session_key (context_id, shared across a conversation's
    tasks) — so the closures bind THIS event's task_id and ignore the session_key
    argument engine_runner calls them with, preserving the (session_key, text)
    callback signature the engine tier is built around.
    """

    def __init__(self, rtr: Any, fn: Any, fn_fail: Any) -> None:
        self._rtr = rtr
        self._fn = fn
        self._fn_fail = fn_fail

    async def handle(self, event: MessageEvent) -> Any:
        task_id = str(event.payload["task_id"])
        event.delivery_context["on_complete"] = lambda _session_key, text: self._fn(
            task_id, text
        )
        event.delivery_context["on_fail"] = lambda _session_key, reason: self._fn_fail(
            task_id, reason
        )
        return await self._rtr.handle(event)


async def _run_opencode_attach(
    router: Any,
    *,
    binary_path: str,
    port: int,
    ephemeral_home: Path,
    config_path: Path | None = None,
) -> None:
    """`--tui`: hand the terminal to opencode's native TUI, attached to our serve.

    Shells out to `opencode attach http://127.0.0.1:<port>` — opencode's own full-screen
    client driving the SAME serve process the harness pre-warmed. Egress hygiene is
    preserved: the server still routes model + MCP traffic through the localhost proxies
    that inject the ek_ (opencode never sees it). Loopback is used even when serve binds
    0.0.0.0 — the attach client is always co-located.

    Harness logging (structlog → sys.stderr, plus the serve-drain + proxy request logs)
    would corrupt opencode's alt-screen, so sys.stderr is redirected to a file for the
    session. The subprocess inherits the real terminal fds (0/1/2) at the OS level, so
    opencode renders normally; only Python-level harness logging is diverted.

    Falls back to the plain REPL if the opencode binary is not found.
    """
    import shutil

    binary = shutil.which(binary_path)
    if not binary:
        log.error(
            "opencode binary not found for attach — falling back to plain REPL",
            binary=binary_path,
        )
        await run_tui_console(router)
        return

    url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "HOME": str(ephemeral_home), "TMPDIR": "/tmp"}
    if config_path is not None:
        env["OPENCODE_CONFIG"] = str(config_path)
    log_path = harness_log_dir() / "tui-attach.log"
    log.info("ach-agent: --tui → opencode attach", url=url, log_file=str(log_path))

    real_stderr = sys.stderr
    with open(log_path, "a", encoding="utf-8") as log_fh:
        sys.stderr = log_fh
        try:
            proc = await asyncio.create_subprocess_exec(binary, "attach", url, "--pure", env=env)
            # opencode owns the terminal and quits on Ctrl+C itself. Both processes share the
            # foreground process group, so terminal SIGINT hits us too — ignore it here (AFTER
            # spawn, so the child inherited the default disposition) or it cancels main() mid
            # proc.wait(), tears through the shutdown cleanup below, and leaves aiohttp sessions
            # unclosed. Not restored on purpose: mashing Ctrl+C during the post-attach cleanup
            # would re-cancel it. The process is exiting right after — nothing else needs SIGINT.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            await proc.wait()
        finally:
            sys.stderr = real_stderr


def _engine_runtime_fields(cfg: Any) -> dict[str, Any]:
    """engine.type/engine.pi -> executable-selection EngineConfig kwargs, plus the
    normalized model.thinking intent every engine translates for itself.

    engine.pi carries ONLY executable knobs (binaryPath/mcpAdapterPath); model identity
    and thinking/reasoning intent live in the model block (CONTRACT §2 model.thinking).
    A Pi config with no engine.pi sub-block still launches the image's `pi` binary.
    """
    thinking = {
        "thinking_enabled": cfg.model.thinking.enabled,
        "thinking_effort": cfg.model.thinking.effort,
    }
    if cfg.engine.type != "pi":
        return {"binary_path": "opencode", "pi_mcp_adapter_path": "", **thinking}
    pi = cfg.engine.pi
    if pi is None:
        return {"binary_path": "pi", "pi_mcp_adapter_path": "", **thinking}
    return {
        "binary_path": pi.binary_path,
        "pi_mcp_adapter_path": pi.mcp_adapter_path,
        **thinking,
    }


async def main(
    tui_mode: bool = False, one_shot_prompt: str | None = None, debug_mode: bool = False
) -> None:
    """Async entrypoint: load config, boot router, start channel adapters + uvicorn.

    Three launch modifiers boot the engine/proxies/hydration but IGNORE the configured
    channels (the typed/passed line IS the prompt — no terminal contract):
      - tui_mode (`--tui`): opens the selected engine's native interactive console.
      - debug_mode (`--debug`): the plain stdin/stdout REPL (see run_tui_console) — the
        minimal console, easiest to pipe/debug. Takes precedence over tui_mode.
      - one_shot_prompt (`--prompt TEXT`): run a single prompt non-interactively, print
        the reply, and exit (see run_one_shot). Highest precedence.
    """
    # SEC: harden this process (dumpable=0 + no_new_privs) and refuse an unsafe host
    # BEFORE the opencode peer is spawned and before ek_ is read into a Python local
    # below (dumpable=0 also reowns the ek_ already in /proc/self/environ). Fail-closed
    # unless ACH_INSECURE_ALLOW_DEGRADED=1. See security/preflight.py.
    run_preflight()
    console_mode = tui_mode or debug_mode or one_shot_prompt is not None
    config_path = os.environ.get(CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH)

    # Step 2: load config (hard-fail on schema mismatch — CFG-02)
    cfg = load_config(config_path)
    try:
        validate_cost_source(cfg.cost.source, cfg.model.type)
    except ValueError as exc:
        log.error(
            "cost source rejected at boot",
            cost_source=cfg.cost.source,
            model_type=cfg.model.type,
            reason=str(exc),
        )
        raise SystemExit(1) from exc
    log.info("cost: active source", cost_source=cfg.cost.source)
    # SEC: secret.env values must never reach opencode's env or the logs. Strip any
    # secret.env name a misconfig also listed in engine.forwardEnv (fail-safe, WARN not
    # hard-fail — see strip_forwarded_secrets), and register the secret names' CURRENT
    # values for generic log redaction (the hardcoded ek_/GITLAB_TOKEN processors don't
    # catch arbitrary secret.env NAMES).
    effective_forward_env = strip_forwarded_secrets(cfg)
    add_secret_redaction(collect_secret_env_names(cfg))
    engine_home, engine_work_dir = resolve_engine_paths(cfg)
    state_dir = link_ach_state(engine_home, engine_work_dir)

    # Step 3: D-02 gate — reject unwired channel types before serving.
    # Skipped under --tui/--prompt: configured channels are ignored in console mode.
    for channel in cfg.channels if not console_mode else []:
        if channel.type not in WIRED_CHANNEL_TYPES:
            log.error(
                "channel type configured but not supported in this build — exiting",
                channel_name=channel.name,
                channel_type=channel.type,
                wired_types=sorted(WIRED_CHANNEL_TYPES),
            )
            sys.exit(1)

    # Step 4: PID file (Pitfall 11 — tolerate non-writable in dev)
    write_pid_file(PID_FILE)

    # Plan 2 (CONTRACT §6.10): self-hydrate from ACH, then front the model + MCP traffic
    # via localhost reverse-proxies that inject the ek_. opencode points ONLY at localhost
    # and never sees the ek_ or the real ACH URL.
    #
    # The ek_ (ACH_TOKEN) is read here solely to pass to hydration + the proxies; it is
    # NEVER logged and NEVER written to opencode.json (the proxies hold it in a closure).
    # ACH_TOKEN is REQUIRED — opencode reaches the model only through the localhost proxy,
    # which is created during hydration. A missing ek_ is hard-failed below (no model endpoint).
    ek = os.environ.get("ACH_TOKEN")
    model_base_url: str = ""
    mcp_local_urls: dict[str, str] = {}
    mcp_proxy: McpProxy | None = None
    # Harness-hosted memory facade: the agent reaches ach-memory ONLY through this localhost
    # MCP server (scope + project injected). Started beside the proxies below; None when
    # memory is not an ach-memory config or its auth env is unset (fail-open, run without it).
    memory_facade: AchMemoryFacade | None = None
    memory_facade_url: str | None = None
    # ach-memory only: the agent's own bank, `{namespace}-{agent.name}`. Boot-static —
    # resolved once here, never from an event payload (it selects a bank).
    memory_project: str = ""
    # Outbound credential for ach-memory, resolved ONCE here where the ek_ is in scope. The
    # facade keeps it; engine_runner needs it for the per-invocation load_context.
    memory_auth_headers: dict[str, str] = {}
    # Harness-hosted repo-checkout facade: exposes `checkout_repo`, reading gitlab-mcp's archive
    # resource harness-side (ek as x-ach-key, never seen by the agent). None when disabled or the
    # gitlab endpoint/ek is missing (fail-open, run without the tool). Declared here so shutdown
    # can stop it even though it is constructed inside the `if ek:` block below.
    repo_facade: Any = None
    repo_facade_url: str | None = None
    a2a_facade: Any = None
    a2a_facade_url: str | None = None
    price_table: PriceTable | None = None
    accountant: CostAccountant | None = None
    if ek:
        manifest = await hydrate(
            cfg.capability.ach.base_url,
            ek,
            cfg.agent.name,
            cfg.capability.ach.environment,
        )
        # hard-fail (sys.exit 1) if the configured model is absent from the hydrated set.
        resolve_model(manifest, cfg.model.name)
        # capability.filter.exclude — governance gate ABOVE the model. Skills are dropped
        # from the hydrated context BEFORE fetch (never downloaded); MCP servers are excluded
        # from the localhost proxy (never fronted, so opencode never discovers them).
        _exclude = cfg.capability.filter.exclude
        _exclude_skills = set(_exclude.skills)
        if _exclude_skills:
            manifest.context.skills = [
                s for s in manifest.context.skills if s.name not in _exclude_skills
            ]
            log.info("filter: skills excluded", excluded=sorted(_exclude_skills))
        await fetch_context(
            manifest.context,
            ek,
            state_dir,
            Path(engine_home) / ".config" / "opencode" / "skills",
        )
        mcp_proxy = McpProxy()
        _exclude_servers = set(_exclude.mcp_servers)
        mcp_local_urls = await mcp_proxy.start(manifest.mcp_servers, ek, exclude=_exclude_servers)
        if _exclude_servers:
            log.info("filter: mcp servers excluded", excluded=sorted(_exclude_servers))
        # Start the memory facade beside the proxies. The agent points at THIS url, never at
        # ach-memory, and never sees the user key or the project. One bank per agent — the
        # project slug is derived from THIS agent's identity, not from any event's payload.
        if isinstance(cfg.memory, AchMemoryMemory):
            from ach_agent.memory.ach_memory import resolve_ach_memory_auth, resolve_project

            _ok, memory_auth_headers = resolve_ach_memory_auth(cfg.memory.ach_memory.auth, ek)
            if _ok:
                memory_project = resolve_project(cfg.memory.ach_memory, cfg.agent.name)
                memory_facade = AchMemoryFacade(
                    cfg.memory.ach_memory.endpoint, memory_auth_headers, memory_project
                )
                memory_facade_url = await memory_facade.start()
            else:
                log.warning("memory: auth unresolved — facade not started; running without memory")
        # Start the repo-checkout facade beside the proxies (mcpServers type=repoCheckout).
        # It fronts gitlab-mcp's archive resource with the ek_ (x-ach-key), so the agent gets a
        # local checkout without ever seeing the ek_ or the raw endpoint.
        _rc = find_repo_checkout(cfg.mcp_servers)
        if _rc is not None:
            _rc_name, _rc_params = _rc
            gl_endpoint = resolve_repo_archive_endpoint(
                manifest.mcp_servers, _rc_params.source_mcp_server_id
            )
            if gl_endpoint:
                from ach_agent.engine.repo_facade import RepoCheckoutFacade

                repo_facade = RepoCheckoutFacade(
                    gl_endpoint, ek, _rc_params.tmp_base, _rc_params.ttl_seconds
                )
                repo_facade_url = await repo_facade.start()
            else:
                log.warning(
                    "repoCheckout: source mcp server not in manifest — tool not wired",
                    source_mcp_server_id=_rc_params.source_mcp_server_id,
                )
        # Model-proxy upstream override (dev/test only — A/B a different model backend,
        # e.g. litellm direct, to isolate forwarder buffering). MODEL-ONLY: hydration + MCP
        # stay on the ACH coords above; only the model proxy's upstream + auth swap. The
        # token is injected VERBATIM as the header value (carry `Bearer ` in it if the
        # backend needs it). SECURITY: this path uses a raw provider key, NOT the ek_ — it
        # bypasses ACH governance/ek-hygiene and is for local testing, never production.
        model_up_base, model_up_header, model_up_token = resolve_model_upstream(
            ek, cfg.capability.ach.base_url
        )
        price_table, accountant = await _build_cost_accounting(
            source=cfg.cost.source,
            wire=cfg.model.type,
            model_name=cfg.model.name,
            model_up_base=model_up_base,
            model_up_token=model_up_token,
        )
        model_proxy_base = await start_model_proxy(
            model_up_base,
            model_up_token,
            model_up_header,
            accountant=accountant,
        )
        # model.type is authoritative for the wire. The hydration manifest reports every model
        # at /v1 (openai-compat) even for gemini, so we DON'T read the manifest endpoint here —
        # doing so forced a type:gemini model onto /v1/chat/completions. resolve_model above
        # still validates membership (hard-fails if the name is absent).
        prefix = _MODEL_ENDPOINT_PREFIX[cfg.model.type]
        model_base_url = f"{model_proxy_base}/{prefix}"
        _ctx = manifest.context
        log.info(
            "hydrated + localhost proxies started",
            environment=manifest.environment,
            model_count=len(manifest.models),
            models=manifest.models,
            mcp_count=len(mcp_local_urls),
            mcp_servers=list(mcp_local_urls.keys()),
            skills=[s.name for s in _ctx.skills],
            prompts=[p.name for p in _ctx.prompts],
            artifacts=[a.name for a in _ctx.artifacts],
        )
        # A2A egress (Plan 3, completed in SP1): expose peer agents as harness-hosted MCP
        # tools on a loopback FastMCP so BOTH engines discover them. The ek_ stays in the
        # harness (peer auth header via A2AAgentClient); only the loopback URL is written into
        # engine config. RTR-06: a2a-sdk imports stay function-scoped; import the builder lazily.
        if manifest.a2a_agents:
            from ach_agent.engine.a2a_egress import A2AEgressFacade, build_a2a_tools

            a2a_tools = build_a2a_tools(manifest.a2a_agents, ek=ek)
            a2a_facade = A2AEgressFacade(a2a_tools)
            a2a_facade_url = await a2a_facade.start()
            log.info(
                "a2a egress facade started",
                agent_count=len(manifest.a2a_agents),
                tool_count=len(a2a_tools),
                url=a2a_facade_url,
            )

    # ACH_TOKEN (ek_) is REQUIRED: opencode always reaches the model through the localhost
    # model-proxy, which only exists once we hydrate with the ek_. Without it there is no
    # model endpoint at all (model_base_url is set only inside the `if ek:` block above).
    if not model_base_url:
        log.error(
            "no model endpoint — set ACH_TOKEN (ek_) so the harness can hydrate and front "
            "the model via the localhost proxy. opencode points only at that proxy; there "
            "is no direct-gateway fallback."
        )
        sys.exit(1)

    # Step 5: build the engine pool. Egress is the agent's via external MCP tools —
    # the harness has no delivery adapter (it never posts on the model's behalf).
    from ach_agent.engine.base.driver import EngineConfig
    from ach_agent.engine.base.pool import EnginePool
    from ach_agent.engine.opencode.driver import OpencodeDriver

    # opencode `serve` always binds loopback (127.0.0.1) on a free ephemeral port the pool
    # picks — only reachable inside the container/host. `--tui` drives it via `opencode attach`
    # (co-located, loopback); nothing is published off-host.
    # codemem is static per-agent: resolve db_path + project once at boot (needs persistence
    # context). Fail-open ("","") when not codemem or the binary is absent (MEM-02/D-02).
    codemem_db_path, codemem_project = resolve_codemem_wiring(cfg)

    # Boot-static system prompt: persona + active backend's TOOLS_SPEC (appended once at boot).
    _persona = resolve_system_prompt(cfg.prompt, state_dir)
    from ach_agent.memory import tools_spec_for

    _spec = tools_spec_for(cfg.memory)
    _system_prompt = f"{_persona}\n\n## Memory Tools\n{_spec}" if _spec else _persona

    passthrough_mcp = collect_passthrough_mcp(cfg.mcp_servers)
    engine_cfg = EngineConfig(
        home=engine_home,
        work_dir=engine_work_dir,
        codemem_db_path=codemem_db_path,
        codemem_project=codemem_project,
        model=cfg.model.name,
        model_type=cfg.model.type,
        params=cfg.model.params,
        # prompt.system = the inline agent persona + per-backend TOOLS_SPEC (boot-static).
        system_prompt=_system_prompt,
        # prompt.compose: append (top-level instructions) | replace (agent.build.prompt).
        compose=cfg.prompt.compose if cfg.prompt else "append",
        steps=cfg.limits.max_steps,
        startup_timeout_seconds=cfg.engine.startup_timeout_seconds,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        model_base_url=model_base_url,
        mcp_local_urls=mcp_local_urls,
        # SEC-01 / ek-hygiene: opencode's env is clean-slate (base allowlist only). Extra
        # var names the operator wants forwarded come from engine.forwardEnv, with any
        # secret.env name stripped (strip_forwarded_secrets, computed above).
        forward_env=effective_forward_env,
        # capability.filter.exclude.tools — disabled in opencode.json (withheld from model).
        exclude_tools=cfg.capability.filter.exclude.tools,
        extra_mcp_servers=passthrough_mcp,
        engine_type=cfg.engine.type,
        **_engine_runtime_fields(cfg),
    )
    # D-03/D-04: dedup store first — it opens/repairs state.db (fail-closed on a bad
    # mount). Then the session map shares that now-valid file (fail-open). The pool
    # owns the session map so run_invocation reuses opencode sessions across restarts.
    dedup_store = open_dedup_store(cfg)
    session_store = open_session_store(cfg)
    if cfg.engine.type == "pi":
        from ach_agent.engine.pi.driver import PiDriver

        driver: EngineDriver = PiDriver()
    else:
        driver = OpencodeDriver()
    pool = EnginePool(driver=driver, sessions_map=session_store, accountant=accountant)

    # Best-effort stats sink (harness-local, ACH_STATS_* — never part of operator contract).
    # Unset ACH_STATS_REDIS_URL → Prometheus-only, no queue/writer.
    from ach_agent.stats import metrics as stats_metrics
    from ach_agent.stats.sink import StatsSink

    _stats_redis = os.environ.get("ACH_STATS_REDIS_URL")
    _stats_retention = int(os.environ.get("ACH_STATS_RETENTION", "3024000"))
    stats_sink = StatsSink(_stats_redis, retention_s=_stats_retention)
    await stats_sink.start()
    # Tier 1 agent trace: same writer machinery pointed at ach:tools with the per-tool metrics.
    tool_sink = StatsSink(
        _stats_redis,
        stream="ach:tools",
        on_record=stats_metrics.observe_tool,
        retention_s=_stats_retention,
    )
    await tool_sink.start()

    # 6b. Build engine_runner. MEM-01/D-02: pass memory_cfg so engine_runner probes
    # before pool.acquire (Pitfall 3).
    # engine.idle_ttl_seconds (default 60) keeps a keyed server warm after its last release
    # so channel.session=auto persists the opencode session across events for the same
    # session_key. Applied to every configured channel; an unknown channel_name still
    # defaults to 0 at the release site (engine_runner). --tui is NOT a channel — it pins a
    # held ref for the whole REPL, so this TTL never stops it mid-session.
    channel_ttl = {ch.name: cfg.engine.idle_ttl_seconds for ch in cfg.channels}
    channels_by_name = {c.name: c for c in cfg.channels}
    # `{{ memory.bank }}` survives as a documented operator template surface (contract §2),
    # now always empty: ach-memory resolves the bank server-side from the project slug.
    memory_bank = ""
    engine_runner = make_engine_runner(
        pool=pool,
        driver=driver,
        engine_cfg=engine_cfg,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        terminal_output_retries=cfg.limits.terminal_output_retries,
        max_tool_calls=cfg.engine.max_tool_calls,
        memory_cfg=cfg.memory,
        channel_ttl=channel_ttl,
        channels_by_name=channels_by_name,
        agent_name=cfg.agent.name,
        memory_bank=memory_bank,
        memory_project=memory_project,
        memory_auth_headers=memory_auth_headers,
        stats_sink=stats_sink,
        tool_sink=tool_sink,
        memory_facade_url=memory_facade_url,
        repo_facade_url=repo_facade_url,
        a2a_facade_url=a2a_facade_url,
        accountant=accountant,
        cost_source=cfg.cost.source,
    )

    # Step 6 (cont.): construct Router with all limits from config (RTR-03/04)
    router = Router(
        max_concurrent_invocations=cfg.limits.max_concurrent_invocations,
        max_queued_total=cfg.limits.max_queued_total,
        idempotency_window_seconds=cfg.limits.idempotency_window_seconds,
        dedup_store=dedup_store,
        engine_runner=engine_runner,
        max_invocation_seconds=float(cfg.limits.max_invocation_seconds),
        channel_concurrency={ch.name: ch.concurrency for ch in cfg.channels},
        max_concurrent_scripts=cfg.limits.max_concurrent_scripts,
        script_channels={ch.name for ch in cfg.channels if ch.type == "webhook-script"},
    )

    # --tui / --prompt launch modifiers: ignore the configured channels and drive the
    # engine directly. The engine + proxies + hydration are already wired above; the
    # first prompt warms the engine via the router→pool path (no HTTP A′ gate involved).
    if console_mode:
        if one_shot_prompt is not None:
            log.info("ach-agent: --prompt one-shot mode (configured channels ignored)")
        elif debug_mode:
            log.info("ach-agent: --debug plain console mode (configured channels ignored)")
        else:
            log.info("ach-agent: --tui console mode (configured channels ignored)")
        try:
            if one_shot_prompt is not None:
                await run_one_shot(router, one_shot_prompt)
            else:
                # --tui/--debug: launch opencode at boot (not lazily on the first prompt) + hold a
                # ref for the whole REPL, so per-invocation release(0) never stops it between
                # prompts — there is no idle TTL; only Ctrl-C / EOF ends the session (the
                # finally below stops it). Probe memory first so the pre-warmed server's
                # opencode.json wires the memory MCP exactly as engine_runner would.
                import dataclasses

                # codemem is already on engine_cfg from boot (static); only the memory
                # facade is resolved here so the pre-warmed opencode.json matches.
                warm_mcp_servers: list[str] = []
                if isinstance(cfg.memory, AchMemoryMemory):
                    from ach_agent.memory.ach_memory import prepare_ach_memory

                    _mem_ok, _ = await prepare_ach_memory(
                        cfg.memory, memory_project, memory_auth_headers
                    )
                    if _mem_ok and memory_facade_url:
                        warm_mcp_servers = [memory_facade_url]
                # The repo-checkout facade is static (no probe) — include it in the pre-warmed
                # opencode.json so the console session sees checkout_repo from the first prompt.
                if repo_facade_url:
                    warm_mcp_servers = [*warm_mcp_servers, repo_facade_url]
                if a2a_facade_url:
                    warm_mcp_servers = [*warm_mcp_servers, a2a_facade_url]
                from ach_agent.channels.tui import _CONSOLE_SESSION_KEY

                warm_codemem_project = engine_cfg.codemem_project
                if isinstance(cfg.memory, CodememMemory) and "{{" in engine_cfg.codemem_project:
                    warm_ctx = build_template_context(
                        {},
                        channel_name="tui",
                        channel_type="tui",
                        channel_source="",
                        agent_name=cfg.agent.name,
                        memory_bank="",
                        event_id="",
                        session_key=_CONSOLE_SESSION_KEY,
                    )
                    # Keyed pool reuses this warm server for the whole console session, so the
                    # project must be rendered HERE — engine_runner's later render is discarded.
                    warm_codemem_project = render_template(engine_cfg.codemem_project, warm_ctx)
                warm_cfg = dataclasses.replace(
                    engine_cfg, mcp_servers=warm_mcp_servers, codemem_project=warm_codemem_project
                )
                # --debug and non-TTY use the harness REPL. Pi's real-TTY --tui is its
                # native CLI, configured by the harness but not launched in RPC mode.
                if debug_mode or not sys.stdout.isatty():
                    await run_tui_console(router)
                elif cfg.engine.type == "pi":
                    from ach_agent.engine.pi.driver import PiDriver

                    # Native Pi bypasses the pool, so nothing has tokenized its proxied
                    # wires. Mint here or the console's model AND tool calls take the
                    # proxies' PLAIN routes and reach Langfuse uncorrelated.
                    tui_token = trace.mint_token()
                    trace.begin_tui(tui_token)
                    warm_cfg = dataclasses.replace(
                        warm_cfg,
                        model_base_url=trace.tokenize_url(warm_cfg.model_base_url, tui_token),
                        mcp_local_urls={
                            sid: trace.tokenize_url(url, tui_token)
                            for sid, url in warm_cfg.mcp_local_urls.items()
                        },
                    )
                    await PiDriver().run_tui(warm_cfg, _CONSOLE_SESSION_KEY)
                else:
                    warm_server = await pool.acquire(_CONSOLE_SESSION_KEY, warm_cfg)
                    # attach drives opencode's own loop — run_turn never runs, so this is
                    # the only place the console session can be correlated.
                    trace.begin_tui(warm_server.proxy_token)
                    # No stdout banner — opencode's own --print-logs already announces the
                    # listening address. Keep one structured info line with the loopback address.
                    log.info(
                        "ach-agent: opencode serve listening",
                        url=f"http://127.0.0.1:{warm_server.port}",
                    )
                    await _run_opencode_attach(
                        router,
                        binary_path=engine_cfg.binary_path,
                        port=warm_server.port,
                        ephemeral_home=warm_server.ephemeral_home,
                        config_path=warm_server.config_path,
                    )
        finally:
            # Stop any warm-held engine server (idle TTL may not have elapsed at EOF).
            await pool.stop_all()
            if hasattr(pool.sessions, "close"):
                pool.sessions.close()
            await stop_model_proxies()
            if mcp_proxy is not None:
                await mcp_proxy.stop()
            if memory_facade is not None:
                await memory_facade.stop()
            if repo_facade is not None:
                await repo_facade.stop()
            if a2a_facade is not None:
                await a2a_facade.stop()
            if hasattr(dedup_store, "close"):
                dedup_store.close()
            await stats_sink.stop()
            await tool_sink.stop()
        log.info("ach-agent: session ended")
        return

    # Collect webhook channels to wire; build FastAPI app if any exist
    webhook_channels = [ch for ch in cfg.channels if ch.type in ("webhook", "webhook-script")]

    # Build A2A bridges and sub-apps (topology A: mounted under the same FastAPI/uvicorn socket).
    # W9: engine_runner must NOT import channels.a2a or hold a bridge reference.
    # Wiring: for each A2A channel, construct an A2AAgentExecutorBridge, then wrap the router
    # in a thin handler that injects an on_complete closure into event.delivery_context before
    # routing. engine_runner reads event.delivery_context['on_complete'] and calls it — no
    # channel-type-specific logic in engine_runner (dependency arrow: channels→engine only).
    a2a_bridges: list[A2AAgentExecutorBridge] = []
    a2a_mounts: list[tuple[str, Any]] = []

    for channel in cfg.channels:
        if channel.type != "a2a":
            continue

        # The bridge is created here (boot module) — engine_runner never imports it.
        bridge = A2AAgentExecutorBridge(handler=None, channel_cfg=channel)

        # on_complete/on_fail (W9: bound here in the boot module, engine tier stays
        # unaware of A2A type). on_fail mirrors on_complete: emits a FAILED event when
        # the terminal output is unusable (action != a2a_reply, or empty reply text).
        _on_complete = bridge.signal_completion
        _on_fail = bridge.signal_failure

        # Wrap the router to inject on_complete + on_fail into delivery_context (W9 pattern).
        bridge._handler = _A2AHandler(router, _on_complete, _on_fail)

        # Build the A2A AgentCard from channel config (minimal — receiver-only v1, spec §14.6).
        # make_a2a_agent_card keeps a2a.* imports inside channels/a2a.py (RTR-06 fence).
        agent_card = make_a2a_agent_card(channel.name)
        sub_app = build_a2a_app(agent_card, bridge)
        mount_path = f"/a2a/{channel.name}"
        a2a_mounts.append((mount_path, sub_app))
        a2a_bridges.append(bridge)
        log.info("a2a channel bridge built", channel_name=channel.name, mount_path=mount_path)

    # 6c. Create FastAPI app with all webhook channels.
    # a2a_mounts threads the A2A sub-apps under the same socket (topology A).
    app = create_app(
        channels=webhook_channels,
        handler=router,
        a2a_mounts=a2a_mounts,
    )
    # Expose state so _drain can flip draining/ready (same ref as app.extra['state'])
    state: HealthState = app.extra["state"]

    # Step 7: wire channel adapters (D-08: one CronScheduler for ALL cron channels, SC#3)
    tasks: list[asyncio.Task[None]] = []
    uv_server: Any = None  # captured below when uvicorn boots (always)

    # D-08/SC#3: collect all cron channels and construct exactly ONE CronScheduler.
    # Pitfall 9 (one task per channel) is superseded by D-08 (one scheduler for all).
    cron_channels = [ch for ch in cfg.channels if ch.type == "cron"]
    cron_scheduler: CronScheduler | None = None
    if cron_channels:
        cron_scheduler = CronScheduler(cron_channels, handler=router)
        await cron_scheduler.start()
        log.info(
            "cron scheduler started",
            channel_count=len(cron_channels),
            channel_names=[ch.name for ch in cron_channels],
        )

    # Queue channels (redis Streams, ackMode:onComplete): one QueueConsumer each.
    # Each consumer owns a single asyncio consume task; stopped in the drain branch.
    queue_channels = [ch for ch in cfg.channels if ch.type == "queue"]
    queue_consumers: list[QueueConsumer] = []
    for channel in queue_channels:
        consumer = QueueConsumer(channel, handler=router)
        await consumer.start()
        queue_consumers.append(consumer)
        log.info("queue consumer started", channel_name=channel.name, stream=channel.queue.key)  # type: ignore[union-attr]

    log.info("channels registered", names=[ch.name for ch in cfg.channels])

    # Boot uvicorn UNCONDITIONALLY (CONTRACT §4): healthz/readyz/metrics MUST always be
    # reachable, even for cron-only or queue-only configs with no inbound HTTP channel —
    # otherwise k8s liveness/readiness probes fail and the pod is killed. Webhook + a2a
    # channels additionally serve their routes on this same socket (topology A).
    # uvicorn shares the SAME event loop as the cron tasks — no thread pool,
    # single-process topology (spec §15 topology A).
    host = cfg.health.host
    port = cfg.health.port
    uv_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",  # uvicorn internal logs; harness uses structlog
    )
    uv_server = uvicorn.Server(uv_config)
    log.info("uvicorn starting", host=host, port=port)
    tasks.append(asyncio.create_task(uv_server.serve()))

    # Install SIGTERM handler via loop.add_signal_handler (NOT signal.signal).
    # RESEARCH Pitfall 2: uvicorn uses signal.signal() inside capture_signals() —
    # loop.add_signal_handler uses signalfd on Linux and coexists with signal.signal.
    shutdown_event: asyncio.Event = asyncio.Event()

    def _on_sigterm() -> None:
        # Idempotent: a repeat SIGTERM/SIGINT during drain (or a late handler
        # invocation as uvicorn restores its own signal handlers on shutdown) must
        # not re-log or re-trigger — the drain is already underway.
        if shutdown_event.is_set():
            return
        log.info("SIGTERM received — initiating graceful drain")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    loop.add_signal_handler(signal.SIGINT, _on_sigterm)  # Ctrl+C in dev

    log.info("ach-agent started", channel_count=len(tasks))

    # Wait for SIGTERM/SIGINT OR all tasks to finish (tasks loop forever normally).
    # uvicorn boots unconditionally, so `tasks` is never empty.
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    await asyncio.wait(
        [shutdown_task, *tasks],
        return_when=asyncio.FIRST_COMPLETED,
    )

    if shutdown_event.is_set():
        # Stop queue consumers BEFORE draining. Unlike HTTP/cron intake, queue consumers
        # do NOT honor the `draining` flag — they keep xreadgroup'ing redis and routing
        # events into lanes. If left running during _drain, they feed new events into lanes
        # that _drain is cancelling: those events fail admission, stay unacked, and get
        # redelivered on the next boot (redelivery churn, defeats graceful drain). Stopping
        # consumers first guarantees no new events enter lanes; events already routed are
        # still drained to completion by _drain's lane.join() below.
        for consumer in queue_consumers:
            await consumer.stop()
        # Graceful drain (DUR-03): flip readyz, drain lanes, cleanup. _drain sets
        # uv_server.should_exit so uvicorn stops accepting, then returns (no sys.exit).
        await _drain(
            state=state,
            uv_server=uv_server,
            cron_scheduler=cron_scheduler,
            router=router,
            dedup_store=dedup_store,
        )
        # Stop every warm keyed opencode server BEFORE the proxies. With
        # engine.idle_ttl_seconds > 0 a recently-used server lingers past its last release
        # with a pending _expire task; without this its subprocess (start_new_session=True,
        # own process group) would survive the harness exit and orphan (leaking the port).
        # Idempotent; also cancels the pending TTL tasks.
        await pool.stop_all()
        if hasattr(pool.sessions, "close"):
            pool.sessions.close()
        # Plan 2: tear down the localhost proxies (closes their aiohttp runners/sessions).
        await stop_model_proxies()
        if mcp_proxy is not None:
            await mcp_proxy.stop()
        if memory_facade is not None:
            await memory_facade.stop()
        if repo_facade is not None:
            await repo_facade.stop()
        if a2a_facade is not None:
            await a2a_facade.stop()
        await stats_sink.stop()
        await tool_sink.stop()
        # uvicorn's serve() task returns on its own once should_exit=True; await it
        # so its lifespan shutdown completes before asyncio.run tears the loop down.
        # This avoids the force-cancel CancelledError traceback the old sys.exit(0)
        # produced — the process now exits 0 cleanly.
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("ach-agent shutdown complete")
    else:
        # Normal termination (all tasks completed without SIGTERM — rare in prod)
        log.info("ach-agent shutdown complete")


def _parse_cli(argv: list[str]) -> tuple[bool, str | None, bool]:
    """Parse launch modifiers from argv.

    `--tui` → native TUI for the selected engine. `--debug` → plain stdin/stdout REPL (minimal,
    pipe-friendly). `--prompt TEXT` (or `--prompt=TEXT`) → single non-interactive prompt
    then exit. All ignore configured channels; precedence is `--prompt` > `--debug` > `--tui`.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--tui", action="store_true")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--prompt")
    args, _unknown = parser.parse_known_args(argv)
    return args.tui, args.prompt, args.debug


if __name__ == "__main__":
    # `--tui` / `--debug` / `--prompt` launch modifiers: drive the engine directly.
    _tui_mode, _one_shot, _debug_mode = _parse_cli(sys.argv[1:])
    try:
        asyncio.run(main(tui_mode=_tui_mode, one_shot_prompt=_one_shot, debug_mode=_debug_mode))
    except KeyboardInterrupt:
        # ponytail: Ctrl+C in the console/REPL modes — exit quietly, no traceback.
        pass

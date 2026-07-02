# SPDX-License-Identifier: Apache-2.0
"""Engine lifecycle: subprocess launch, readiness polling, invocation, shutdown.

Constraint: No router or Hermes imports (D-08, RTR-06).

Hardening implemented in 00-02:
  - H-01: 1MB SSE read buffer (in client.py)
  - H-02: bounded health-gated SSE reconnect + mid-invocation liveness on the live path
    (consume_sse_after_send below; reuses events.py's shared reader/accumulator helpers)
  - H-03: Process-group kill (SIGTERM → 10s → SIGKILL) via _process_group_kill
  - H-05: stdout/stderr drain tasks (_drain_logs with 50-line tail, started at launch)
  - ENG-06: Startup deadline calls sys.exit(1), NOT raises
  - maxInvocationSeconds: owned by the lane (router), NOT run_invocation (Plan 1)
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import os
import re
import shutil
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from ach_agent.engine.events import OpenCodeToolUpdate

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SHUTDOWN_TIMEOUT = 10  # seconds between SIGTERM and SIGKILL (H-03)
_LOG_TAIL_SIZE = 50  # lines kept in stdout/stderr tail for diagnostics (H-05)
# Mid-invocation liveness poll: how often the SSE consume loop wakes to check the engine is
# still alive (B5) instead of blocking a flat 300s on a single queue.get(). The cumulative
# 300s stall bound (reset on every real event) is preserved as the wedged-but-alive backstop.
_LIVENESS_POLL_S = 5.0
_SSE_STALL_S = 300.0  # cumulative no-event bound before giving up on a live-but-wedged stream


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class EngineConfig:
    """Rendered runtime config — engine section (CONTRACT.md §2).

    Fields extended by later plans as needed.
    """

    binary_path: str = "opencode"
    # Stable opencode HOME: holds .config/opencode (opencode.json + hydrated skills),
    # .local/share/opencode (sessions — persist because HOME is stable), and node_modules.
    home: str = ""
    work_dir: str = "/workspace"
    model: str = "gpt-4o-mini"  # opencode validates model names; must be a known OpenAI model ID
    params: dict[str, object] = field(default_factory=dict)  # model params (temperature, …)
    system_prompt: str = ""
    steps: int = 50
    startup_timeout_seconds: int = 30
    max_invocation_seconds: int = 1800
    # MEM-01/D-02: optional MCP server URL for memory tools (present iff backend reachable).
    # Written to opencode.json before subprocess launch so the model either has memory tools
    # or does not — no runtime tool-registration API exists.
    mcp_servers: list[str] = field(default_factory=list)
    # Plan 2 (localhost-proxy / ek-hygiene): opencode.json points the model at this localhost
    # model-proxy baseURL (no ek_; the proxy injects it). Always set in a real boot — the
    # harness hard-fails without it (no direct-gateway fallback). Empty only in unit tests
    # that never invoke the model.
    model_base_url: str = ""
    # {server_id: "http://127.0.0.1:<port>/mcp/<id>"} from McpProxy — proxied external MCP
    # servers written into opencode.json's mcp block alongside any memory server.
    mcp_local_urls: dict[str, str] = field(default_factory=dict)
    # codemem (MEM/D-02): when set, opencode.json registers a LOCAL stdio MCP server that
    # opencode spawns as its own child: `codemem mcp --db-path <db>`. Empty → no codemem.
    # Static per-agent db path (operator config). Viewer is disabled via env (headless).
    codemem_db_path: str = ""
    # Stable codemem project namespace (config memory.codemem.project → CODEMEM_PROJECT env).
    # Required in config; carried here so the codemem MCP entry pins a consistent project.
    codemem_project: str = ""
    # SEC-01 / ek-hygiene: extra env var NAMES the operator wants forwarded from the harness
    # env into the opencode subprocess (engine.forwardEnv). The opencode env is built
    # clean-slate from a small base allowlist (see build_opencode_env) — nothing else is
    # inherited — so the ek_ (ACH_TOKEN/ACH_API_KEY) never reaches opencode unless explicitly
    # named here. Use sparingly (e.g. a custom CA bundle path); never list the ek_.
    forward_env: list[str] = field(default_factory=list)
    # capability.filter.exclude.tools — opencode tool ids to disable in opencode.json
    # (agent.build.tools[<id>]=False), withholding them from the model.
    exclude_tools: list[str] = field(default_factory=list)


@dataclass
class ManagedServer:
    """Owns one opencode subprocess + HTTP client + port + ephemeral home.

    Created by launch(); stopped via stop().
    """

    port: int = 0
    ephemeral_home: Path = field(default_factory=lambda: Path("/tmp/oc-unset"))
    config_path: Path | None = None
    # process and client are None until launch() populates them
    _process: object | None = field(default=None, repr=False)
    _client: object | None = field(default=None, repr=False)
    # logical session_key → opencode session id (ses_…). opencode requires a session
    # created via POST /session before POST /session/{id}/message; we create one per
    # logical key on first use and reuse it for conversational continuity.
    _sessions: dict[str, str] = field(default_factory=dict, repr=False)
    # 50-line tail buffers for diagnostics (H-05)
    _stdout_tail: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=_LOG_TAIL_SIZE), repr=False
    )
    _stderr_tail: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=_LOG_TAIL_SIZE), repr=False
    )

    def is_alive(self) -> bool:
        """Return True if the subprocess is still running."""
        proc = self._process
        if proc is None:
            return False
        # asyncio.subprocess.Process has .returncode (None = still running)
        return getattr(proc, "returncode", None) is None

    async def stop(self) -> None:
        """Gracefully terminate the subprocess (SIGTERM → wait → SIGKILL).

        Idempotent (CR-01): sets self._process = None after kill so a second
        call is a no-op regardless of PID reuse.
        """
        proc = self._process
        if proc is None:
            return
        self._process = None  # CR-01: clear before kill so second stop() is no-op
        await _process_group_kill(proc)  # type: ignore[arg-type]

        # Close the aiohttp client session
        client = self._client
        if client is not None and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                pass

        # B7: free the reserved port so a later server can reuse it. release_port
        # uses set.discard, so a double-stop (port already released) is a safe no-op.
        from ach_agent.engine.client import release_port

        release_port(self.port)


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def _key_suffix(session_key: str) -> str:
    """Filename-safe, deterministic suffix for a session_key.

    Gives each keyed opencode process its OWN config + prompt file inside the SHARED
    home, so distinct keys never truncate-rewrite the same file (I-1) while sharing
    skills/.ach-state/node_modules under that home. session_key is always non-empty
    (the pool derives it per channel).
    """
    digest = hashlib.sha1(session_key.encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_key)[:48]
    return f"_{safe}-{digest}"


def write_opencode_config(ephemeral_home: Path, config: EngineConfig, session_key: str) -> Path:
    """Write per-session opencode config file to ephemeral home before subprocess launch.

    Security (SEC-01 / T-00-EK / Pitfall 6): no secret is ever written. opencode points at
    the localhost model-proxy and the proxy injects the ek_; opencode.json carries only a
    dummy apiKey and the loopback baseURL.

    Security (T-00-TRACE): Secrets are never passed as CLI arguments — only config file.

    Returns the Path of the config file written (opencode_<suffix>.json).
    """
    suffix = _key_suffix(session_key)
    config_dir = ephemeral_home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = config_dir / "personality" / f"system_prompt{suffix}.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(config.system_prompt or "", encoding="utf-8")

    # CONTRACT §6.10 ek-hygiene: opencode always points at the localhost model-proxy baseURL
    # (model_base_url); the proxy injects the ek_, so opencode.json carries NO ek_ and NO real
    # ACH URL. apiKey is a dummy placeholder (the proxy ignores inbound auth and adds its own).
    api_key = "local-proxy"  # placeholder; real ek_ injected by the localhost proxy
    base_url = config.model_base_url

    # opencode provider: a CUSTOM provider id backed by @ai-sdk/openai-compatible (lenient
    # parser) instead of opencode's bundled @ai-sdk/openai (strict). The strict provider
    # crashes ("text part not found") on ACH/litellm's gemini compat wire, which leaks
    # gemini thought-signatures into tool_call ids and sends tool_calls with null content.
    # @ai-sdk/openai-compatible tolerates it (verified against real ACH). A custom id is
    # required — opencode honors the `npm` field only for non-builtin provider ids.
    provider_id = "ach"
    oc_config: dict[str, object] = {
        "autoupdate": False,
        "permission": "allow",
        "share": "disabled",
        "logLevel": "WARN",
        "model": f"{provider_id}/{config.model}",
        # opencode uses a "small model" for side tasks (session titles, summaries).
        # It defaults to a hardcoded gpt-5-nano which 400s on ACH (not in the catalog),
        # so pin it to the configured model (registered + working) to avoid the error.
        "small_model": f"{provider_id}/{config.model}",
        "enabled_providers": [provider_id],
        "provider": {
            provider_id: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "ACH",
                "options": {
                    "apiKey": api_key,
                    "baseURL": base_url,
                    **config.params,
                },
                # Register the hydrated model id explicitly. ACH model ids
                # (e.g. "gemini.gemini-flash-latest") are not in opencode's built-in
                # provider catalog; without this opencode raises ProviderModelNotFoundError.
                "models": {config.model: {}},
            }
        },
        "instructions": [str(prompt_path.resolve())],  # append-mode system prompt
        "agent": {
            "build": {
                "steps": config.steps,
                # Disable opencode's interactive `question` tool. The harness runs a single
                # prompt→reply turn with no channel to feed an answer back into a running
                # opencode turn, so an asked question dead-ends (blocks, then times out).
                # Removing it forces the agent to answer in text instead.
                "tools": {"question": False, **{t: False for t in config.exclude_tools}},
            },
            "plan": {"disable": True},
        },
    }

    # Register MCP servers in opencode.json pre-launch (no runtime tool-registration API).
    # opencode 1.16 schema (verified live): `mcp.<id> = {type:"remote", url, enabled:true}`
    # — NOT nested under a `servers` key, and the type is "remote" (not "streamable-http").
    # Two sources, never colliding on key:
    #   - memory server(s) from mcp_servers (MEM-02; present iff the backend was reachable)
    #   - proxied external MCP servers from mcp_local_urls (Plan 2; localhost URLs only)
    # SEC (T-04-22 / §6.10): only URLs are written — the ek_ bearer is never in config files.
    mcp_block: dict[str, dict[str, object]] = {
        f"memory-{i}": {"type": "remote", "url": url, "enabled": True}
        for i, url in enumerate(config.mcp_servers)
    }
    for sid, url in config.mcp_local_urls.items():
        mcp_block[sid] = {"type": "remote", "url": url, "enabled": True}
    if config.codemem_db_path:
        # MCP type=local: opencode owns the codemem stdio child (1:1 with this opencode process).
        # SEC: no ek_; codemem is local. Viewer disabled (headless, N sessions).
        # CODEMEM_PROJECT: pin a STABLE project namespace. codemem otherwise derives the
        # project from cwd (git repo root), and for a non-git work_dir its remember/search
        # fallbacks disagree — memories are stored under one project but the default search
        # looks under another, so cross-session recall silently returns nothing. The db is
        # already per-agent, so a fixed project makes remember + search always agree.
        mcp_block["codemem"] = {
            "type": "local",
            "command": ["codemem", "mcp", "--db-path", config.codemem_db_path],
            "enabled": True,
            "environment": {
                "CODEMEM_VIEWER": "0",
                "CODEMEM_VIEWER_AUTO": "0",
                "CODEMEM_PROJECT": config.codemem_project,
            },
        }
    if mcp_block:
        oc_config["mcp"] = mcp_block
    config_path = config_dir / f"opencode{suffix}.json"
    config_path.write_text(json.dumps(oc_config, indent=2), encoding="utf-8")
    log.debug(
        "opencode config written",
        path=str(config_path),
    )
    return config_path


# Base allowlist (SEC-01 / ek-hygiene): the only harness env vars opencode inherits by
# default — the benign set a CLI needs to run (locate binaries, locale, terminal). Secrets
# (ACH_TOKEN/ACH_API_KEY, GITLAB_TOKEN, provider keys) are deliberately ABSENT. HOME/TMPDIR
# are pinned to the ephemeral home below, so the values here (if any) are overridden.
# Note: XDG_CONFIG_HOME is intentionally excluded — it would override $HOME/.config/opencode
# and break the ephemeral opencode.json the harness just wrote.
_OPENCODE_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "SHELL",
        "USER",
        "LOGNAME",
        "HOSTNAME",
        "LANG",
        "LANGUAGE",
        "TERM",
        "TZ",
    }
)


def build_opencode_env(
    ephemeral_home: Path, config: EngineConfig, config_path: Path
) -> dict[str, str]:
    """Construct a minimal, allowlisted environment for the opencode subprocess.

    Clean-slate (SEC-01 / ek-hygiene): opencode does NOT inherit the harness env. It gets
    only ``_OPENCODE_ENV_ALLOWLIST`` (benign CLI basics), plus any var NAMES the operator
    lists in ``engine.forwardEnv`` (config.forward_env). The ek_ (ACH_TOKEN/ACH_API_KEY)
    is never present unless explicitly named — and it must not be, because the localhost
    model-proxy injects it and opencode points only at 127.0.0.1.

    Pinned last (override anything above): HOME → the shared ephemeral home; TMPDIR → ``/tmp``
    (world-writable, off the persistent volume) so opencode's bun runtime extracts its native
    addons (~15 MB, regenerated every boot, dlopen'd not renamed → no cross-device risk) into
    ephemeral space instead of bloating HOME / the PVC. The harness already requires a writable
    /tmp (harness log dir, default engine home). GIT_TERMINAL_PROMPT=0 so git never blocks a
    non-interactive subprocess on a prompt.
    Note: XDG_CONFIG_HOME is intentionally excluded — it would override $HOME/.config/opencode
    and break the per-session opencode config file the harness just wrote.
    """
    env: dict[str, str] = {
        name: os.environ[name] for name in _OPENCODE_ENV_ALLOWLIST if name in os.environ
    }
    # Operator-defined exceptions (engine.forwardEnv) — forwarded by name when present.
    for name in config.forward_env:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    # Exa web-search is on by default for every agente: the harness pins the enable flag so
    # opencode always gets it without the operator listing it in forwardEnv. setdefault means a
    # forwarded harness value (via forwardEnv) still wins if the operator sets one explicitly.
    env.setdefault("OPENCODE_ENABLE_EXA", "true")
    # Pinned hardening — last word, overrides any inherited/forwarded value.
    env["HOME"] = str(ephemeral_home)
    env["TMPDIR"] = "/tmp"
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Point opencode at THIS server's per-session config file; HOME-shared skills/
    # agents and the session store stay discoverable from the shared config dir.
    env["OPENCODE_CONFIG"] = str(config_path)
    return env


async def launch(
    port: int, ephemeral_home: Path, config: EngineConfig, session_key: str
) -> ManagedServer:
    """Launch opencode serve in a shared ephemeral home with a per-session config file.

    Hardening applied:
      - start_new_session=True (Pitfall 3 / H-03: process-group kill safety)
      - GIT_TERMINAL_PROMPT=0 (prevent git from prompting and hanging)
      - stdout/stderr PIPE with drain tasks (H-05: prevents PIPE buffer deadlock)
      - OPENCODE_SERVER_PASSWORD NOT set (deviation from Pitfall 5: setting it
        causes GET /app to return 401; documented in SUMMARY)
    """
    from ach_agent.engine.client import OpenCodeClient

    binary = shutil.which(config.binary_path)
    if not binary:
        raise RuntimeError(f"opencode binary not found: {config.binary_path!r}")

    config_path = write_opencode_config(ephemeral_home, config, session_key)

    # Ensure work_dir exists
    work_dir = Path(config.work_dir)
    if not work_dir.exists():
        work_dir.mkdir(parents=True, exist_ok=True)

    # Build a clean-slate subprocess env (SEC-01 / ek-hygiene): opencode inherits NOTHING
    # from the harness env except a small base allowlist + operator-named extras. The ek_
    # (ACH_TOKEN/ACH_API_KEY) is therefore absent from opencode's environment in proxy mode.
    #
    # Note on OPENCODE_SERVER_PASSWORD (Pitfall 5 deviation):
    # When set, opencode requires authentication for ALL routes including GET /app,
    # breaking the readiness probe (401). Do NOT set it; accept the warning in logs.
    # The server binds to 127.0.0.1 only.
    env = build_opencode_env(ephemeral_home, config, config_path)

    log.info(
        "launching opencode serve",
        port=port,
        hostname="127.0.0.1",
        binary=binary,
        ephemeral_home=str(ephemeral_home),
    )

    proc = await asyncio.create_subprocess_exec(
        binary,
        "serve",
        "--port",
        str(port),
        "--hostname",
        "127.0.0.1",  # loopback only — the harness client + opencode attach are co-located
        "--print-logs",
        "--pure",  # disable external plugins (Pitfall isolation)
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(work_dir),
        env=env,
        start_new_session=True,  # H-03 / Pitfall 3: required for os.killpg safety
    )

    server = ManagedServer(port=port, ephemeral_home=ephemeral_home, config_path=config_path)
    server._process = proc

    # H-05: Start drain tasks immediately after subprocess creation.
    # Two tasks drain stdout and stderr to prevent OS PIPE buffer (64KB) from
    # filling and deadlocking the subprocess. Also accumulates 50-line diagnostic tail.
    asyncio.create_task(_drain_logs(proc.stdout, "stdout", server._stdout_tail))  # type: ignore[arg-type]
    asyncio.create_task(_drain_logs(proc.stderr, "stderr", server._stderr_tail))  # type: ignore[arg-type]

    base_url = f"http://127.0.0.1:{port}"
    client = OpenCodeClient(base_url)
    await client.open()
    server._client = client

    log.info("opencode subprocess started", pid=proc.pid, port=port)
    return server


async def poll_ready(
    server: ManagedServer,
    startup_timeout_seconds: int,
) -> None:
    """Poll GET /app until HTTP 200 or deadline.

    ENG-06 / Pitfall 2: On deadline exceeded this calls sys.exit(1) — NOT raises.
    Raising would leave the process running; sys.exit(1) causes the substrate to
    mark the pod NotReady and restart it per spec §8.5.
    """
    from ach_agent.engine.client import OpenCodeClient

    client = server._client
    if not isinstance(client, OpenCodeClient):
        raise RuntimeError("ManagedServer has no client — call launch() first")

    loop = asyncio.get_running_loop()
    deadline = loop.time() + startup_timeout_seconds

    while loop.time() < deadline:
        proc = server._process
        if proc is not None and getattr(proc, "returncode", None) is not None:
            # Process died before ready — exit with error so substrate restarts
            log.error(
                "opencode exited during startup",
                code=getattr(proc, "returncode", None),
                port=server.port,
            )
            sys.exit(1)
        if await client.check_health():
            log.info("opencode ready", port=server.port)
            return
        await asyncio.sleep(0.5)

    # ENG-06 / Pitfall 2: startup deadline exceeded — must sys.exit(1)
    # NOT raise: the process must die so the substrate marks it NotReady.
    log.error(
        "opencode not ready within deadline — exiting",
        startup_timeout_seconds=startup_timeout_seconds,
        port=server.port,
    )
    sys.exit(1)


async def run_invocation(
    server: ManagedServer,
    session_id: str,
    prompt: str,
    terminal_retries: int,
    free_form: bool = False,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
    reuse: bool = True,
    max_tool_calls: int = 0,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Orchestrate: subscribe SSE → send prompt → consume → return the terminal object.

    No self-timeout: the lane owns the single authoritative maxInvocationSeconds bound
    (RTR-04). When the lane's deadline fires it cancels this coroutine; the caller
    (engine_runner) resolves the reply_future with InvocationTimeout and force-releases the
    pooled server (ttl=0), which stops the runaway process. This function therefore neither
    times out nor kills — it just runs the turn.

    IMPORTANT: SSE subscription MUST happen before send_message because opencode
    emits session.idle on the SSE stream in real-time. If send_message completes
    before subscribe_events(), the session.idle event is missed and the consumer hangs.
    """
    from ach_agent.engine.client import OpenCodeClient

    client = server._client
    if not isinstance(client, OpenCodeClient):
        raise RuntimeError("ManagedServer has no client")

    # opencode requires a session created via POST /session before /session/{id}/message
    # (sending to an arbitrary id → 500). Map the logical session_key → an opencode
    # session id, created once per key and reused for conversational continuity.
    # When reuse=False (channel.session='none'), always create a fresh opencode session
    # and never touch server._sessions (no read, no write).
    if reuse:
        oc_session_id = server._sessions.get(session_id)
        if oc_session_id is None:
            created = await client.create_session()
            oc_session_id = str(created.get("id", ""))
            if not oc_session_id:
                raise RuntimeError(f"opencode create_session returned no id: {created!r}")
            server._sessions[session_id] = oc_session_id
    else:
        created = await client.create_session()
        oc_session_id = str(created.get("id", ""))
        if not oc_session_id:
            raise RuntimeError(f"opencode create_session returned no id: {created!r}")

    # Subscribe to SSE FIRST, then send the message.
    # This ensures we don't miss the session.idle event. The lane bounds this await via
    # maxInvocationSeconds; a deadline cancels it (no inner timeout here).
    stats = stats if stats is not None else {}
    accumulated_text = await consume_sse_after_send(
        client,
        oc_session_id,
        prompt,
        on_text=on_text,
        on_tool=on_tool,
        is_alive=server.is_alive,
        max_tool_calls=max_tool_calls,
        stats=stats,
    )
    if stats.get("aborted"):
        # Step-budget abort (Plan 4): the turn was cut mid-tool-loop, so accumulated_text may
        # lack a terminal object. Run ONE wrap-up turn (budget OFF, same opencode session) so the
        # model stops calling tools and emits a clean terminal object; its output then flows
        # through the normal extract/repair path below.
        # NOTE: legacy also ran a "tool-only correction" (has_text_beyond_action) — NOT ported:
        # ach-agent's terminal contract expects prose + a trailing terminal object and
        # extract_terminal rfinds the last {"action"...}, so "text beyond the action" is normal
        # here, not an error to correct. Only the step-budget abort/wrap-up transfers.
        log.warning("step-budget abort — running wrap-up turn", session_id=session_id)
        wrap = (
            "You have reached your tool-call budget for this turn. Do NOT call any more tools. "
            "Reply now with ONLY the terminal JSON object "
            '({"action":"none","text":"..."} or {"action":"a2a_reply","text":"..."}) '
            "summarizing what you found and did."
        )
        accumulated_text = await consume_sse_after_send(
            client,
            oc_session_id,
            wrap,
            on_text=on_text,
            on_tool=on_tool,
            is_alive=server.is_alive,
            max_tool_calls=0,  # never abort the wrap-up
        )

    # debug (not info): at INFO this line lands on stderr right as the streamed reply
    # finishes on stdout, gluing onto the last reply char on a shared TTY.
    log.debug(
        "invocation complete",
        session_id=session_id,
        text_length=len(accumulated_text),
    )

    # Free-form mode (--tui console): no terminal contract — return the raw reply text
    # verbatim. The terminal-extraction + repair turn below is for the structured
    # channels (a2a_reply / none); applying it to a console chat reply would fire a
    # pointless extra model turn and could print the repair output instead of the reply.
    if free_form:
        return {"action": "none", "text": accumulated_text}

    # Extract the single terminal object from accumulated SSE text. One backstop
    # repair turn if absent — then fall back to a synthetic none-action carrying the
    # raw text so the caller always receives a dict with an "action" key.
    from ach_agent.engine.validator import extract_terminal

    obj = extract_terminal(accumulated_text)
    if obj is None and terminal_retries > 0:
        repair = (
            "Reply with ONLY a terminal JSON object: "
            '{"action":"none","text":"..."} or {"action":"a2a_reply","text":"..."}.'
        )
        accumulated_text = await consume_sse_after_send(
            client, oc_session_id, repair, is_alive=server.is_alive
        )
        obj = extract_terminal(accumulated_text)
    return obj if obj is not None else {"action": "none", "text": accumulated_text}


async def consume_sse_after_send(
    client: object,
    session_id: str,
    prompt: str,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
    *,
    is_alive: Callable[[], bool] | None = None,
    max_reconnects: int = 3,
    max_tool_calls: int = 0,
    stats: dict[str, Any] | None = None,
) -> str:
    """Subscribe to SSE, send the prompt ONCE, then consume to session.idle with reconnect.

    The SSE subscription must happen before send_message because opencode emits events in
    real-time. This helper ensures correct ordering, and additionally (B4/B5):

    - **Reconnect (B4):** a transient SSE-reader drop (``aiohttp.ClientError``) mid-turn is
      recovered by re-subscribing (up to ``max_reconnects``), gated on ``check_health()`` (and
      ``is_alive()`` when given). The prompt is sent EXACTLY ONCE — ``result_queue``, the send
      task, and the ``ReplyAccumulator`` persist across attempts; only ``resp`` + the reader
      task are recreated. opencode resends growing cumulative snapshots, so the accumulator's
      prefix-dedup means re-sent text is neither double-counted nor re-emitted via ``on_text``.
      A send-POST failure (``_SendFailed``) is terminal — never reconnected.
    - **Liveness (B5):** each queue wait uses a short ``_LIVENESS_POLL_S`` poll; on a poll
      timeout, if ``is_alive()`` is False the invocation fails fast with ``engine_died`` instead
      of hanging out the full ``_SSE_STALL_S`` bound (which is preserved, reset on every real
      event, as the wedged-but-alive backstop).

    ``on_text``: optional sink called with each new text suffix as the assistant's
    message.part.updated snapshots grow (so a console prints the reply live instead of
    waiting for the whole invocation).
    ``on_tool``: optional sink called with each tool-part lifecycle update (running /
    completed / error). Lets a channel show "running a tool" — important when a tool
    such as calendar auth_wait blocks for minutes after the visible text is produced,
    which otherwise looks like a frozen, unfinished reply.
    ``is_alive``: optional liveness probe (``ManagedServer.is_alive``); when None, liveness
    degrades to the ``_SSE_STALL_S`` bound and reconnect gates only on ``check_health()``.
    """
    import aiohttp

    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import (
        EngineError,
        OpenCodeSessionError,
        OpenCodeSessionIdle,
        OpenCodeTextUpdate,
        OpenCodeToolUpdate,
        OpenCodeUsage,
        OpenCodeUserMessage,
        ReplyAccumulator,
        _consume_events_from_response,
        _SendFailed,
    )

    assert isinstance(client, OpenCodeClient), "client must be OpenCodeClient"

    loop = asyncio.get_running_loop()
    # Persist ACROSS reconnect attempts: the reducer (opencode resends growing snapshots), the
    # user-echo id set, the result queue, and the single send task. Only resp + sse_task are
    # per-attempt.
    acc = ReplyAccumulator(on_text, on_tool)
    # Step-budget (Plan 4): count DISTINCT tool call_ids across the whole turn. A set (not a
    # counter) so opencode's resent snapshots on a reconnect never inflate the count. Persists
    # across reconnect attempts alongside acc.
    tool_call_ids: set[str] = set()
    aborted = False
    user_message_ids: set[str] = set()
    result_queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
    send_task: asyncio.Task[None] | None = None
    sent = False

    def _on_send_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
        # A send-POST failure is terminal: wrap it so the loop never reconnects/re-sends.
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            result_queue.put_nowait(_SendFailed(exc))

    try:
        for attempt in range(max_reconnects + 1):
            resp = await client.subscribe_events()
            sse_task = asyncio.create_task(
                _consume_events_from_response(client, resp, result_queue)
            )
            terminal_error: EngineError | None = None
            terminal_idle = False
            reconnect = False
            try:
                # Gate the send on a confirmed-live subscription. On a reconnect the fresh
                # /event connection re-emits server.connected, so this re-gates correctly.
                await _await_subscription_ready(result_queue, session_id)

                # opencode's POST /session/{id}/message is SYNCHRONOUS — it returns only when
                # the whole turn has finished. Fire it concurrently and consume the SSE stream
                # live (session.idle is the real done signal); awaiting it first would replay
                # every event in one burst at the end. Sent ONCE — a reconnect must not re-send
                # (that would start a duplicate turn).
                if not sent:
                    send_task = asyncio.create_task(client.send_message(session_id, prompt))
                    send_task.add_done_callback(_on_send_done)
                    sent = True

                stall_deadline = loop.time() + _SSE_STALL_S
                while True:
                    try:
                        item = await asyncio.wait_for(result_queue.get(), timeout=_LIVENESS_POLL_S)
                    except TimeoutError:
                        # Liveness poll (B5): a dead engine fails fast instead of waiting out
                        # the full stall bound.
                        if is_alive is not None and not is_alive():
                            raise EngineError("engine_died", "opencode exited mid-invocation")
                        if loop.time() >= stall_deadline:
                            raise EngineError("sse_timeout", "SSE stream stalled for 300s")
                        continue
                    # A real item arrived — reset the wedged-but-alive backstop.
                    stall_deadline = loop.time() + _SSE_STALL_S

                    if isinstance(item, _SendFailed):
                        raise item.original
                    if isinstance(item, aiohttp.ClientError):
                        # Reader drop (B4). An unhealthy/dead engine is terminal — surface the
                        # real ClientError. A healthy engine reconnects while budget remains;
                        # once the budget is spent the drop becomes sse_exhausted.
                        healthy = (is_alive is None or is_alive()) and await client.check_health()
                        if not healthy:
                            raise item
                        if attempt < max_reconnects:
                            log.warning(
                                "live SSE dropped, reconnecting",
                                attempt=attempt + 1,
                                max_reconnects=max_reconnects,
                            )
                            reconnect = True
                            break
                        raise EngineError(
                            "sse_exhausted",
                            f"SSE stream disconnected after {max_reconnects} reconnect attempts",
                        )
                    if isinstance(item, Exception):
                        raise item

                    event = item
                    if isinstance(event, OpenCodeUserMessage):
                        user_message_ids.add(event.message_id)
                    elif isinstance(event, OpenCodeTextUpdate):
                        if event.message_id not in user_message_ids:
                            acc.add_text(event.part_id, event.text)
                    elif isinstance(event, OpenCodeToolUpdate):
                        acc.add_tool(event)
                        tool_call_ids.add(event.call_id or event.part_id)
                        if (
                            max_tool_calls > 0
                            and not aborted
                            and len(tool_call_ids) >= max_tool_calls
                        ):
                            aborted = True
                            log.warning(
                                "step budget reached — aborting turn",
                                session_id=session_id,
                                tool_calls=len(tool_call_ids),
                                max_tool_calls=max_tool_calls,
                            )
                            try:
                                await client.abort_session(session_id)
                            except Exception:  # noqa: BLE001
                                log.warning(
                                    "abort_session failed", session_id=session_id, exc_info=True
                                )
                            # keep consuming — session.idle arrives after the abort
                    elif isinstance(event, OpenCodeUsage):
                        acc.add_usage(event)
                    elif isinstance(event, OpenCodeSessionIdle):
                        log.debug("session.idle received", session_id=session_id)
                        if stats is not None:
                            stats["tool_calls"] = len(tool_call_ids)
                            stats["aborted"] = aborted
                        terminal_idle = True
                        break
                    elif isinstance(event, OpenCodeSessionError):
                        log.warning(
                            "session.error received",
                            session_id=session_id,
                            error_type=event.error_type,
                            message=event.message,
                        )
                        terminal_error = EngineError(event.error_type, event.message)
                        break
            finally:
                # Fully stop THIS attempt's reader before the loop may start a new one on the
                # SHARED result_queue. Awaiting (not wait-with-timeout-then-abandon) guarantees
                # no second reader can ever run concurrently and interleave/pollute events into
                # the next attempt's readiness gate. _consume_events_from_response swallows
                # CancelledError and returns, so this is prompt and won't raise; a pathological
                # non-cancellable read is still bounded from above by the lane's
                # maxInvocationSeconds (which cancels this await too).
                sse_task.cancel()
                try:
                    await sse_task
                except asyncio.CancelledError:
                    pass
                try:
                    await resp.release()
                except Exception:  # noqa: BLE001
                    pass

            if terminal_error is not None:
                raise terminal_error
            if terminal_idle:
                if stats is not None:
                    stats["tool_count"] = acc.tool_count()
                    stats["usage"] = acc.usage()
                return acc.text()
            if not reconnect:
                break  # stream ended without a terminal event and no reconnectable drop

        raise EngineError(
            "sse_exhausted",
            f"SSE stream disconnected after {max_reconnects} reconnect attempts",
        )
    finally:
        # The send task persists across attempts; cancel it once at the very end and await it
        # to completion so it can't outlive the invocation and push a late _SendFailed onto the
        # (now-unreferenced) queue. Its exception was already retrieved by _on_send_done.
        if send_task is not None:
            send_task.cancel()
            try:
                await send_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass


async def _await_subscription_ready(result_queue: asyncio.Queue, session_id: str) -> None:  # type: ignore[type-arg]
    """Block until opencode confirms the SSE subscription is live, before the turn is sent.

    opencode emits ``server.connected`` (OpenCodeStreamReady) as the first event on a fresh
    GET /event connection. Waiting for it ensures the turn's first events can't race ahead of
    server-side subscriber registration and be lost — an intermittent "no text appeared" that
    extra logging latency happens to hide. Bounded so a missing/renamed event can never hang
    the invocation: log and let the caller send anyway after 5s. An early SSE error surfaces
    as the first queued item and is re-raised (the caller's finally then cleans up).
    """
    from ach_agent.engine.events import OpenCodeStreamReady, _SendFailed

    try:
        first = await asyncio.wait_for(result_queue.get(), timeout=5.0)
    except TimeoutError:
        log.warning("SSE not confirmed within 5s — sending anyway", session_id=session_id)
        return
    # A send-POST failure that lands here (e.g. surfaced during a reconnect's gate) is unwrapped
    # to the original — same as the main consume loop — so callers see the real error, not the
    # _SendFailed wrapper.
    if isinstance(first, _SendFailed):
        raise first.original
    if isinstance(first, Exception):
        raise first
    # server.connected carries nothing to render and is consumed here; any other event
    # (shouldn't precede it) is preserved for the main loop.
    if not isinstance(first, OpenCodeStreamReady):
        result_queue.put_nowait(first)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _drain_logs(
    stream: asyncio.StreamReader,
    label: str,
    tail: collections.deque[str] | None = None,
) -> None:
    """Drain subprocess stdout/stderr to prevent PIPE deadlock (H-05).

    Accumulates a 50-line tail in the provided deque for error diagnostics.
    Without draining, the OS PIPE buffer (64KB) fills and the subprocess
    blocks writing, deadlocking the harness.
    """
    try:
        while True:
            line = await stream.readline()
            if not line:
                break
            msg = line.decode("utf-8", errors="replace").rstrip()
            if tail is not None and msg:
                tail.append(msg)
            # Log at debug level — results arrive via SSE, not stdout
            if msg:
                log.debug(f"[OC-{label.upper()}] {msg}")
    except Exception:  # noqa: BLE001
        pass


async def _process_group_kill(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM → 10s wait → SIGKILL for the subprocess process group (H-03).

    Requires start_new_session=True at launch time so the subprocess PID
    is the process group leader (Pitfall 3 prevention).

    Kills the entire process group so opencode child processes (tools, goroutines)
    cannot orphan and leak ports/memory (T-00-ORPHAN).

    CR-01: Guards against PID reuse after the process exits and is reaped.
    Resolves the real pgid via os.getpgid() rather than assuming pid==pgid.
    """
    if proc.returncode is not None:
        return  # already exited/reaped — PID may be recycled, do NOT killpg
    pid = proc.pid
    try:
        pgid = os.getpgid(pid)  # resolve real pgid; raises ProcessLookupError if gone
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=SHUTDOWN_TIMEOUT)
    except TimeoutError:
        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()
    log.debug("opencode subprocess stopped", pid=pid)

# SPDX-License-Identifier: Apache-2.0
"""Engine lifecycle: subprocess launch, readiness polling, invocation, shutdown.

Constraint: No router or Hermes imports (D-08, RTR-06).

Hardening implemented in 00-02:
  - H-01: 1MB SSE read buffer (in client.py)
  - H-02: SSE reconnect with bounded retry (in events.py)
  - H-03: Process-group kill (SIGTERM → 10s → SIGKILL) via _process_group_kill
  - H-05: stdout/stderr drain tasks (_drain_logs with 50-line tail, started at launch)
  - ENG-06: Startup deadline calls sys.exit(1), NOT raises
  - ENG-07: maxInvocationSeconds watchdog via asyncio.timeout + on_kill seam
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
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
    provider: str = "openai"
    model: str = "gpt-4o-mini"  # opencode validates model names; must be a known OpenAI model ID
    params: dict[str, object] = field(default_factory=dict)  # model params (temperature, …)
    system_prompt: str = ""
    steps: int = 50
    startup_timeout_seconds: int = 30
    shared_enabled: bool = False
    shared_ttl_seconds: int = 0
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

    @property
    def client(self) -> object:
        """Return the OpenCodeClient; raises if not yet launched."""
        if self._client is None:
            raise RuntimeError("ManagedServer not yet launched — call launch() first")
        return self._client


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def write_opencode_config(ephemeral_home: Path, config: EngineConfig) -> None:
    """Write opencode.json to ephemeral home before subprocess launch.

    Security (SEC-01 / T-00-EK / Pitfall 6): no secret is ever written. opencode points at
    the localhost model-proxy and the proxy injects the ek_; opencode.json carries only a
    dummy apiKey and the loopback baseURL.

    Security (T-00-TRACE): Secrets are never passed as CLI arguments — only config file.
    """
    config_dir = ephemeral_home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = ephemeral_home / "personality" / "system_prompt.txt"
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
    if mcp_block:
        oc_config["mcp"] = mcp_block
    (config_dir / "opencode.json").write_text(json.dumps(oc_config, indent=2), encoding="utf-8")
    log.debug(
        "opencode.json written",
        path=str(config_dir / "opencode.json"),
        provider=config.provider,
    )


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


def build_opencode_env(ephemeral_home: Path, config: EngineConfig) -> dict[str, str]:
    """Construct a minimal, allowlisted environment for the opencode subprocess.

    Clean-slate (SEC-01 / ek-hygiene): opencode does NOT inherit the harness env. It gets
    only ``_OPENCODE_ENV_ALLOWLIST`` (benign CLI basics), plus any var NAMES the operator
    lists in ``engine.forwardEnv`` (config.forward_env). The ek_ (ACH_TOKEN/ACH_API_KEY)
    is never present unless explicitly named — and it must not be, because the localhost
    model-proxy injects it and opencode points only at 127.0.0.1.

    Pinned last (override anything above): HOME/TMPDIR → the per-server ephemeral home;
    GIT_TERMINAL_PROMPT=0 so git never blocks a non-interactive subprocess on a prompt.
    """
    env: dict[str, str] = {
        name: os.environ[name] for name in _OPENCODE_ENV_ALLOWLIST if name in os.environ
    }
    # Operator-defined exceptions (engine.forwardEnv) — forwarded by name when present.
    for name in config.forward_env:
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    # Pinned hardening — last word, overrides any inherited/forwarded value.
    env["HOME"] = str(ephemeral_home)
    env["TMPDIR"] = str(ephemeral_home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


async def launch(port: int, ephemeral_home: Path, config: EngineConfig) -> ManagedServer:
    """Launch opencode serve in an isolated ephemeral home.

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

    write_opencode_config(ephemeral_home, config)

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
    env = build_opencode_env(ephemeral_home, config)

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

    server = ManagedServer(port=port, ephemeral_home=ephemeral_home)
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


async def send_message(server: ManagedServer, session_id: str, prompt: str) -> None:
    """Submit a prompt to an existing opencode session via POST /session/{id}/message.

    Wraps OpenCodeClient.send_message.
    """
    from ach_agent.engine.client import OpenCodeClient

    client = server._client
    if not isinstance(client, OpenCodeClient):
        raise RuntimeError("ManagedServer has no client")
    await client.send_message(session_id, prompt)


async def run_invocation(
    server: ManagedServer,
    session_id: str,
    prompt: str,
    terminal_retries: int,
    max_invocation_seconds: int,
    on_kill: Callable[[], None],
    free_form: bool = False,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
) -> dict[str, Any]:
    """Orchestrate: subscribe SSE → send prompt → consume → return the terminal object.

    ENG-07 / D-02 / D-03: Wrapped in asyncio.timeout(max_invocation_seconds).
    On TimeoutError:
      1. _process_group_kill(server.process) — genuine kill (D-03)
      2. ENGINE_WATCHDOG_KILLS.inc() — watchdog-kill metric
      3. on_kill() — injected seam: Phase 0 = FakeSlotManager; Phase 1 = real router
      4. raise InvocationTimeout(max_invocation_seconds)

    Engine NEVER imports the router — on_kill stays a plain Callable[[], None] (D-02).

    IMPORTANT: SSE subscription MUST happen before send_message because opencode
    emits session.idle on the SSE stream in real-time. If send_message completes
    before subscribe_events(), the session.idle event is missed and the consumer hangs.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import InvocationTimeout
    from ach_agent.engine.metrics import ENGINE_WATCHDOG_KILLS

    client = server._client
    if not isinstance(client, OpenCodeClient):
        raise RuntimeError("ManagedServer has no client")

    # opencode requires a session created via POST /session before /session/{id}/message
    # (sending to an arbitrary id → 500). Map the logical session_key → an opencode
    # session id, created once per key and reused for conversational continuity.
    oc_session_id = server._sessions.get(session_id)
    if oc_session_id is None:
        created = await client.create_session()
        oc_session_id = str(created.get("id", ""))
        if not oc_session_id:
            raise RuntimeError(f"opencode create_session returned no id: {created!r}")
        server._sessions[session_id] = oc_session_id

    try:
        async with asyncio.timeout(max_invocation_seconds):
            # Subscribe to SSE FIRST, then send the message.
            # This ensures we don't miss the session.idle event.
            accumulated_text = await consume_sse_after_send(
                client, oc_session_id, prompt, on_text=on_text, on_tool=on_tool
            )
    except TimeoutError:
        # D-03: must genuinely kill the subprocess
        log.warning(
            "invocation exceeded maxInvocationSeconds — killing",
            max_invocation_seconds=max_invocation_seconds,
            session_id=session_id,
        )
        proc = server._process
        if proc is not None:
            await _process_group_kill(proc)  # type: ignore[arg-type]
        # Emit watchdog-kill metric (D-03)
        ENGINE_WATCHDOG_KILLS.inc()
        # Call the injected release seam (D-02)
        on_kill()
        raise InvocationTimeout(max_invocation_seconds)

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
        accumulated_text = await consume_sse_after_send(client, oc_session_id, repair)
        obj = extract_terminal(accumulated_text)
    return obj if obj is not None else {"action": "none", "text": accumulated_text}


async def consume_sse_after_send(
    client: object,
    session_id: str,
    prompt: str,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
) -> str:
    """Subscribe to SSE, send the prompt, then consume to session.idle.

    The SSE subscription must happen before send_message because opencode emits
    events in real-time. This helper ensures correct ordering.

    ``on_text``: optional sink called with each new text suffix as the assistant's
    message.part.updated snapshots grow (so a console prints the reply live instead of
    waiting for the whole invocation).
    ``on_tool``: optional sink called with each tool-part lifecycle update (running /
    completed / error). Lets a channel show "running a tool" — important when a tool
    such as calendar auth_wait blocks for minutes after the visible text is produced,
    which otherwise looks like a frozen, unfinished reply.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import (
        EngineError,
        OpenCodeSessionError,
        OpenCodeSessionIdle,
        OpenCodeTextUpdate,
        OpenCodeToolUpdate,
        OpenCodeUserMessage,
        ReplyAccumulator,
        _consume_events_from_response,
    )

    assert isinstance(client, OpenCodeClient), "client must be OpenCodeClient"

    acc = ReplyAccumulator(on_text, on_tool)
    user_message_ids: set[str] = set()

    # Subscribe to SSE first
    resp = await client.subscribe_events()
    result_queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]

    # Start SSE reader task (listens from now)
    sse_task = asyncio.create_task(_consume_events_from_response(client, resp, result_queue))
    # send_task is created only after the readiness gate; declare it up-front so the finally
    # can clean up whether or not we got that far (e.g. the gate raises an early SSE error).
    send_task: asyncio.Task[None] | None = None

    terminal_error: EngineError | None = None
    terminal_idle = False

    try:
        # Gate the send on a confirmed-live subscription (inside the try so the finally still
        # cancels sse_task + releases resp if the gate hits an early SSE error — CR-03).
        await _await_subscription_ready(result_queue, session_id)

        # opencode's POST /session/{id}/message is SYNCHRONOUS — it returns only when the whole
        # turn has finished. Awaiting it before draining the queue would mean every event
        # (received live by sse_task) sits in the queue and is replayed in ONE burst after the
        # turn ends — so on_text/on_tool fire all at once at the end, and a blocking tool like
        # calendar auth_wait (~120s) freezes ALL output until then. Fire it concurrently and
        # consume the SSE stream live; session.idle (on the stream) is the real done signal.
        # A send failure is surfaced by pushing the exception onto the queue (handled below).
        send_task = asyncio.create_task(client.send_message(session_id, prompt))

        def _on_send_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
            if not t.cancelled() and t.exception() is not None:
                result_queue.put_nowait(t.exception())

        send_task.add_done_callback(_on_send_done)

        while True:
            try:
                item = await asyncio.wait_for(result_queue.get(), timeout=300.0)
            except TimeoutError:
                raise EngineError("sse_timeout", "SSE stream stalled for 300s")

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
            elif isinstance(event, OpenCodeSessionIdle):
                log.debug("session.idle received", session_id=session_id)
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
        # Cancel both background tasks and drain them CONCURRENTLY under a single 2s budget
        # (not 2s each) — the send task is normally already complete by session.idle but may
        # be left hanging by a slow tool. asyncio.wait never raises task exceptions; the send
        # exception was already retrieved by _on_send_done, the sse task swallows its own.
        pending = [t for t in (sse_task, send_task) if t is not None]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=2.0)
        try:
            await resp.release()
        except Exception:  # noqa: BLE001
            pass

    if terminal_error is not None:
        raise terminal_error
    if terminal_idle:
        return acc.text()

    raise EngineError("sse_exhausted", "SSE stream ended without session.idle")


async def _await_subscription_ready(result_queue: asyncio.Queue, session_id: str) -> None:  # type: ignore[type-arg]
    """Block until opencode confirms the SSE subscription is live, before the turn is sent.

    opencode emits ``server.connected`` (OpenCodeStreamReady) as the first event on a fresh
    GET /event connection. Waiting for it ensures the turn's first events can't race ahead of
    server-side subscriber registration and be lost — an intermittent "no text appeared" that
    extra logging latency happens to hide. Bounded so a missing/renamed event can never hang
    the invocation: log and let the caller send anyway after 5s. An early SSE error surfaces
    as the first queued item and is re-raised (the caller's finally then cleans up).
    """
    from ach_agent.engine.events import OpenCodeStreamReady

    try:
        first = await asyncio.wait_for(result_queue.get(), timeout=5.0)
    except TimeoutError:
        log.warning("SSE not confirmed within 5s — sending anyway", session_id=session_id)
        return
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

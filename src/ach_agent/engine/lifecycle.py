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
from typing import Any

import structlog

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
    work_dir: str = "/workspace"
    session_dir: str = "/var/lib/ach-agent/opencode/sessions"
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

    Security (SEC-01 / T-00-EK / Pitfall 6): The value of ACH_API_KEY is NEVER
    read into a Python variable here. Only the reference string "{env:ACH_API_KEY}"
    is written into the JSON file; opencode dereferences it at runtime.

    Security (T-00-TRACE): Secrets are never passed as CLI arguments — only config file.
    """
    config_dir = ephemeral_home / ".config" / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)

    prompt_path = ephemeral_home / "personality" / "system_prompt.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(config.system_prompt or "", encoding="utf-8")

    # {env:VAR} interpolation confirmed working in opencode v1.16.0 (RESEARCH Unknown 3)
    # ACH_API_KEY value never assigned to a Python variable — only the reference string.
    oc_config: dict[str, object] = {
        "autoupdate": False,
        "permission": "allow",
        "share": "disabled",
        "logLevel": "WARN",
        "model": f"{config.provider}/{config.model}",
        "enabled_providers": [config.provider],
        "provider": {
            config.provider: {
                "options": {
                    "apiKey": "{env:ACH_API_KEY}",  # ek_ dereferenced at runtime only
                    "baseURL": "{env:ACH_BASE_URL}",  # Hub/mock endpoint
                    **config.params,
                }
            }
        },
        "instructions": [str(prompt_path.resolve())],  # append-mode system prompt
        "agent": {
            "build": {
                "steps": config.steps,
            },
            "plan": {"disable": True},
        },
    }

    # D-02 / MEM-02: conditionally register memory MCP server ONLY when reachable.
    # Written pre-launch so the model either has memory tools or does not — never
    # receives a tool that can fail (harness-side enforcement of fail-open invariant).
    # SEC (T-04-22): endpoint URL only — ek_ bearer is never written to config files.
    if config.mcp_servers:
        oc_config["mcp"] = {
            "servers": {
                f"memory-{i}": {"type": "streamable-http", "url": url}
                for i, url in enumerate(config.mcp_servers)
            }
        }
    (config_dir / "opencode.json").write_text(json.dumps(oc_config, indent=2), encoding="utf-8")
    log.debug(
        "opencode.json written",
        path=str(config_dir / "opencode.json"),
        provider=config.provider,
    )


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

    # Build subprocess env: inherit os.environ + add hardening vars
    # ACH_BASE_URL and ACH_API_KEY are already in os.environ;
    # they are referenced via {env:...} in opencode.json — NOT re-injected here.
    #
    # Note on OPENCODE_SERVER_PASSWORD (Pitfall 5 deviation):
    # When set, opencode requires authentication for ALL routes including GET /app,
    # breaking the readiness probe (401). Do NOT set it; accept the warning in logs.
    # The server binds to 127.0.0.1 only.
    env: dict[str, str] = {
        **os.environ,
        "HOME": str(ephemeral_home),
        "TMPDIR": str(ephemeral_home),
        "GIT_TERMINAL_PROMPT": "0",
    }

    log.info(
        "launching opencode serve",
        port=port,
        binary=binary,
        ephemeral_home=str(ephemeral_home),
    )

    proc = await asyncio.create_subprocess_exec(
        binary,
        "serve",
        "--port",
        str(port),
        "--hostname",
        "127.0.0.1",
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

    try:
        async with asyncio.timeout(max_invocation_seconds):
            # Subscribe to SSE FIRST, then send the message.
            # This ensures we don't miss the session.idle event.
            accumulated_text = await consume_sse_after_send(client, session_id, prompt)
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

    log.info(
        "invocation complete",
        session_id=session_id,
        text_length=len(accumulated_text),
    )

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
        accumulated_text = await consume_sse_after_send(client, session_id, repair)
        obj = extract_terminal(accumulated_text)
    return obj if obj is not None else {"action": "none", "text": accumulated_text}


async def consume_sse_after_send(
    client: object,
    session_id: str,
    prompt: str,
) -> str:
    """Subscribe to SSE, send the prompt, then consume to session.idle.

    The SSE subscription must happen before send_message because opencode emits
    events in real-time. This helper ensures correct ordering.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import (
        EngineError,
        OpenCodeSessionError,
        OpenCodeSessionIdle,
        OpenCodeTextUpdate,
        OpenCodeUserMessage,
        _consume_events_from_response,
    )

    assert isinstance(client, OpenCodeClient), "client must be OpenCodeClient"

    accumulated: list[str] = []
    user_message_ids: set[str] = set()

    # Subscribe to SSE first
    resp = await client.subscribe_events()
    result_queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]

    # Start SSE reader task (listens from now)
    sse_task = asyncio.create_task(_consume_events_from_response(client, resp, result_queue))

    terminal_error: EngineError | None = None
    terminal_idle = False

    try:
        # CR-03: send_message is now inside the try block so the finally clause
        # always cancels sse_task and releases resp — even if send_message raises.
        await client.send_message(session_id, prompt)

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
                    accumulated.append(event.text)
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
        sse_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(sse_task), timeout=2.0)
        except (TimeoutError, asyncio.CancelledError):
            pass
        try:
            await resp.release()
        except Exception:  # noqa: BLE001
            pass

    if terminal_error is not None:
        raise terminal_error
    if terminal_idle:
        return "".join(accumulated)

    raise EngineError("sse_exhausted", "SSE stream ended without session.idle")


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

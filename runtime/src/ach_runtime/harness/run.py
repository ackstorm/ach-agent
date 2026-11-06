# SPDX-License-Identifier: MIT
"""The `run` role: a manifest-defined agent in one process, with the engine's TUI attached.

Single-unit topology (see harness/agent.py): not for production. The workspace is the
caller's directory; the TUI is the engine's own (opencode attaches to the session's server, pi takes the
session over once its RPC process has stopped).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import signal
import socket
import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.harness.agent import AGENT_FACADE_PORT, SINGLE_UNIT_WARNING, make_agent_assembly
from ach_runtime.harness.app import _retrieve_outcome
from ach_runtime.harness.boot import run_control
from ach_runtime.harness.local import LocalRoutingTransport, LocalSource
from ach_runtime.harness.refs import session_ref, workspace_name, workspace_ref
from ach_runtime.manifest import (
    KEY_ENV, LOCAL_NAMESPACE, TUI_CHANNEL, TUI_SESSION, TUI_TOKEN_ENV, TUI_WORKSPACE, URL_ENV,
    load_manifest, manifest_to_wire, secret_env_names,
)

log = logging.getLogger(__name__)

STARTUP_TIMEOUT_SECONDS = 180  # hydration + first engine start
SHUTDOWN_TIMEOUT_SECONDS = 60
WARMUP_PROMPT = "A developer attached a local terminal to this session. Reply with one word: ready."


class RunError(RuntimeError):
    """`run` could not bring the agent up for the TUI."""


def token_expiry(key: str) -> datetime | None:
    """The `exp` of a JWT-shaped key (an ach-cli login session token), read without
    verification and only for a warning; None for an `ek-` key or anything unreadable."""
    parts = key.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        return datetime.fromtimestamp(int(claims["exp"]), tz=UTC)
    except (ValueError, KeyError, TypeError):
        return None


def _outcome_error(outcome: asyncio.Future[Any]) -> str:
    """Why a finished warm-up run produced no session: its exception, or its RunStatus error."""
    if outcome.cancelled():
        return "cancelled"
    if (exc := outcome.exception()) is not None:
        return str(exc) or type(exc).__name__
    status = outcome.result()
    return getattr(status, "error", None) or f"state {getattr(status, 'state', 'unknown')}"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait(predicate, what: str, control: asyncio.Task[None], deadline: float):
    """Poll `predicate` until it returns a truthy value; fail if control dies or time runs out."""
    while time.monotonic() < deadline:
        if control.done():
            control.result()  # re-raises the control failure
            raise RunError(f"the agent stopped before {what}")
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(0.5)
    raise RunError(f"timed out waiting for {what}")


async def run_local(
    manifest_path: Path, *, workspace: Path, name: str | None = None, data_dir: Path,
    tui: Sequence[str] | None = None, log_path: Path | None = None, **assembly_kwargs: Any,
) -> int:
    manifest = load_manifest(manifest_path)
    base_url = os.environ.get(URL_ENV)
    if manifest.ach is not None and (not os.environ.get(KEY_ENV) or not base_url):
        raise RunError(f"no ACH credential: set {URL_ENV} and {KEY_ENV} or run `ach-cli login`")
    if missing := [env for env in secret_env_names(manifest) if not os.environ.get(env)]:
        raise RunError(f"the manifest's headers read unset variables: {', '.join(missing)}")
    base_dir = manifest_path.resolve().parent
    name = name or base_dir.name
    wire = manifest_to_wire(manifest, base_dir=base_dir, name=name, base_url=base_url)
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    config_path.write_text(json.dumps(wire), encoding="utf-8")
    os.environ.setdefault("ACH_SANDBOX_KEY", secrets.token_urlsafe(32))
    os.environ[TUI_TOKEN_ENV] = secrets.token_urlsafe(32)

    def say(message: str) -> None:
        os.write(terminal, f"ach-agent: {message}\n".encode())

    created: list[LocalSource] = []
    controls: list[Any] = []
    task: asyncio.Task[None] | None = None
    proc: asyncio.subprocess.Process | None = None
    terminal = os.dup(2)
    try:
        if log_path is not None:  # the TUI owns the terminal: harness and engine logs go to the file
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.dup2(log_fd, 2)
            os.close(log_fd)
        log.warning(SINGLE_UNIT_WARNING)
        port = _free_port()
        inner = make_agent_assembly(
            data_dir, LocalRoutingTransport(), created, workspace_dir=workspace, **assembly_kwargs
        )

        async def assembly(config):
            controls.append(control := await inner(config))
            return control

        task = asyncio.create_task(run_control(
            config_path, host="127.0.0.1", ingress_port=port,
            facade_host="127.0.0.1", facade_port=AGENT_FACADE_PORT, host_api=False, assembly=assembly,
        ))
        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=10) as client:

            async def ready() -> bool:
                try:
                    return (await client.get("/readyz")).status_code == 200
                except httpx.TransportError:
                    return False

            await _wait(ready, "the harness to be ready", task, deadline)

        say(f"{name} ready; starting the session")
        event = Event(event_id=f"tui:{uuid.uuid4().hex}", channel=TUI_CHANNEL, type="webhook",
                      source="generic", payload={"text": WARMUP_PROMPT})
        ticket = controls[0].admission.admit(
            RoutedEvent(event=event, workspace_key=TUI_WORKSPACE, session_key=TUI_SESSION)
        )
        # Admission owns both outcomes; retrieve a failure so it is never "unretrieved".
        ticket.started.add_done_callback(_retrieve_outcome)
        ticket.completed.add_done_callback(_retrieve_outcome)

        wref = workspace_ref(LOCAL_NAMESPACE, wire["agent"]["uid"], TUI_WORKSPACE)
        unit, sref = workspace_name(name, wref), session_ref(wref, TUI_SESSION)

        async def target():
            for source in created:
                engine = source.engine(unit)
                if engine is not None and (found := await engine.tui_command(sref)) is not None:
                    return found
            if ticket.completed.done():  # the warm-up run ended with no session to attach to
                raise RunError(f"the warm-up run failed: {_outcome_error(ticket.completed)}")
            return None

        command = await _wait(target, "the TUI session to start", task, deadline)
        if (expires := token_expiry(os.environ.get(KEY_ENV, ""))) is not None:
            minutes = int((expires - datetime.now(UTC)).total_seconds() // 60)
            say(f"warning: the ACH session token expires at {expires:%H:%M} UTC (in {minutes} min); "
                "restart ach-agent run after that")
        say(f"workspace {workspace}; quit the TUI to stop the agent")
        if command.clean_env:
            env = dict(command.env)
        else:
            hidden = set(secret_env_names(manifest))  # the TUI talks only to the session's server
            env = {k: v for k, v in os.environ.items() if not k.startswith("ACH_") and k not in hidden}
            env.update(command.env)
            env["HOME"] = str(data_dir / "tui-home")
            Path(env["HOME"]).mkdir(exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            *(tui or command.program), *command.args, env=env, cwd=workspace, stderr=terminal,
        )
        # The TUI ends the run, and so does the harness stopping under it (docker stop, a crash).
        tui_exit = asyncio.ensure_future(proc.wait())
        await asyncio.wait({tui_exit, task}, return_when=asyncio.FIRST_COMPLETED)
        if tui_exit.done():
            return tui_exit.result()
        cause = None if task.cancelled() else task.exception()
        say(f"the agent stopped under the TUI: {cause or 'shut down'}")
        return 1
    finally:
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except TimeoutError:
                proc.kill()
        if task is not None and not task.done():
            os.kill(os.getpid(), signal.SIGTERM)  # run_control owns SIGTERM: graceful drain
            try:
                await asyncio.wait_for(task, SHUTDOWN_TIMEOUT_SECONDS)
            except Exception:
                task.cancel()
        for source in created:  # stops engine processes; the workspace directory is kept
            await source.aclose()
        say("stopped")
        if log_path is not None:
            os.dup2(terminal, 2)
        os.close(terminal)

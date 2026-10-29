# SPDX-License-Identifier: MIT
"""Run deterministic GitLab webhook scripts without acquiring a Workspace or Session."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import signal
import tempfile
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from ach_runtime.channels.event import Event
from ach_runtime.channels.metrics import WEBHOOK_SCRIPT_FAILURES, WEBHOOK_SCRIPT_RUNS
from ach_runtime.config.schema import ScriptBlock
from ach_runtime.harness.handoff import _event_env

_LOG = structlog.get_logger(__name__)
_BASE_ENV = ("PATH", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR")
_TAIL_BYTES = 4096
_EK_PATTERN = re.compile(r"ek[-_][A-Za-z0-9_\-]+")
_SCRIPT_TRAMPOLINE = 's="$ACH_SCRIPT"; unset ACH_SCRIPT; eval "$s"'


class WebhookScriptFailed(RuntimeError):
    """A webhook script could not be spawned, timed out, or exited unsuccessfully."""


def _remaining_seconds(deadline_at: datetime) -> float:
    return (deadline_at - datetime.now(deadline_at.tzinfo)).total_seconds()


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        await proc.wait()


async def _read_tail(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    tail = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        tail.extend(chunk)
        if len(tail) > _TAIL_BYTES:
            del tail[:-_TAIL_BYTES]
            truncated = True
    return bytes(tail), truncated


def _redact(text: str, environ: Mapping[str, str], secret_names: tuple[str, ...]) -> str:
    text = _EK_PATTERN.sub("[REDACTED]", text)
    for name in ("GITLAB_TOKEN", *secret_names):
        if value := environ.get(name):
            text = text.replace(value, "[REDACTED]")
    return text


def _stderr_tail(stderr: bytes, environ: Mapping[str, str], secret_names: tuple[str, ...]) -> str:
    return _redact(stderr.decode("utf-8", "replace").strip(), environ, secret_names)


async def _execute_hook(
    script: str,
    payload: bytes,
    configured_timeout_seconds: float,
    deadline_at: datetime,
    *,
    cwd: Path,
    env: dict[str, str],
    on_started: Callable[[], None],
) -> tuple[int, bytes, bytes, bool]:
    env = {**env, "ACH_SCRIPT": script}
    spawn_timeout = _remaining_seconds(deadline_at)
    if spawn_timeout <= 0:
        raise TimeoutError("invocation deadline expired before webhook script spawn")
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_exec(
                "/bin/sh", "-eu", "-c", _SCRIPT_TRAMPOLINE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
                env=env,
                start_new_session=True,
            ),
            timeout=spawn_timeout,
        )
    except TimeoutError:
        raise TimeoutError("invocation deadline expired during webhook script spawn") from None
    except OSError as exc:
        raise WebhookScriptFailed(f"webhook script could not be started: {exc}") from exc

    if _remaining_seconds(deadline_at) <= 0:
        await _kill_process_group(proc)
        raise TimeoutError("invocation deadline expired before webhook script Started")

    try:
        on_started()
        timeout_seconds = min(configured_timeout_seconds, _remaining_seconds(deadline_at))
        if timeout_seconds <= 0:
            await _kill_process_group(proc)
            raise TimeoutError("invocation deadline expired before webhook script execution")

        async def communicate() -> tuple[int, bytes, bytes, bool]:
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            stdout_task = asyncio.create_task(_read_tail(proc.stdout))
            stderr_task = asyncio.create_task(_read_tail(proc.stderr))
            try:
                try:
                    proc.stdin.write(payload)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    proc.stdin.close()
                returncode = await proc.wait()
                stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
                return returncode, stdout[0], stderr[0], stdout[1] or stderr[1]
            finally:
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

        return await asyncio.wait_for(communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _kill_process_group(proc)
        WEBHOOK_SCRIPT_FAILURES.labels(reason="timeout").inc()
        raise WebhookScriptFailed(f"webhook script timed out after {timeout_seconds:g}s") from None
    except asyncio.CancelledError:
        await _kill_process_group(proc)
        raise
    except BaseException:
        await _kill_process_group(proc)
        raise


async def _remove_workspace(path: Path) -> None:
    def report_failure(function: Any, name: str, exc: BaseException) -> None:
        _LOG.warning("webhook-script: workspace removal failed", path=name, error=str(exc))

    await asyncio.to_thread(shutil.rmtree, path, onexc=report_failure)


async def run_webhook_script(
    cfg: ScriptBlock,
    event: Event,
    deadline_at: datetime,
    *,
    environ: Mapping[str, str],
    on_started: Callable[[], None],
) -> None:
    """Run the selected script in a private temporary directory and signal spawn as Started."""
    payload = json.dumps(event.payload, separators=(",", ":")).encode("utf-8") + b"\n"
    if _remaining_seconds(deadline_at) <= 0:
        raise TimeoutError("invocation deadline expired before webhook script spawn")
    configured_timeout = float(cfg.timeout_seconds)

    workspace = Path(tempfile.mkdtemp(prefix="webhook-script-"))
    status = "failed"
    secret_names = tuple(alias.env for alias in cfg.secret_env.values())
    try:
        env = {name: environ[name] for name in _BASE_ENV if name in environ}
        env.update(cfg.env)
        for name, alias in cfg.secret_env.items():
            value = environ.get(alias.env)
            if value:
                env[name] = value
            else:
                _LOG.warning("webhook-script: secret env var is unset", name=name, env_name=alias.env)
        env.update(_event_env(event))
        env.update({
            "ACH_WORKSPACE": str(workspace),
            "ACH_HANDOFF_DIR": str(workspace),
            "ACH_EVENT_ID": event.event_id,
            "ACH_CHANNEL": event.channel,
            "GIT_TERMINAL_PROMPT": "0",
        })

        with tempfile.TemporaryDirectory(prefix="ach-webhook-script-home-") as home:
            env["HOME"] = home
            started_at = asyncio.get_running_loop().time()
            _LOG.info(
                "webhook-script: script running",
                channel=event.channel,
                timeout_seconds=configured_timeout,
            )
            try:
                returncode, stdout, stderr, truncated = await _execute_hook(
                    cfg.script, payload, configured_timeout, deadline_at,
                    cwd=workspace, env=env, on_started=on_started,
                )
            except WebhookScriptFailed as exc:
                if "could not be started" in str(exc):
                    WEBHOOK_SCRIPT_FAILURES.labels(reason="spawn").inc()
                raise

            _LOG.debug(
                "webhook-script: script output",
                channel=event.channel,
                returncode=returncode,
                stdout=_redact(stdout.decode("utf-8", "replace"), environ, secret_names),
                stderr=_redact(stderr.decode("utf-8", "replace"), environ, secret_names),
                truncated=truncated,
            )
            if returncode != 0:
                WEBHOOK_SCRIPT_FAILURES.labels(reason="exit").inc()
                raise WebhookScriptFailed(
                    f"webhook script exited {returncode}: "
                    f"{_stderr_tail(stderr, environ, secret_names)}"
                )
            status = "ok"
            _LOG.info(
                "webhook-script: script complete",
                channel=event.channel,
                returncode=returncode,
                duration_ms=int((asyncio.get_running_loop().time() - started_at) * 1000),
            )
    finally:
        WEBHOOK_SCRIPT_RUNS.labels(channel=event.channel, status=status).inc()
        await _remove_workspace(workspace)

# SPDX-License-Identifier: Apache-2.0
"""Channel prepare/cleanup workspace hooks (CONTRACT §2).

The harness runs prepare on the LANE after the router admits each event and before
`pool.acquire` acquires or reuses its session engine, with `ACH_WORKSPACE` as cwd. Cleanup
runs best-effort from the workspace's parent when the reserved session is torn down: after
an acquired engine stops, or after prepare/engine-acquire failure before acquisition.
The canonical use is cloning and later removing the repo a merge-request event names, so
the agent gets a real `.git` checkout it never had to fetch — and never holds the credential
for.

Why here and not in the channel's HTTP handler: the pinned order is
`dedup → backpressure → lane`. Cloning before admit turns a redelivery flood into a
clone flood on events dedup was about to discard, and nothing bounds it
(`maxConcurrentInvocations` applies only after admit).

Three rules make the seam safe, and none of them are optional:

1. **The payload never reaches the script text.** The script is static config; every
   event-derived value arrives as an environment variable. There is no shell
   interpolation of attacker-controlled data, so there is no command injection.
2. **Event values are validated before they become env.** Non-scalars (the callables
   `delivery_context` carries) are dropped, non-printable values are dropped, and repo
   paths must match a strict slug regex with no `..` segment — a script builds its clone
   URL as `{configured base}/{ACH_EVENT_PROJECT_PATH}.git`, so that variable is a trust
   boundary.
3. **The script is never written to disk and never placed in argv.** The agent shares this
   uid; a script file inside the workspace would be a file the agent could rewrite between
   events and have the harness execute with the harness's secrets in env. `/proc/<pid>/cmdline`
   is world-readable, so the script text does not go there either — it arrives on stdin, or
   (when stdin carries the event payload) through the env, via `_SCRIPT_TRAMPOLINE`.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import tempfile
from pathlib import Path
from typing import Any

import structlog

from ach_agent.boot.paths import link_ach_state
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock, resolve_secret
from ach_agent.engine.metrics import (
    CLEANUP_FAILURES,
    PREPARE_FAILURES,
    WEBHOOK_SCRIPT_FAILURES,
    WEBHOOK_SCRIPT_RUNS,
)
from ach_agent.engine.sanitized_env import redact_text

log = structlog.get_logger(__name__)

# Path-hostile characters in a session_key (`42:7`, `owner/repo:9`) collapsed to '-'; the
# sha256 tail keeps two keys that slug identically on separate directories.
_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
# One printable-ASCII line. Rejects newlines, NULs and control bytes before they can be
# smuggled into the script's environment.
_PRINTABLE = re.compile(r"[\x20-\x7e]{1,512}")
# A forge repo path: `group/sub/project`. No scheme, no host, no userinfo, no '..'.
_REPO_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*(?:/[A-Za-z0-9._][A-Za-z0-9._-]*)*")
# delivery_context keys whose value a script is expected to interpolate into a URL.
_REPO_PATH_KEYS = frozenset({"project_path", "repo"})

# The base env the script inherits. Deliberately tiny (same spirit as the opencode
# clean-slate allowlist): a prepare script gets what a CLI needs to run and nothing else.
# Every credential it may use is named explicitly in `prepare.secretEnv`.
_BASE_ENV = ("PATH", "SHELL", "LANG", "LANGUAGE", "TZ")

# Hook output is diagnostic and potentially noisy, so retain only a bounded tail.
_HOOK_OUTPUT_TAIL_BYTES = 4096

# Runs the script when stdin is already taken by the event payload. The script travels in
# ACH_SCRIPT (env, owner-readable) instead of argv (/proc/<pid>/cmdline, world-readable),
# and is unset before eval so no grandchild process inherits it.
_SCRIPT_TRAMPOLINE = 's="$ACH_SCRIPT"; unset ACH_SCRIPT; eval "$s"'


class PrepareFailed(RuntimeError):
    """The prepare script exited non-zero, timed out, or could not be started.

    Raised inside engine_runner's try, so it takes the ordinary failure path: the reply
    future gets the exception, a2a's on_fail fires, and nothing is posted. Deliberately
    fail-CLOSED (unlike memory's fail-open probe): a review posted after a failed clone
    is a review of a repo that is not there, which is worse than no review at all.
    """


class WebhookScriptFailed(RuntimeError):
    """A deterministic webhook script failed before completing its event."""


class _HookSpawnFailed(RuntimeError):
    pass


class _HookTimedOut(RuntimeError):
    pass


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
        if len(tail) > _HOOK_OUTPUT_TAIL_BYTES:
            del tail[:-_HOOK_OUTPUT_TAIL_BYTES]
            truncated = True
    return bytes(tail), truncated


def _rmtree_failed(func: Any, path: str, exc: BaseException) -> None:
    """shutil.rmtree onexc hook — a workspace that cannot be removed must not be silent."""
    log.warning("hook: workspace removal failed", path=path, error=str(exc))


def _stderr_tail(stderr: bytes) -> str:
    """The script's stderr tail, safe to embed in an exception message.

    The log processors redact event_dict VALUES; an exception message reaches the output
    through log.exception's traceback, which structlog renders from exc_info after the
    chain has run. A script echoing `git clone https://oauth2:$TOKEN@…` on failure would
    otherwise print its credential verbatim, so the tail is redacted here at the source.
    """
    return redact_text(stderr.decode("utf-8", "replace").strip())


def workspace_dir(work_dir: str, session_key: str) -> Path:
    """The stable workspace path for a session_key under the engine workDir.

    Keyed by session_key, not by invocation: the pool already gives one agente per
    session_key, and that agente's cwd is fixed at launch — a per-invocation directory
    could never become the cwd of a warm-reused server. It also means the clone survives
    across events on the same MR, which is the whole point of the cache.

    Consequence for script authors: **the script re-runs against a populated directory**
    and must be idempotent (clone-or-fetch, not clone).
    """
    slug = _SLUG_UNSAFE.sub("-", session_key)[:64].strip("-.") or "s"
    return Path(work_dir) / f"{slug}-{hashlib.sha256(session_key.encode()).hexdigest()[:8]}"


def prepare_workspace(home: str, work_dir: str, session_key: str) -> Path:
    """Create the session's workspace and return it (becomes the engine cwd)."""
    ws = workspace_dir(work_dir, session_key)
    ws.mkdir(parents=True, exist_ok=True)
    # The agent's shell now sits one level below workDir, so re-link the hydration state
    # under the workspace — otherwise ./.ach-state (prompts, artifacts) resolves to nothing.
    link_ach_state(home, str(ws))
    return ws


def _event_value(key: str, value: Any) -> str | None:
    """Coerce one delivery_context value to a safe env string, or None to drop it.

    delivery_context is not a pure data bag — it also carries the on_complete/on_fail/
    on_text callables — so anything non-scalar is dropped rather than stringified.
    """
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int | float):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None
    if not _PRINTABLE.fullmatch(text):
        return None
    if key in _REPO_PATH_KEYS and not (_REPO_PATH.fullmatch(text) and ".." not in text.split("/")):
        log.warning(
            "prepare: dropping malformed repo path from the script env",
            key=key,
        )
        return None
    return text


def build_prepare_env(cfg: PrepareBlock, event: MessageEvent, workspace: Path) -> dict[str, str]:
    """Build the script's environment: base allowlist, operator vars, then pinned harness vars.

    Order matters. Operator `env`/`secretEnv` are applied BEFORE the harness vars so the
    `ACH_*` identity of the invocation is always authoritative — config cannot shadow it
    (the schema also rejects those names outright, this is the belt to that suspenders).
    """
    env: dict[str, str] = {name: os.environ[name] for name in _BASE_ENV if name in os.environ}
    env.update(cfg.env)
    for name, src in cfg.secret_env.items():
        value = resolve_secret(src)
        if value is None:
            # Fail closed: a missing credential must surface as the script's own failure
            # (`sh -u` on an unset var), never as an anonymous clone that half-works.
            log.warning("prepare: secret env var is unset", name=name, env_name=src.env)
            continue
        env[name] = value

    for key, value in event.delivery_context.items():
        text = _event_value(key, value)
        if text is not None:
            env[f"ACH_EVENT_{key.upper()}"] = text

    env["ACH_WORKSPACE"] = str(workspace)
    env["ACH_SESSION_KEY"] = event.session_key
    env["ACH_EVENT_ID"] = event.idempotency_key
    env["ACH_CHANNEL"] = event.channel_name
    # git must never block a non-interactive subprocess on a credential prompt, and HOME
    # is pinned into the workspace so git writes its config there and never reads the
    # harness user's ~/.gitconfig or ~/.git-credentials.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["HOME"] = str(workspace)
    return env


async def _execute_hook(
    script: str,
    timeout_seconds: int,
    *,
    cwd: Path,
    env: dict[str, str],
    stdin_payload: bytes | None = None,
) -> tuple[int, bytes, bytes, bool]:
    argv: tuple[str, ...]
    if stdin_payload is None:
        argv = ("/bin/sh", "-eu", "-s")
    else:
        argv = ("/bin/sh", "-eu", "-c", _SCRIPT_TRAMPOLINE)
        env = {**env, "ACH_SCRIPT": script}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise _HookSpawnFailed(str(exc)) from exc

    async def communicate() -> tuple[int, bytes, bytes, bool]:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        stdout_task = asyncio.create_task(_read_tail(proc.stdout))
        stderr_task = asyncio.create_task(_read_tail(proc.stderr))
        try:
            try:
                proc.stdin.write(script.encode() if stdin_payload is None else stdin_payload)
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

    try:
        result = await asyncio.wait_for(communicate(), timeout=timeout_seconds)
    except TimeoutError:
        await _kill_process_group(proc)
        raise _HookTimedOut from None
    except asyncio.CancelledError:
        await _kill_process_group(proc)
        raise
    return result


def _log_hook_output(
    hook: str,
    event: MessageEvent,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
    truncated: bool,
) -> None:
    log.debug(
        f"{hook}: script output",
        session_key=event.session_key,
        returncode=returncode,
        stdout=stdout.decode("utf-8", "replace"),
        stderr=stderr.decode("utf-8", "replace"),
        truncated=truncated,
    )


async def run_prepare(cfg: PrepareBlock, event: MessageEvent, workspace: Path) -> None:
    """Run the prepare script to completion; raise PrepareFailed on any bad outcome.

    The script arrives on stdin (`sh -eu -s`) so it never exists as a file the co-resident
    agent could rewrite, and so no part of it is visible in /proc/<pid>/cmdline. `-e` makes
    the first failing command fail the invocation; `-u` makes a missing credential loud.
    """
    env = build_prepare_env(cfg, event, workspace)
    started = asyncio.get_running_loop().time()
    try:
        log.info(
            "prepare: script running",
            session_key=event.session_key,
            workspace=str(workspace),
            timeout_seconds=cfg.timeout_seconds,
        )
        returncode, stdout, stderr, truncated = await _execute_hook(
            cfg.script,
            cfg.timeout_seconds,
            cwd=workspace,
            env=env,
        )
    except _HookSpawnFailed as exc:
        PREPARE_FAILURES.labels(reason="spawn").inc()
        raise PrepareFailed(f"prepare script could not be started: {exc}") from exc
    except _HookTimedOut:
        PREPARE_FAILURES.labels(reason="timeout").inc()
        raise PrepareFailed(f"prepare script timed out after {cfg.timeout_seconds}s") from None

    _log_hook_output("prepare", event, returncode, stdout, stderr, truncated)

    if returncode != 0:
        PREPARE_FAILURES.labels(reason="exit").inc()
        raise PrepareFailed(f"prepare script exited {returncode}: {_stderr_tail(stderr)}")

    log.info(
        "prepare: workspace ready",
        session_key=event.session_key,
        workspace=str(workspace),
        returncode=returncode,
        duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
    )


async def run_webhook_script(cfg: PrepareBlock, event: MessageEvent, work_dir: str) -> None:
    """Run a deterministic webhook handler with normalized JSON on stdin and no engine."""
    # Serialized BEFORE the workspace exists, so a payload that cannot be encoded leaves no
    # directory behind. ensure_ascii (the default) is what makes that total: json.loads
    # accepts a lone surrogate — a truncated emoji in a commit message — and encoding one as
    # UTF-8 raises. The trailing newline is load-bearing: without it `read -r line` returns 1
    # at EOF (and `sh -e` aborts the script), while `while read` drops the payload entirely.
    payload = json.dumps(event.payload, separators=(",", ":")).encode() + b"\n"
    base = Path(work_dir)
    base.mkdir(parents=True, exist_ok=True)
    workspace = Path(tempfile.mkdtemp(prefix="webhook-script-", dir=base))
    started = asyncio.get_running_loop().time()
    status = "failed"
    try:
        env = build_prepare_env(cfg, event, workspace)
        log.info(
            "webhook-script: script running",
            session_key=event.session_key,
            workspace=str(workspace),
            timeout_seconds=cfg.timeout_seconds,
        )
        try:
            returncode, stdout, stderr, truncated = await _execute_hook(
                cfg.script,
                cfg.timeout_seconds,
                cwd=workspace,
                env=env,
                stdin_payload=payload,
            )
        except _HookSpawnFailed as exc:
            WEBHOOK_SCRIPT_FAILURES.labels(reason="spawn").inc()
            raise WebhookScriptFailed(f"webhook script could not be started: {exc}") from exc
        except _HookTimedOut:
            WEBHOOK_SCRIPT_FAILURES.labels(reason="timeout").inc()
            raise WebhookScriptFailed(
                f"webhook script timed out after {cfg.timeout_seconds}s"
            ) from None

        _log_hook_output("webhook-script", event, returncode, stdout, stderr, truncated)
        if returncode != 0:
            WEBHOOK_SCRIPT_FAILURES.labels(reason="exit").inc()
            raise WebhookScriptFailed(f"webhook script exited {returncode}: {_stderr_tail(stderr)}")
        status = "ok"
        log.info(
            "webhook-script: script complete",
            session_key=event.session_key,
            returncode=returncode,
            duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
        )
    finally:
        # The only per-run signal this channel type emits: it writes no ach:sessions entry
        # (no engine turn to describe), so without it a script-only agent looks idle.
        WEBHOOK_SCRIPT_RUNS.labels(channel=event.channel_name, status=status).inc()
        # Off the event loop: the workspace can hold a full checkout, and rmtree is O(files)
        # of blocking syscalls on the same loop that serves uvicorn, every other lane and the
        # SSE readers. onexc (not ignore_errors) so a directory left behind is visible.
        await asyncio.to_thread(shutil.rmtree, workspace, onexc=_rmtree_failed)


async def run_cleanup(cfg: PrepareBlock, event: MessageEvent, workspace: Path) -> None:
    """Run the best-effort cleanup hook when a reserved session is torn down."""
    env = build_prepare_env(cfg, event, workspace)
    started = asyncio.get_running_loop().time()
    try:
        log.info(
            "cleanup: script running",
            session_key=event.session_key,
            workspace=str(workspace),
            timeout_seconds=cfg.timeout_seconds,
        )
        returncode, stdout, stderr, truncated = await _execute_hook(
            cfg.script,
            cfg.timeout_seconds,
            cwd=workspace.parent,
            env=env,
        )
    except _HookSpawnFailed as exc:
        CLEANUP_FAILURES.labels(reason="spawn").inc()
        log.warning("cleanup: script could not be started", error=str(exc))
        return
    except _HookTimedOut:
        CLEANUP_FAILURES.labels(reason="timeout").inc()
        log.warning(
            "cleanup: script timed out",
            session_key=event.session_key,
            timeout_seconds=cfg.timeout_seconds,
        )
        return

    _log_hook_output("cleanup", event, returncode, stdout, stderr, truncated)

    if returncode != 0:
        CLEANUP_FAILURES.labels(reason="exit").inc()
        log.warning(
            "cleanup: script exited nonzero",
            session_key=event.session_key,
            returncode=returncode,
        )
        return

    log.info(
        "cleanup: workspace hook complete",
        session_key=event.session_key,
        workspace=str(workspace),
        returncode=returncode,
        duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
    )

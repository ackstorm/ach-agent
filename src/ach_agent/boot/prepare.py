# SPDX-License-Identifier: Apache-2.0
"""channel.prepare — the per-invocation workspace hook (CONTRACT §2).

An operator-supplied `/bin/sh` script the harness runs on the LANE, after the router
admitted the event and before `pool.acquire`, with the invocation's workspace as cwd.
Its canonical use is cloning the repo a merge-request event names, so the agent gets a
real `.git` checkout it never had to fetch — and never holds the credential for.

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
3. **The script is fed on stdin, never written to disk.** The agent shares this uid; a
   script file inside the workspace would be a file the agent could rewrite between
   events and have the harness execute with the harness's secrets in env.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import re
import signal
from pathlib import Path
from typing import Any

import structlog

from ach_agent.boot.paths import link_ach_state
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock, resolve_secret
from ach_agent.engine.metrics import PREPARE_FAILURES

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

# Only the last of a failing script's stderr is logged — enough to diagnose, bounded so a
# runaway script cannot flood the log pipeline.
_STDERR_TAIL_CHARS = 2000


class PrepareFailed(RuntimeError):
    """The prepare script exited non-zero, timed out, or could not be started.

    Raised inside engine_runner's try, so it takes the ordinary failure path: the reply
    future gets the exception, a2a's on_fail fires, and nothing is posted. Deliberately
    fail-CLOSED (unlike memory's fail-open probe): a review posted after a failed clone
    is a review of a repo that is not there, which is worse than no review at all.
    """


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


async def run_prepare(cfg: PrepareBlock, event: MessageEvent, workspace: Path) -> None:
    """Run the prepare script to completion; raise PrepareFailed on any bad outcome.

    The script arrives on stdin (`sh -eu -s`) so it never exists as a file the co-resident
    agent could rewrite, and so no part of it is visible in /proc/<pid>/cmdline. `-e` makes
    the first failing command fail the invocation; `-u` makes a missing credential loud.
    """
    env = build_prepare_env(cfg, event, workspace)
    started = asyncio.get_running_loop().time()
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-eu",
            "-s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workspace),
            env=env,
            start_new_session=True,  # own process group, so a timeout kills the children too
        )
    except OSError as exc:
        PREPARE_FAILURES.labels(reason="spawn").inc()
        raise PrepareFailed(f"prepare script could not be started: {exc}") from exc

    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(cfg.script.encode()), timeout=cfg.timeout_seconds
        )
    except TimeoutError:
        # Kill the whole group: `git clone` spawns children that outlive a bare proc.kill().
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await proc.wait()
        PREPARE_FAILURES.labels(reason="timeout").inc()
        raise PrepareFailed(f"prepare script timed out after {cfg.timeout_seconds}s") from None

    if proc.returncode != 0:
        PREPARE_FAILURES.labels(reason="exit").inc()
        # The tail can contain whatever the script echoed; structlog's secret-redaction
        # processors cover every secretEnv name (collect_secret_env_names).
        raise PrepareFailed(
            f"prepare script exited {proc.returncode}: "
            f"{stderr.decode('utf-8', 'replace')[-_STDERR_TAIL_CHARS:].strip()}"
        )

    log.info(
        "prepare: workspace ready",
        session_key=event.session_key,
        workspace=str(workspace),
        duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
    )

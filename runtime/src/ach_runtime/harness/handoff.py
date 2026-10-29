# SPDX-License-Identifier: MIT
"""Run configured handoff hooks with the legacy selected environment contract."""
from __future__ import annotations

import asyncio
import os
import re
import signal
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class HandoffFailed(RuntimeError):
    """A handoff hook failed, timed out or could not be spawned."""


_PRINTABLE = re.compile(r"[\x20-\x7e]{1,512}")
_REPO_PATH = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*(?:/[A-Za-z0-9._][A-Za-z0-9._-]*)*")
_REPO_PATH_KEYS = frozenset({"project_path", "repo"})


def _event_value(key: str, value: Any) -> str | None:
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
    if key in _REPO_PATH_KEYS and (
        not _REPO_PATH.fullmatch(text) or ".." in text.split("/")
    ):
        return None
    return text


def _event_env(event: Any) -> dict[str, str]:
    """Project flat legacy context and validated adapter metadata into ACH_EVENT_* names."""
    env: dict[str, str] = {}

    def put(key: str, value: Any, *, name: str | None = None, validation_key: str | None = None) -> None:
        text = _event_value(validation_key or key, value)
        if text is not None:
            env[f"ACH_EVENT_{(name or key).upper()}"] = text

    context = event.context if isinstance(event.context, dict) else {}
    for key, value in context.items():
        if isinstance(key, str):
            put(key, value)

    gitlab = context.get("gitlab")
    if event.source == "gitlab" and isinstance(gitlab, dict):
        mapping = (
            ("projectId", "project_id", "PROJECT_ID"),
            ("projectPath", "project_path", "PROJECT_PATH"),
            ("mergeRequestIid", "mr_iid", "MR_IID"),
            ("issueIid", "issue_iid", "ISSUE_IID"),
            ("headSha", "head_sha", "HEAD_SHA"),
        )
        for key, validation_key, name in mapping:
            if key in gitlab:
                put(validation_key, gitlab[key], name=name, validation_key=validation_key)
        put("kind", event.type, name="KIND")
        if "mergeRequestIid" in gitlab:
            put("target_type", "mr", name="TARGET_TYPE")
        elif "issueIid" in gitlab:
            put("target_type", "issue", name="TARGET_TYPE")

    github = context.get("github")
    if event.source == "github" and isinstance(github, dict):
        if "repo" in github:
            put("repo", github["repo"], name="REPO")
        if "prNumber" in github:
            put("pr_number", github["prNumber"], name="PR_NUMBER")

    return env


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await proc.wait()


async def run_handoff(cfg: Any, event: Any, ticket: Any, workspace: Path, *, environ: Mapping[str, str]) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    env = {key: environ[key] for key in ("PATH", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR") if key in environ}
    env.update(cfg.env)
    for name, alias in cfg.secret_env.items():
        secret = environ.get(alias.env)
        if secret:
            env[name] = secret
    env.update(_event_env(event))
    env.update({
        "ACH_WORKSPACE": str(workspace),
        "ACH_HANDOFF_DIR": str(workspace),
        "ACH_SESSION_KEY": ticket.routed_event.session_key,
        "ACH_WORKSPACE_KEY": ticket.routed_event.workspace_key,
        "ACH_EVENT_ID": event.event_id,
        "ACH_CHANNEL": event.channel,
        "GIT_TERMINAL_PROMPT": "0",
    })
    with tempfile.TemporaryDirectory(prefix="ach-handoff-home-") as home:
        env["HOME"] = home
        remaining = (ticket.deadline_at - __import__("datetime").datetime.now(ticket.deadline_at.tzinfo)).total_seconds()
        timeout = min(float(cfg.timeout_seconds), remaining)
        if timeout <= 0:
            raise TimeoutError("invocation deadline expired before handoff")
        try:
            proc = await asyncio.create_subprocess_exec(
                "/bin/sh", "-eu", "-s", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=workspace, env=env, start_new_session=True,
            )
        except OSError as exc:
            raise HandoffFailed("handoff hook could not be started") from exc
        try:
            async with asyncio.timeout(timeout):
                _, stderr = await proc.communicate(cfg.script.encode("utf-8"))
        except (TimeoutError, asyncio.CancelledError):
            await _kill_tree(proc)
            raise
        if proc.returncode != 0:
            raise HandoffFailed(f"handoff hook exited {proc.returncode}")

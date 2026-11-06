# SPDX-License-Identifier: MIT
"""Drives pi's RPC mode: one `pi --mode rpc` process per session, JSONL over stdin and stdout.

Serves PiG and upstream pi alike. The image picks the distribution through PI_BIN.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import itertools
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ach_runtime.config.public import PublicConfig
from ach_runtime.engine.base import (
    EngineInvocation, EngineOutcome, EngineReceipt, NativeLaunchFailed, _kill_process_group,
)
from ach_runtime.engine.pi_config import PiConfig, build_pi_env, to_pi_config, write_pi_config
from ach_runtime.observability.runs import RunUsage

log = logging.getLogger(__name__)

PI_BIN = os.environ.get("PI_BIN", "pi")
PI_PACKAGE = os.environ.get("PI_PACKAGE", "@pi-in-go/pig")
PI_VERSION = os.environ.get("PI_VERSION", "0.7.0")
COMMAND_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 5.0
# asyncio's readline splits on LF only, as the protocol requires. A record holds a whole message.
STDOUT_LIMIT_BYTES = 64 * 1024 * 1024


@dataclass
class _PiInvocation:
    max_steps: int
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    outcome: EngineOutcome = "unknown"
    last_progress_at: float = field(default_factory=time.monotonic)
    turns: int = 0
    stop_reason: str | None = None
    error: str | None = None
    step_limited: bool = False
    cancel_requested: bool = False
    result: str | None = None
    usage: RunUsage | None = None
    stats_before: dict[str, Any] = field(default_factory=dict)


def _usage(before: dict[str, Any], after: dict[str, Any], turns: int) -> RunUsage:
    """`get_session_stats` is cumulative over the session, so one run is the difference."""
    old, new = before.get("tokens") or {}, after.get("tokens") or {}
    delta = lambda key: new.get(key, 0) - old.get(key, 0)  # noqa: E731
    return RunUsage(
        source="pi", basis="engine_estimate", input=delta("input"), output=delta("output"), reasoning=None,
        cache_read=delta("cacheRead"), cache_write=delta("cacheWrite"),
        cost_usd=after.get("cost", 0) - before.get("cost", 0), steps=turns,
    )


class _PiSession:
    def __init__(self, session_ref: str, home: Path, scratch: Path) -> None:
        self.session_ref, self.home, self.scratch = session_ref, home, scratch
        self.sid: str | None = None
        self.fingerprint: str | None = None
        self.invocations: dict[str, _PiInvocation] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._current: _PiInvocation | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.tui_args: list[str] = []
        self.tui_env: dict[str, str] = {}

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def _background(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def launch(self, args: list[str], env: dict[str, str], cwd: Path) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                PI_BIN, "--mode", "rpc", *args, cwd=cwd, env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=STDOUT_LIMIT_BYTES,
            )
        except FileNotFoundError as exc:
            raise NativeLaunchFailed(f"pi engine: {PI_BIN} is not in this image") from exc
        self._background(self._read())

    async def call(self, command: dict[str, Any], *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> dict[str, Any]:
        if not self.alive:
            raise NativeLaunchFailed("pi process is not running")
        request_id = str(next(self._ids))
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        assert self._proc is not None and self._proc.stdin is not None
        stdin = self._proc.stdin

        async def exchange() -> dict[str, Any]:
            stdin.write(json.dumps({"id": request_id, **command}).encode() + b"\n")
            await stdin.drain()
            return await future

        try:
            return await asyncio.wait_for(exchange(), timeout)
        except ConnectionError as exc:
            raise NativeLaunchFailed(f"pi stdin is closed: {exc}") from exc
        finally:
            self._pending.pop(request_id, None)

    async def _read(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while line := await self._proc.stdout.readline():
                record = json.loads(line)
                future = self._pending.get(record.get("id")) if record.get("type") == "response" else None
                if future is not None:
                    if not future.done():
                        future.set_result(record)
                else:
                    self._on_event(record)
        except Exception:
            # A line that is not JSON, or longer than the stdout limit, ends the stream: kill the
            # process so the wait below returns and the invocation settles.
            log.exception("pi session %s: unreadable stdout", self.session_ref)
            await self.kill()
        finally:
            await self._proc.wait()
            self._on_exit()

    def _on_event(self, record: dict[str, Any]) -> None:
        state = self._current
        if state is None:
            return
        state.last_progress_at = time.monotonic()
        kind = record.get("type")
        if kind == "message_end":
            message = record.get("message") or {}
            if message.get("role") == "assistant":
                state.stop_reason = message.get("stopReason")
                state.error = message.get("errorMessage") or state.error
        elif kind == "turn_end":
            state.turns += 1
            if state.turns >= state.max_steps and not state.step_limited:
                # ponytail: a soft limit that can overshoot by one turn. An exact one needs an extension.
                state.step_limited = True
                log.warning("pi session %s reached maxSteps (%d): aborting", self.session_ref, state.max_steps)
                self._background(self._abort())
        elif kind == "agent_settled":
            self._background(self._settle(state, aborted=bool(record.get("aborted"))))

    async def _abort(self) -> None:
        with contextlib.suppress(NativeLaunchFailed, TimeoutError):
            await self.call({"type": "abort"})

    async def _settle(self, state: _PiInvocation, *, aborted: bool) -> None:
        if aborted:
            outcome: EngineOutcome = "failed" if state.step_limited else "cancelled"
        elif state.stop_reason == "error":
            outcome = "failed"
            log.warning("pi session %s failed: %s", self.session_ref, state.error)
        else:
            outcome = "completed"
        try:
            if outcome == "completed":
                try:
                    state.result = ((await self.call({"type": "get_last_assistant_text"})).get("data") or {}).get("text")
                except (NativeLaunchFailed, TimeoutError):
                    outcome = "failed"
            with contextlib.suppress(NativeLaunchFailed, TimeoutError):
                stats = (await self.call({"type": "get_session_stats"})).get("data") or {}
                state.usage = _usage(state.stats_before, stats, state.turns)
        finally:
            self._finish(state, outcome)

    def _finish(self, state: _PiInvocation, outcome: EngineOutcome) -> None:
        """The first outcome wins: a process exit must not be overwritten by a late settle."""
        if state.settled.is_set():
            return
        state.outcome = outcome
        if self._current is state:
            self._current = None
        state.settled.set()

    def _on_exit(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(NativeLaunchFailed("pi process exited"))
        if self._current is not None:
            state = self._current
            self._finish(state, "cancelled" if state.cancel_requested else "failed")

    async def begin(self, state: _PiInvocation) -> None:
        state.stats_before = (await self.call({"type": "get_session_stats"})).get("data") or {}
        self._current = state

    async def stop(self) -> bool:
        """Closes stdin, which pi treats as an orderly shutdown, then kills the group if it stays."""
        if self._proc is None or self._proc.returncode is not None:
            return True
        assert self._proc.stdin is not None
        self._proc.stdin.close()
        try:
            await asyncio.wait_for(self._proc.wait(), STOP_TIMEOUT_SECONDS)
            return self._proc.returncode == 0
        except TimeoutError:
            await self.kill()
            return False

    async def kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            await _kill_process_group(self._proc.pid, confirm_timeout=STOP_TIMEOUT_SECONDS)


class PiEngine:
    family = "pi"

    def __init__(self) -> None:
        self._sessions: dict[str, _PiSession] = {}

    def _session_for(self, session_ref: str, home: Path, scratch: Path) -> _PiSession:
        return self._sessions.setdefault(session_ref, _PiSession(session_ref, home, scratch))

    def _state(self, invocation_id: str) -> _PiInvocation | None:
        for session in self._sessions.values():
            if (state := session.invocations.get(invocation_id)) is not None:
                return state
        return None

    async def _ensure_running(self, session: _PiSession, config: PiConfig, env: dict[str, str], cwd: Path, public: PublicConfig) -> None:
        fingerprint = hashlib.sha256(json.dumps([config.models, config.mcp, config.settings, config.args, config.system_prompt, env], sort_keys=True).encode()).hexdigest()
        if session.alive and session.fingerprint == fingerprint:
            return
        if session.alive and not await session.stop():
            raise NativeLaunchFailed("managed refresh requires a confirmed clean pi stop")
        write_pi_config(config, session.home)
        await session.launch([*config.args, *(["--session", session.sid] if session.sid else [])], env, cwd)
        try:
            reply = await session.call({"type": "get_state"}, timeout=public.engine.startup_timeout_seconds)
        except TimeoutError as exc:
            await session.kill()
            raise NativeLaunchFailed("pi engine: no answer within engine.startupTimeoutSeconds") from exc
        data = reply.get("data") or {}
        if not reply.get("success") or (data.get("model") or {}).get("id") != public.model.name:
            await session.kill()
            raise NativeLaunchFailed(f"pi engine: model {public.model.name} was not selected: {reply.get('error')}")
        if session.sid is None:
            session.sid = data["sessionId"]
        elif data.get("sessionId") != session.sid:
            await session.kill()
            raise NativeLaunchFailed(f"pi engine: session {session.sid} did not resume")
        session.fingerprint = fingerprint

    async def start(self, invocation: EngineInvocation, *, home: Path, scratch: Path) -> EngineReceipt:
        session = self._session_for(invocation.session_ref, home, scratch)
        home.mkdir(parents=True, exist_ok=True)
        scratch.mkdir(parents=True, exist_ok=True)
        public = invocation.public
        try:
            config = to_pi_config(
                public, home=home, catalog=invocation.catalog, mcp=invocation.mcp,
                resolved_system_prompt=invocation.resolved_system_prompt,
                provider_base_url_override=invocation.provider_base_url_override,
            )
        except ValueError as exc:
            raise NativeLaunchFailed(str(exc)) from exc
        env = build_pi_env(home=home, scratch=scratch, approved_env=invocation.engine_env)
        state = _PiInvocation(max_steps=public.limits.max_steps)
        session.invocations[invocation.invocation_id] = state
        session.tui_args, session.tui_env = config.args, env
        try:
            await self._ensure_running(session, config, env, invocation.workspace_dir, public)
            await session.begin(state)
        except BaseException:
            session._finish(state, "failed")  # never left unsettled: cancel() waits on it
            raise
        state.outcome = "running"
        try:
            reply = await session.call({"type": "prompt", "message": invocation.prompt})
        except (NativeLaunchFailed, TimeoutError) as exc:
            raise NativeLaunchFailed(f"prompt send receipt unknown: {exc}", ambiguous=True) from exc
        if not reply.get("success"):
            session._finish(state, "failed")
            raise NativeLaunchFailed(f"prompt rejected: {reply.get('error')}")
        assert session.sid is not None
        return EngineReceipt(native_session_id=session.sid, input_id=invocation.invocation_id)

    async def status(self, invocation_id: str) -> EngineOutcome:
        state = self._state(invocation_id)
        return state.outcome if state is not None else "unknown"

    def last_progress(self, invocation_id: str) -> float | None:
        state = self._state(invocation_id)
        return state.last_progress_at if state is not None else None

    def result(self, invocation_id: str) -> str | None:
        state = self._state(invocation_id)
        return state.result if state is not None and state.outcome == "completed" else None

    def usage(self, invocation_id: str) -> RunUsage | None:
        state = self._state(invocation_id)
        return state.usage if state is not None else None

    async def wait_settled(self, invocation_id: str) -> EngineOutcome:
        state = self._state(invocation_id)
        if state is None:
            return "unknown"
        await state.settled.wait()
        return state.outcome

    async def close(self) -> None:
        await asyncio.gather(*(session.stop() for session in self._sessions.values()))

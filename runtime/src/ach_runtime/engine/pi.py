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
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ach_runtime.config.public import PublicConfig
from ach_runtime.engine.base import (
    CANCEL_KILL_BOUND_SECONDS, SNAPSHOT_VERSION, EngineInvocation, EngineOutcome, EngineReceipt, NativeLaunchFailed,
    RestoreMismatch, SnapshotManifest, TuiCommand, _HaltBudget, _OwnedProcessGroup, check_manifest, install_restored_home,
)
from ach_runtime.engine.pi_config import PiConfig, build_pi_env, to_pi_config, write_pi_config
from ach_runtime.observability.runs import RunUsage

log = logging.getLogger(__name__)

PI_BIN = os.environ.get("PI_BIN", "pi")
PI_PACKAGE = os.environ.get("PI_PACKAGE", "@pi-in-go/pig")
PI_VERSION = os.environ.get("PI_VERSION", "0.7.0")
COMMAND_TIMEOUT_SECONDS = 30.0
STOP_TIMEOUT_SECONDS = 5.0
OWNERSHIP_POLL_SECONDS = 0.05
# The service stops observing at the force deadline: forcing slightly before it lets the kill land inside the window.
FORCE_MARGIN_SECONDS = 0.05
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
        self._owned: _OwnedProcessGroup | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._current: _PiInvocation | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self.tui_args: list[str] = []
        self.tui_env: dict[str, str] = {}

    @property
    def running(self) -> bool:
        """The process `pi` was launched as has not exited."""
        return self._proc is not None and self._proc.returncode is None

    @property
    def alive(self) -> bool:
        """Pi, or anything it started, still runs. PiG's launcher can die while its native binary lives on, and
        tools run in groups of their own, so the launched process alone does not answer this."""
        if self._owned is None:
            return False
        # Nothing tracked means no /proc: the launched process is all there is to ask.
        return bool(self._owned._live()) if self._owned._owned else self.running

    def _background(self, coro: Any) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def launch(self, args: list[str], env: dict[str, str], cwd: Path) -> None:
        try:
            proc = await asyncio.create_subprocess_exec(
                PI_BIN, "--mode", "rpc", *args, cwd=cwd, env=env, start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, limit=STDOUT_LIMIT_BYTES,
            )
        except FileNotFoundError as exc:
            raise NativeLaunchFailed(f"pi engine: {PI_BIN} is not in this image") from exc
        self._proc, self._owned = proc, _OwnedProcessGroup(proc.pid)
        self._background(self._read(proc, self._owned))

    async def call(self, command: dict[str, Any], *, timeout: float = COMMAND_TIMEOUT_SECONDS) -> dict[str, Any]:
        if not self.running:
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

    async def _observe(self, owned: _OwnedProcessGroup) -> None:
        """Records descendants by PID and start time while pi runs: a tool is found while its parent is
        alive, before it reparents, so `kill` can still reach it after pi is gone."""
        while True:
            owned._refresh()
            await asyncio.sleep(OWNERSHIP_POLL_SECONDS)

    async def _read(self, proc: asyncio.subprocess.Process, owned: _OwnedProcessGroup) -> None:
        assert proc.stdout is not None
        observer = asyncio.ensure_future(self._observe(owned))
        try:
            while line := await proc.stdout.readline():
                if proc is not self._proc:  # a replacement owns the session: this stream is stale
                    continue
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
            await owned.kill_and_confirm(confirm_timeout=STOP_TIMEOUT_SECONDS, force_immediately=True)
        finally:
            await proc.wait()
            # Tools run in groups of their own: they die with pi only through the owned-process kill.
            if not await owned.kill_and_confirm(confirm_timeout=STOP_TIMEOUT_SECONDS, force_immediately=True):
                log.warning("pi session %s: processes started by pi outlived it", self.session_ref)
            observer.cancel()
            if proc is self._proc:
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
                # ponytail: a soft limit that can overshoot by a turn or two; the abort races the turn loop. An exact one needs an extension.
                state.step_limited = True
                log.warning("pi session %s reached maxSteps (%d): aborting", self.session_ref, state.max_steps)
                self._background(self._abort())
        elif kind == "agent_settled":
            self._background(self._settle(state, aborted=bool(record.get("aborted"))))

    async def _abort(self, timeout: float = COMMAND_TIMEOUT_SECONDS) -> None:
        with contextlib.suppress(NativeLaunchFailed, TimeoutError):
            await self.call({"type": "abort"}, timeout=timeout)

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
        if not state.settled.is_set():  # a cancel can land while the stats call is in flight
            self._current = state

    async def stop(self) -> bool:
        """Closes stdin, which pi treats as an orderly shutdown, then kills what stays. True only for a
        clean exit that leaves nothing of pi's running."""
        clean = True
        if self.running:
            assert self._proc is not None and self._proc.stdin is not None
            self._proc.stdin.close()
            try:
                await asyncio.wait_for(self._proc.wait(), STOP_TIMEOUT_SECONDS)
                clean = self._proc.returncode == 0
            except TimeoutError:
                clean = False
        if self.alive:
            await self.kill()
            return False
        return clean

    async def kill(self) -> bool:
        """SIGKILLs everything pi started and returns whether it is confirmed gone. Acts on the owned
        processes, not on the launched one: a dead launcher can leave its native binary running."""
        if self._owned is None:
            return True
        return await self._owned.kill_and_confirm(confirm_timeout=STOP_TIMEOUT_SECONDS, force_immediately=True)


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
        if session.running and session.fingerprint == fingerprint:
            return
        if session.alive:
            was_running = session.running
            clean = await session.stop()
            if session.alive or (was_running and not clean):
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

    async def tui_command(self, session_ref: str) -> TuiCommand | None:
        """pi has no attach: the TUI and the agent loop are one process, so the TUI takes the
        session over once the RPC process has stopped. None until a turn has completed and while
        one runs, so a failed warm-up reaches `run`'s own failure path."""
        session = self._sessions.get(session_ref)
        if session is None or session.sid is None:
            return None
        states = session.invocations.values()
        if not all(state.settled.is_set() for state in states) or not any(state.outcome == "completed" for state in states):
            return None
        if not await session.stop():
            raise NativeLaunchFailed("cannot open the TUI: the pi process did not stop cleanly")
        return TuiCommand(
            program=[PI_BIN], args=[*session.tui_args, "--session", session.sid],
            env=session.tui_env, clean_env=True,
        )

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
        if state.settled.is_set():
            raise NativeLaunchFailed("invocation was cancelled before its prompt was sent")
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

    async def cancel(self, invocation_id: str, reason: str, *, _halt_budget: _HaltBudget | None = None) -> None:
        for session in self._sessions.values():
            state = session.invocations.get(invocation_id)
            if state is None:
                continue
            state.cancel_requested = True
            log.info("pi session %s cancelling: %s", session.session_ref, reason)
            if session._current is not state:  # settled, or not yet at its prompt
                session._finish(state, "cancelled")
                return
            deadline = (_halt_budget.force_deadline - FORCE_MARGIN_SECONDS) if _halt_budget else time.monotonic() + CANCEL_KILL_BOUND_SECONDS
            if session.running:
                await session._abort(max(0.0, deadline - time.monotonic()))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(state.settled.wait(), max(0.0, deadline - time.monotonic()))
            if not state.settled.is_set():
                if _halt_budget is not None:
                    _halt_budget.mark_forced()
                confirmed = await session.kill()
                session._finish(state, "cancelled")
                if not confirmed:
                    raise NativeLaunchFailed(f"pi processes for {invocation_id} did not confirm exit")
            return

    async def suspend(self, session_ref: str) -> bool:
        session = self._sessions.get(session_ref)
        return True if session is None else await session.stop()

    async def snapshot(
        self, session_ref: str, *, agent: dict[str, str], workspace_ref: str, registry: dict[str, Any],
    ) -> SnapshotManifest:
        session = self._sessions.get(session_ref)
        if session is None or session.sid is None:
            raise NativeLaunchFailed(f"no native session to snapshot for {session_ref}")
        if session.alive:
            raise NativeLaunchFailed("snapshot requires a stopped pi process; call suspend() first")
        manifest = SnapshotManifest(
            snapshot_version=SNAPSHOT_VERSION, engine_family=self.family, engine_package=PI_PACKAGE,
            engine_version=PI_VERSION, agent=agent, workspace_ref=workspace_ref, session_ref=session_ref,
            native_session_id=session.sid, workspace_path=str(registry.get("workspace_path", "")), registry=registry,
        )
        (session.home / ".ach-registry.json").write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
        return manifest

    async def retire_session(self, session_ref: str) -> None:
        session = self._sessions.get(session_ref)
        if session is None:
            return
        if session.alive:
            raise NativeLaunchFailed(f"retire requires a stopped pi process for {session_ref}")
        session.sid = None
        session.fingerprint = None

    async def restore(
        self, session_ref: str, manifest: SnapshotManifest, *, expected: dict[str, str], public: PublicConfig,
        engine_env: dict[str, str], managed_agent: str = "build", home: Path | None = None, scratch: Path | None = None,
        restored_home: Path | None = None, in_place: bool = False, deadline_at: datetime | None = None,
    ) -> None:
        """Relaunches pi on the saved session id and requires it to answer for that id.

        The config written here carries no catalog: the next `start` rewrites it, because the
        fingerprint differs, and relaunches pi on the same session."""
        check_manifest(manifest, expected)
        session = self._sessions.get(session_ref)
        if session is None:
            if home is None or scratch is None:
                raise RestoreMismatch(f"no reserved session state to restore into for {session_ref}")
            session = self._session_for(session_ref, home, scratch)
        if session.alive:
            raise RestoreMismatch(f"restore requires a stopped pi process for {session_ref}")
        if restored_home is None and not in_place:
            raise RestoreMismatch("restore requires a staged HOME or an in-place one")
        if restored_home is not None and not restored_home.is_dir():
            raise RestoreMismatch("staged HOME payload does not exist")
        previous = install_restored_home(session.home, restored_home) if restored_home is not None else None
        config = to_pi_config(public, home=session.home, catalog=None, mcp=None, resolved_system_prompt=None, lookup_only_prompt=True)
        env = build_pi_env(home=session.home, scratch=session.scratch, approved_env=engine_env)
        session.tui_args, session.tui_env = config.args, env
        session.sid, session.fingerprint = manifest.native_session_id, None
        try:
            await self._ensure_running(session, config, env, Path(manifest.workspace_path), public)
        except (NativeLaunchFailed, TimeoutError) as exc:
            await session.kill()
            session.sid = None
            if previous is not None:
                shutil.rmtree(session.home, ignore_errors=True)
                previous.rename(session.home)
            raise RestoreMismatch(f"pi session {manifest.native_session_id} did not resume: {exc}") from exc
        if previous is not None:
            shutil.rmtree(previous, ignore_errors=True)

    async def close(self) -> None:
        await asyncio.gather(*(session.stop() for session in self._sessions.values()))

# SPDX-License-Identifier: MIT
"""ExecutionService: start/status/list_runs/cancel over a retained invocation registry
(contract §§3-4), plus session snapshot/restore orchestration (contract §§4,6).

Review round 1 fix (753c228 -> this revision) restructures `start()` around a detached
owner task: preparation/native-send always runs in its own `asyncio.Task`, and every
caller — the first admitter and every identical-payload retry — only ever
`asyncio.shield`s the shared `started` Future. A caller being cancelled (e.g. an HTTP
disconnect) can therefore never cancel the owner's in-flight native send (finding #2).

The per-session `asyncio.Lock` is acquired by the owner task and released only once the
invocation reaches a terminal native outcome (`_hold_until_settled`), not merely once
native admission succeeds — a second invocation for the same session cannot enqueue while
the first is still running (finding #3). The original UTC deadline is checked before every
await in the owner's path, not only after `Started` (finding #1); a deadline that fires
*after* `Started` is a timeout -> `failed`, never relabelled `cancelled`, and never cancels
its own watchdog task (the old self-cancellation bug — fixed by giving the watchdog its own
`_timeout_cancel` path instead of routing through the public `cancel()`).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from pydantic import JsonValue

from ach_runtime.config.public import PublicConfig
from ach_runtime.config.schema import PromptBlock
from ach_runtime.engine.opencode import (
    CANCEL_KILL_BOUND_SECONDS,
    EngineOutcome,
    EngineReceipt,
    SnapshotManifest,
    _HaltBudget,
)
from ach_runtime.execution.sessions import (
    HookResult,
    SessionHookFailed,
    SessionRegistry,
    redact_facade_tokens,
    run_gate_hook,
    run_hook,
)
from ach_runtime.execution.wire import RunState, RunStatus, StartRequest, Started
from ach_runtime.facades._mcp_session import mcp_session

MAX_RETAINED_RUNS = 1024
# sessionStart exit code meaning "decided not to run": the invocation ends `skipped`.
SKIP_EXIT_CODE = 78
_HOOK_ERROR_TAIL = 500
DEFAULT_WORKSPACE_DIR = Path("/workspace")  # root path ruling: fixed image/CLI default
# OpenCode lists a remote MCP server's tools once, when it connects, and never re-lists unless
# the server pushes `tools/list_changed`: a server still starting (0 tools) leaves the whole
# run without its tools. Probe each configured server before the send; sleep between tries.
MCP_TOOLS_BACKOFF_SECONDS = (2.0, 6.0)
MCP_TOOLS_PROBE_TIMEOUT_SECONDS = 4.0


class ConflictingStart(ValueError):
    """Same `invocation_id`, different request fields — the contract never joins these."""


class WorkspaceMismatch(ValueError):
    """The admitted Workspace does not belong to this execution process."""


class CapacityExceeded(RuntimeError):
    """The terminal-run registry is full and nothing is old enough to evict (finding #14)."""


class Engine(Protocol):
    async def start(
        self, invocation: object, *, home: Path, scratch: Path, password: str | None = None
    ) -> object: ...

    async def status(self, invocation_id: str) -> EngineOutcome: ...

    async def wait_settled(self, invocation_id: str) -> EngineOutcome: ...

    def result(self, invocation_id: str) -> str | None: ...

    async def cancel(
        self, invocation_id: str, reason: str, *, _halt_budget: _HaltBudget | None = None
    ) -> None: ...

    async def suspend(self, session_ref: str) -> bool: ...

    async def retire_session(self, session_ref: str) -> None: ...

    async def snapshot(
        self, session_ref: str, *, agent: dict[str, str], workspace_ref: str, registry: dict[str, Any]
    ) -> SnapshotManifest: ...

    async def restore(
        self,
        session_ref: str,
        manifest: SnapshotManifest,
        *,
        expected: dict[str, str],
        public: PublicConfig,
        engine_env: dict[str, str],
        managed_agent: str = "build",
        home: Path | None = None,
        scratch: Path | None = None,
        restored_home: Path | None = None,
        deadline_at: datetime | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


@dataclass
class _Invocation:
    request: StartRequest
    public: PublicConfig  # captured at admission — immune to a later configure() (finding #8)
    run_id: str
    started: asyncio.Future[Started]
    received_at: float = field(default_factory=time.monotonic)
    catalog: dict[str, Any] | None = None
    terminal: bool = False
    state: RunState = "preparing"
    error: str | None = None
    result: JsonValue = None
    terminal_at: float | None = None
    owner_task: asyncio.Task[None] | None = None
    deadline_task: asyncio.Task[None] | None = None
    settle_task: asyncio.Task[None] | None = None
    cancel_task: asyncio.Task[None] | None = None
    reservation: asyncio.Lock | None = None
    receipt: EngineReceipt | None = None
    # True from the instant the native send is no longer definitely unsent (finding:
    # "releases without confirmed native halt") — a cleanup claimed after this point must
    # confirm halt via `engine.cancel()` rather than assume nothing was accepted.
    possible_send: bool = False
    # The single retained cleanup's shared clock record (review focus 5) — created once, by
    # whichever trigger claims cleanup first (`_ensure_cleanup`).
    cleanup_budget: _HaltBudget | None = None
    # I1: the two retained cleanup tasks themselves — created fresh by every claimed
    # cleanup and never replaced, so a bounded, non-cancelling observation (I1/I2) can
    # inspect both identities and results even after the shared deadline expires.
    engine_cancel_task: asyncio.Task[None] | None = None
    owner_stop_task: asyncio.Task[None] | None = None


def _build_fixture_invocation(
    request: StartRequest, public: PublicConfig, workspace_dir: Path
) -> object:
    from ach_runtime.engine.opencode import EngineInvocation

    system_prompt = public.prompt.system if public.prompt is not None else None
    resolved_system_prompt = None
    if system_prompt is not None and system_prompt.type in ("file", "ach"):
        resolved_system_prompt = _resolve_system_prompt(
            public.prompt, workspace_dir / ".ach-state"
        )

    return EngineInvocation(
        invocation_id=request.invocation_id,
        session_ref=request.session_ref,
        workspace_dir=workspace_dir,
        prompt=request.prompt,
        public=public,
        engine_env=dict(public.engine.env),
        resolved_system_prompt=resolved_system_prompt,
        deadline_at=request.deadline_at,
    )


def _resolve_system_prompt(prompt_block: PromptBlock | None, state_dir: Path) -> str:
    """Resolve the legacy text/file/ACH system prompt forms under `.ach-state`."""
    if prompt_block is None or prompt_block.system is None:
        return ""
    system = prompt_block.system
    if system.type == "text":
        return str(system.text)

    root = state_dir.resolve()
    if system.type == "file":
        target = (root / str(system.file)).resolve()
    else:
        prompt_dir = (root / "prompts" / str(system.ach)).resolve()
        if not prompt_dir.is_relative_to(root) or not prompt_dir.is_dir():
            raise ValueError("prompt.system.ach is not hydrated under .ach-state/prompts")
        if system.file:
            target = (prompt_dir / str(system.file)).resolve()
        else:
            files = sorted(path for path in prompt_dir.rglob("*") if path.is_file())
            if len(files) != 1:
                raise ValueError(
                    "prompt.system.ach needs an explicit file when its directory does not "
                    "contain exactly one file"
                )
            target = files[0].resolve()
    if not target.is_relative_to(root):
        raise ValueError("prompt.system file escapes .ach-state")
    if not target.is_file():
        raise ValueError("prompt.system file was not found under .ach-state")
    return target.read_text(encoding="utf-8")


async def _count_mcp_tools(url: str, headers: dict[str, str] | None) -> int:
    async with asyncio.timeout(MCP_TOOLS_PROBE_TIMEOUT_SECONDS):
        async with mcp_session(url, headers) as session:
            return len((await session.list_tools()).tools)


async def _require_mcp_tools(catalog: dict[str, Any] | None) -> None:
    """Raise unless every configured MCP server lists at least one tool, retrying a server
    that lists none (or cannot be reached) up to `len(MCP_TOOLS_BACKOFF_SECONDS)` times."""
    mcp = (catalog or {}).get("mcp") or {}
    pending = {
        name: entry
        for name, entry in mcp.items()
        if isinstance(entry, dict) and isinstance(entry.get("url"), str)
    }
    reasons: dict[str, str] = {}
    for delay in (0.0, *MCP_TOOLS_BACKOFF_SECONDS):
        await asyncio.sleep(delay)
        counts = await asyncio.gather(
            *(_count_mcp_tools(e["url"], e.get("headers")) for e in pending.values()),
            return_exceptions=True,
        )
        for name, count in zip(list(pending), counts):
            while isinstance(count, BaseExceptionGroup):  # the MCP SDK's task-group wrapper
                count = count.exceptions[0]
            if isinstance(count, int) and count > 0:
                del pending[name]
            else:
                reasons[name] = "0 tools" if isinstance(count, int) else (
                    redact_facade_tokens(f"{type(count).__name__}: {count}")
                )
        if not pending:
            return
    raise RuntimeError(
        "mcp tools unavailable: "
        + ", ".join(f"{name} ({reasons[name]})" for name in sorted(pending))
    )


def _remaining_seconds(deadline_at: datetime) -> float:
    return max(0.0, (deadline_at - datetime.now(timezone.utc)).total_seconds())


class ExecutionService:
    def __init__(
        self,
        *,
        engine: Engine,
        sessions: SessionRegistry,
        workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
        build_invocation: Callable[
            [StartRequest, PublicConfig, Path], object
        ] = _build_fixture_invocation,
        workspace_ref: str | None = None,
    ) -> None:
        self._engine = engine
        self._sessions = sessions
        self._workspace_dir = workspace_dir
        self._build_invocation = build_invocation
        self._workspace_ref = workspace_ref
        self._invocations: dict[str, _Invocation] = {}
        self._public: PublicConfig | None = None
        self._catalog: dict[str, Any] | None = None
        self._closed = False
        self._workspace_frozen = False
        self._close_task: asyncio.Task[None] | None = None

    @property
    def ready(self) -> bool:
        return self._public is not None and not self._closed

    def configure(self, public: PublicConfig, *, catalog: dict[str, Any] | None = None) -> None:
        """Adopt the Harness-accepted PublicConfig for next-invocation use (contract §5).

        Does not touch invocations already admitted: each captured `inv.public` at
        admission time keeps running under the configVersion it was accepted under
        (finding #8) — a request recorded with version A can never run under version B.
        """
        if self._closed:
            raise RuntimeError("service is closing")
        self._public = public.model_copy(deep=True)
        self._catalog = copy.deepcopy(catalog)

    # ------------------------------------------------------------------ start/admission

    async def start(self, request: StartRequest) -> Started:
        if self._workspace_frozen:
            raise RuntimeError("Workspace archive operation is active")
        if self._workspace_ref is not None and request.workspace_ref != self._workspace_ref:
            raise WorkspaceMismatch("workspaceRef does not match this execution pod")
        if self._closed:
            raise RuntimeError("execution service is closing")
        existing = self._invocations.get(request.invocation_id)
        if existing is not None:
            if existing.request != request:
                raise ConflictingStart(
                    f"invocation {request.invocation_id} already started with different data"
                )
            return await asyncio.shield(existing.started)

        if self._public is None:
            raise RuntimeError("ExecutionService.start called before configure()")
        if request.config_version != self._public.config_version:
            raise RuntimeError(
                f"config_version {request.config_version!r} is not the active configuration"
            )
        self._reserve_capacity()

        public = self._public.model_copy(deep=True)  # capture at admission
        catalog = copy.deepcopy(self._catalog)
        if request.memory_available is False and catalog is not None:
            # I7: false removes only the memory facade from THIS invocation's own captured
            # catalog -- the configured shared catalog and every other run are untouched.
            mcp = catalog.get("mcp")
            if isinstance(mcp, dict):
                mcp.pop("memory", None)
        run_id = str(uuid.uuid4())
        started_future: asyncio.Future[Started] = asyncio.get_running_loop().create_future()
        inv = _Invocation(request=request, public=public, catalog=catalog, run_id=run_id, started=started_future)
        self._invocations[request.invocation_id] = inv
        inv.owner_task = asyncio.create_task(self._run_start(inv))
        # R1: the original-deadline watchdog is independent of the owner from the start —
        # a hook or native-send coroutine that never honors its own cancellation must not
        # prevent the deadline from being enforced.
        inv.deadline_task = asyncio.create_task(self._deadline_watch(inv))
        return await asyncio.shield(inv.started)

    async def _run_start(self, inv: _Invocation) -> None:
        """The sole owner of preparation + native send; never cancelled by a caller giving
        up on its `await` (finding #2) — only by this coroutine's own logic."""
        request = inv.request
        deadline = request.deadline_at
        session_lock = self._sessions.lock(request.session_ref)

        if _remaining_seconds(deadline) <= 0:
            self._fail_before_started(
                inv, TimeoutError("deadline already expired before preparation"), "failed"
            )
            return
        try:
            # A direct `await` inside the timeout scope (not a `wait_for`-wrapped sub-task)
            # keeps cancellation delivered to this exact acquire — the old `wait_for` form
            # could race: its wrapper task could win the lock and then still be torn down
            # as timed out/cancelled before ownership was ever recorded here.
            async with asyncio.timeout(_remaining_seconds(deadline)):
                await session_lock.acquire()
        except TimeoutError as exc:
            self._fail_before_started(inv, exc, "failed")
            return
        # No await between a successful acquire and recording ownership — otherwise a
        # cancellation landing in that gap would strand the lock with no owner to release it.
        inv.reservation = session_lock
        if inv.terminal:
            # An explicit cancel() already claimed cleanup ownership (it set `terminal`
            # before this owner resumed) — leave the reservation in place for
            # `_do_cancel`'s own finalizer to release after it joins this owner, so a
            # concurrent successor cannot observe the lock as free before cleanup is done.
            return

        try:
            if _remaining_seconds(deadline) <= 0:
                raise TimeoutError("deadline expired after acquiring session reservation")
            session = self._sessions.get_or_create(request.session_ref)
            inv.state = "preparing"
            hook = inv.public.hooks.session_start
            gate: HookResult | None = None
            if not session.started and hook is not None:
                env = self.hook_env(inv.public, inv.catalog, {
                    "ACH_CHANNEL": request.channel,
                    "ACH_INVOCATION_ID": request.invocation_id,
                    "ACH_SESSION_REF": request.session_ref,
                    "ACH_WORKSPACE_KEY": request.workspace_key,
                    "ACH_SESSION_KEY": request.session_key,
                })
                gate = await run_gate_hook(
                    hook, cwd=session.home, env=env,
                    timeout_seconds=min(float(hook.timeout_seconds), _remaining_seconds(deadline)),
                )
        except SessionHookFailed as exc:
            self._finish_without_engine(inv, "failed", f"sessionStart {exc}")
            return
        except TimeoutError as exc:
            self._fail_before_started(inv, exc, "failed", release=session_lock)
            return
        except asyncio.CancelledError:
            # The owner itself was cancelled (explicit `cancel()`) mid-preparation/send —
            # `_do_cancel` owns the terminal/cancelled bookkeeping and the reservation
            # release (in its own finalizer, after joining this owner and running engine
            # cleanup); releasing here would let a successor acquire before cleanup is done.
            raise
        except Exception as exc:  # noqa: BLE001 - classified below, never raised past the owner
            # A definitive pre-send failure (session-fetch/hook path never reached native
            # send) is still safe to release immediately like the branches above.
            self._fail_before_started(inv, exc, "failed", release=session_lock)
            return

        if inv.terminal:
            # Same cancel-owns-cleanup rule as above: leave the release to `_do_cancel`.
            return

        # sessionStart is the run gate: 78 is a deliberate skip, any other non-zero a failure;
        # both are recorded runs that never reach the engine.
        send_request = request
        if gate is not None:
            if gate.returncode == SKIP_EXIT_CODE:
                self._finish_without_engine(inv, "skipped", None)
                return
            if gate.returncode != 0:
                self._finish_without_engine(
                    inv, "failed",
                    f"sessionStart exited {gate.returncode}: "
                    f"{redact_facade_tokens(gate.stderr)[-_HOOK_ERROR_TAIL:]}",
                )
                return
            if gate.stdout.strip():
                # Never mutate `request`: it is the admitted payload compared for idempotency.
                send_request = request.model_copy(
                    update={"prompt": f"{request.prompt}\n\n{gate.stdout}"}
                )

        inv.state = "sending"
        try:
            invocation = self._build_invocation(send_request, inv.public, self._workspace_dir)
            if inv.catalog is not None and hasattr(invocation, "catalog"):
                invocation.catalog = copy.deepcopy(inv.catalog)
                mcp = invocation.catalog.get("mcp")
                artifacts = mcp.get("artifacts") if isinstance(mcp, dict) else None
                artifact_url = artifacts.get("url") if isinstance(artifacts, dict) else None
                builtin_artifacts_prefix = (
                    f"{inv.public.infrastructure.facade_endpoint.rstrip('/')}/artifacts/t/"
                )
                if isinstance(artifact_url, str) and artifact_url.startswith(
                    builtin_artifacts_prefix
                ):
                    artifacts["url"] = (
                        f"{artifact_url.rstrip('/')}/runs/"
                        f"{quote(request.invocation_id, safe='')}/"
                    )
            elif inv.catalog is not None and hasattr(invocation, "mcp"):
                invocation.mcp = copy.deepcopy(inv.catalog.get("mcp", {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - never built/sent anything: safe pre-send release
            self._fail_before_started(inv, exc, "failed", release=session_lock)
            return
        try:
            # Probe the catalog the engine gets: it carries the per-run artifacts URL.
            await _require_mcp_tools(getattr(invocation, "catalog", None) or inv.catalog)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - still pre-send: a recorded failed run
            self._finish_without_engine(inv, "failed", str(exc))
            return
        if _remaining_seconds(deadline) <= 0:
            # Still pre-send: nothing was ever dispatched, so this is safe to release
            # immediately like the preparation-phase failures above.
            self._fail_before_started(
                inv,
                TimeoutError("deadline expired before native send"),
                "failed",
                release=session_lock,
            )
            return
        try:
            # From here, a cancellation/timeout racing this await can no longer assume
            # nothing was accepted — `possible_send` routes any failure through the
            # halt-confirming cleanup instead of a guessed-safe release.
            inv.possible_send = True
            receipt_coro = self._engine.start(invocation, home=session.home, scratch=session.scratch)
            receipt = await asyncio.wait_for(receipt_coro, timeout=_remaining_seconds(deadline))
        except asyncio.CancelledError:
            # Same cancel-owns-cleanup rule as the preparation phase.
            raise
        except TimeoutError as exc:
            # Unlike a pre-send timeout, this one raced the native send itself: the native
            # side may have already accepted it. Route through the possible-send path,
            # which confirms halt before ever releasing the reservation (finding: "releases
            # without confirmed native halt").
            await self._fail_after_possible_send(inv, exc, session_lock)
            return
        except Exception as exc:  # noqa: BLE001 - classified by `_fail_after_possible_send`
            await self._fail_after_possible_send(inv, exc, session_lock)
            return
        # Retain the exact receipt before publishing Started — a cleanup claimed between
        # the receipt landing and here must still find it recorded.
        inv.receipt = receipt
        session.native_session_id = getattr(receipt, "native_session_id", None)
        # Marker only after a confirmed native receipt — a rejected Start must not skip
        # the hook on the next attempt for this session (finding #12 ordering).
        if not session.started:
            self._sessions.mark_started(session)

        if inv.terminal:
            # Same cancel-owns-cleanup rule as above: leave the release to the cleanup that
            # claimed `inv.terminal`; Started must never publish after that claim.
            return

        inv.state = "running"
        started = Started(
            invocationId=request.invocation_id,
            sessionRef=request.session_ref,
            runId=inv.run_id,
            configVersion=request.config_version,
        )
        if not inv.started.done():
            inv.started.set_result(started)
        inv.settle_task = asyncio.create_task(self._hold_until_settled(inv, session_lock))

    def _finish_without_engine(self, inv: _Invocation, state: RunState, error: str | None) -> None:
        """End an invocation that sessionStart kept from the engine. `Started` is published
        first so control records the run (outcome, metrics) like any other; nothing was
        sent, so the reservation is released immediately."""
        if inv.terminal:
            # A concurrent cancel() already owns finalization and the release.
            return
        inv.terminal = True
        inv.state = state
        inv.error = error
        inv.terminal_at = time.monotonic()
        self._retain_reporting(inv)
        if inv.deadline_task is not None:
            inv.deadline_task.cancel()
        if not inv.started.done():
            inv.started.set_result(Started(
                invocationId=inv.request.invocation_id,
                sessionRef=inv.request.session_ref,
                runId=inv.run_id,
                configVersion=inv.request.config_version,
            ))
        self._release_session_lock(inv)

    def hook_env(
        self, public: PublicConfig, catalog: dict[str, Any] | None, extra: dict[str, str]
    ) -> dict[str, str]:
        """Environment for lifecycle hook scripts: the approved engine env plus what a
        deterministic step needs — the workspace path and this pod's MCP facade URLs
        (`ach-mcp`). The URLs carry the workspace facade token (the same credential the
        engine already holds); hook errors are redacted with `redact_facade_tokens`."""
        mcp = (catalog or {}).get("mcp") or {}
        urls = {
            name: entry["url"]
            for name, entry in mcp.items()
            if isinstance(entry, dict) and isinstance(entry.get("url"), str)
        }
        env = dict(public.engine.env)
        if "PYTHONPATH" in os.environ:
            env["PYTHONPATH"] = os.environ["PYTHONPATH"]
        env["ACH_WORKSPACE"] = str(self._workspace_dir)
        env["ACH_MCP_URLS"] = json.dumps(urls, sort_keys=True)
        env.update(extra)
        return env

    async def run_workspace_hook(self, phase: str, *, restored: bool = False) -> bool:
        """Run `workspaceStart` (`phase="start"`) or `workspaceStop` (`"stop"`) in the
        workspace directory. Returns False when that hook is not configured; a non-zero exit
        or a timeout raises `SessionHookFailed`."""
        if self._public is None:
            raise RuntimeError("execution service is not configured")
        hooks = self._public.hooks
        hook = hooks.workspace_start if phase == "start" else hooks.workspace_stop
        if hook is None:
            return False
        extra = {"ACH_WORKSPACE_RESTORED": "true" if restored else "false"} if phase == "start" else {}
        await run_hook(
            hook, cwd=self._workspace_dir, env=self.hook_env(self._public, self._catalog, extra)
        )
        return True

    def _fail_before_started(
        self,
        inv: _Invocation,
        exc: BaseException,
        state: RunState,
        *,
        release: asyncio.Lock | None = None,
    ) -> None:
        if inv.terminal:
            # Explicit cancellation already owns finalization (e.g. a hook's existing
            # CancelledError -> SessionHookFailed conversion racing a concurrent cancel()):
            # it already settled/will settle `started` as cancelled and owns the release —
            # overwriting state/error or the Future here would hand the caller the wrong
            # terminal result.
            return
        inv.terminal = True
        inv.state = state
        inv.error = str(exc)
        inv.terminal_at = time.monotonic()
        self._retain_reporting(inv)
        if inv.deadline_task is not None:
            inv.deadline_task.cancel()
        if not inv.started.done():
            inv.started.set_exception(exc)
        if release is not None:
            self._release_session_lock(inv)

    async def _fail_after_possible_send(
        self, inv: _Invocation, exc: BaseException, session_lock: asyncio.Lock
    ) -> None:
        """A send that may have already reached native (an ambiguous response, or a timeout
        racing the native call itself) must never release the reservation on a guess — halt
        is confirmed via `engine.cancel()` first (finding: "releases without confirmed
        native halt"). A clean, non-ambiguous rejection (native definitively refused it) is
        the one exception: nothing was ever accepted, so it is still safe to release
        immediately, like a pre-send failure."""
        if inv.terminal:
            return
        ambiguous = getattr(exc, "ambiguous", False)
        if not ambiguous and not isinstance(exc, TimeoutError):
            self._fail_before_started(inv, exc, "failed", release=session_lock)
            return
        inv.error = str(exc)
        # Claim cleanup (its own task runs independently) and exit immediately — never
        # await the worker that joins Start (Start must settle and exit, not be the thing
        # cleanup waits on).
        self._ensure_cleanup(
            inv, f"unconfirmed send: {exc}", "unknown" if ambiguous else "failed"
        )
        if not inv.started.done():
            inv.started.set_exception(exc)

    def _ensure_cleanup(
        self,
        inv: _Invocation,
        reason: str,
        terminal_state: RunState,
        *,
        join: Callable[[], Any] | None = None,
    ) -> asyncio.Task[None]:
        """Synchronously claim retained cleanup ownership — the first trigger (deadline,
        explicit cancel, possible-send failure, or settlement loss) wins and stamps the
        shared `_HaltBudget` at this exact moment; every other trigger simply joins the
        same task via `inv.cancel_task` instead of racing a second cleanup (review focus
        1/2). No `await` happens before the claim (`inv.terminal`/`inv.cancel_task`) is
        recorded, so a concurrently-resuming owner or a second caller always observes it.
        Also retires the original-deadline watchdog here (R1/R2) — unless this very claim
        IS that watchdog's own coroutine, which must never cancel its own task."""
        if inv.cancel_task is not None:
            return inv.cancel_task
        inv.terminal = True
        if inv.deadline_task is not None and asyncio.current_task() is not inv.deadline_task:
            inv.deadline_task.cancel()
        if inv.cleanup_budget is None:
            started_at = time.monotonic()
            inv.cleanup_budget = _HaltBudget(
                cleanup_started_at=started_at, force_deadline=started_at + CANCEL_KILL_BOUND_SECONDS
            )
        inv.cancel_task = asyncio.create_task(
            self._confirm_halt_and_release(inv, reason, terminal_state, join=join)
        )
        return inv.cancel_task

    async def _stop_owner(self, inv: _Invocation) -> None:
        """Cancel and boundedly join a still-running owner task — shared by every cleanup
        trigger that needs the owner stopped before it alone could resume and submit a
        prompt. Uses the shared cleanup clock (`inv.cleanup_budget.force_deadline`), never
        an unbounded `await` — raises if the owner fails, or is not provably done by that
        deadline (a stalled/cancellation-resistant owner), so the caller can surface it as
        retained uncertainty rather than silently finalizing. A done-but-never-joined owner
        (already failed before cleanup claimed it) is checked too, never skipped."""
        owner = inv.owner_task
        if owner is None:
            return
        if not owner.done():
            owner.cancel()
            budget = inv.cleanup_budget
            deadline = budget.force_deadline if budget is not None else time.monotonic()
            done, _pending = await asyncio.wait({owner}, timeout=max(0.0, deadline - time.monotonic()))
            if owner not in done and budget is not None and budget.confirmation_deadline is not None:
                # I2: an actual force has since frozen the shared confirmation deadline —
                # observation extends to that SAME frozen boundary (never a renewed or
                # invented one) so an owner finishing within the allowed confirmation
                # window is not prematurely classified unknown.
                deadline = budget.confirmation_deadline
                done, _pending = await asyncio.wait(
                    {owner}, timeout=max(0.0, deadline - time.monotonic())
                )
            if owner not in done:
                raise TimeoutError("owner task did not stop within the shared cleanup deadline")
        if owner.cancelled():
            return
        exc = owner.exception()
        if exc is not None:
            raise exc

    async def _confirm_halt_and_release(
        self,
        inv: _Invocation,
        reason: str,
        terminal_state: RunState,
        *,
        join: Callable[[], Any] | None = None,
    ) -> None:
        """The sole retained cleanup worker, claimed exactly once via `_ensure_cleanup`:
        native cancel enters before the owner-stop join is created (I1) — both run
        concurrently under the same shared budget (review focus 2), but each is retained
        as its own task (`engine_cancel_task`/`owner_stop_task`) rather than an anonymous
        `gather()` awaitable, so a bounded `asyncio.wait` observation on the shared clock
        can inspect both identities and results even past expiry (I1) — an unresolved
        task is NEVER cancelled here, only retained for later inspection. Release/
        finalization waits for BOTH to complete, never just the engine side, so a
        still-stopping owner can never overlap a successor's reservation (R2). A failure,
        or an expiry with either task still unresolved, retains the reservation and
        alarms as `unknown` rather than guessing (finding: "Retain unknown/alarm and
        reservation if halt cannot be proved")."""
        inv.engine_cancel_task = asyncio.create_task(
            self._engine.cancel(inv.request.invocation_id, reason, _halt_budget=inv.cleanup_budget)
        )
        owner_join = join if join is not None else lambda: self._stop_owner(inv)
        inv.owner_stop_task = asyncio.create_task(owner_join())
        tasks: list[asyncio.Task[Any]] = [inv.engine_cancel_task, inv.owner_stop_task]

        budget = inv.cleanup_budget
        assert budget is not None
        pending = set(tasks)
        done, pending = await asyncio.wait(
            pending, timeout=max(0.0, budget.force_deadline - time.monotonic())
        )
        if pending and budget.confirmation_deadline is not None:
            # I2: an actual force has since frozen the shared confirmation deadline —
            # observation extends to that same frozen boundary, never a renewed one.
            more_done, pending = await asyncio.wait(
                pending, timeout=max(0.0, budget.confirmation_deadline - time.monotonic())
            )
            done |= more_done

        if pending:
            # Retained, not cancelled (I1): the unresolved task(s) stay inspectable.
            inv.state = "unknown"
            inv.error = f"{inv.error or reason}; cleanup uncertain: observation expired"
            inv.terminal_at = time.monotonic()
            self._retain_reporting(inv)
            return

        for task in tasks:
            if task.cancelled():
                inv.state = "unknown"
                inv.error = f"{inv.error or reason}; cleanup uncertain: retained task was cancelled"
                inv.terminal_at = time.monotonic()
                self._retain_reporting(inv)
                return  # cancellation of either retained proof is not successful cleanup
            exc = task.exception()
            if exc is not None:
                inv.state = "unknown"
                inv.error = f"{inv.error or reason}; cleanup uncertain: {exc}"
                inv.terminal_at = time.monotonic()
                self._retain_reporting(inv)
                return  # reservation retained: halt could not be proved
        inv.state = terminal_state
        inv.error = inv.error or reason
        inv.terminal_at = time.monotonic()
        self._retain_reporting(inv)
        self._release_session_lock(inv)

    async def _hold_until_settled(self, inv: _Invocation, session_lock: asyncio.Lock) -> None:
        """Retain the session reservation through correlated terminal settlement — a second
        invocation for the same session cannot be admitted until this releases (finding
        #3). `session_lock` is kept for signature compatibility; the lock actually released
        is whatever `_release_session_lock` finds on `inv.reservation` at that moment — a
        cancellation may have already released it and handed it to a successor, in which
        case this is a no-op rather than stealing the successor's reservation."""
        try:
            outcome = await self._engine.wait_settled(inv.request.invocation_id)
        except Exception as exc:  # noqa: BLE001 - e.g. observation lost; never a safe settlement
            if inv.terminal:
                # A concurrent cancel()/timeout already owns finalization and its own
                # retained cleanup task; nothing to do here.
                return
            if inv.deadline_task is not None:
                inv.deadline_task.cancel()
            await self._ensure_cleanup(inv, f"settlement lost: {exc}", "unknown")
            return
        if inv.terminal:
            # A retained cleanup (cancel/deadline/possible-send-failure) already claimed
            # finalization — its own `_confirm_halt_and_release` owns the release; this
            # late settlement must not race it (review focus 1).
            return
        inv.state = outcome
        if outcome == "completed":
            result_getter = getattr(self._engine, "result", None)
            if callable(result_getter):
                text = result_getter(inv.request.invocation_id)
                if isinstance(text, str):
                    inv.result = {"text": text}
        inv.terminal = True
        inv.terminal_at = time.monotonic()
        self._retain_reporting(inv)
        if inv.deadline_task is not None:
            inv.deadline_task.cancel()
        self._release_session_lock(inv)

    def _retain_reporting(self, inv: _Invocation) -> None:
        """Merge one immutable report into existing free-form result metadata."""
        if isinstance(inv.result, dict) and "reporting" in inv.result:
            return
        known_terminal = inv.state in {"completed", "failed", "cancelled", "skipped"}
        duration_ms = None
        usage_dict = None
        if known_terminal and inv.terminal_at is not None:
            duration_ms = max(0.0, (inv.terminal_at - inv.received_at) * 1000.0)
            getter = getattr(self._engine, "usage", None)
            if callable(getter):
                try:
                    usage = getter(inv.request.invocation_id)
                except Exception:
                    usage = None
                if usage is not None:
                    to_dict = getattr(usage, "to_dict", None)
                    if callable(to_dict):
                        value = to_dict()
                        if isinstance(value, dict):
                            usage_dict = value
        reporting = {"duration_ms": duration_ms, "usage": usage_dict}
        if isinstance(inv.result, dict):
            retained = dict(inv.result)
        elif inv.result is None:
            retained = {}
        else:
            # Existing successful engine results are objects containing raw text. Keep any
            # other JsonValue shape intact under its existing value instead of dropping it.
            retained = {"value": inv.result}
        retained["reporting"] = reporting
        inv.result = retained

    async def _deadline_watch(self, inv: _Invocation) -> None:
        """Fire at the deadline, or earlier once a running invocation shows no engine
        progress for `limits.maxStallSeconds` (a hung tool call holding the slot)."""
        stall = inv.public.limits.max_stall_seconds
        last_progress = getattr(self._engine, "last_progress", None)
        end = time.monotonic() + _remaining_seconds(inv.request.deadline_at)
        reason = "deadline expired"
        try:
            while (remaining := end - time.monotonic()) > 0:
                # ponytail: polls at stall/10, so a stall is caught within 1.1x maxStallSeconds
                await asyncio.sleep(min(remaining, stall / 10))
                progress = (
                    last_progress(inv.request.invocation_id)
                    if callable(last_progress) and inv.state == "running"
                    else None
                )
                if progress is not None and time.monotonic() - progress >= stall:
                    reason = f"stalled: no engine progress for {stall}s"
                    break
        except asyncio.CancelledError:
            return
        if inv.terminal:
            return
        await self._timeout_cancel(inv, reason)

    async def _timeout_cancel(self, inv: _Invocation, reason: str = "deadline expired") -> None:
        """The original deadline firing: a protection against runaway work, not a user
        cancellation — mapped to `failed`. Settles `Started` immediately so a caller is
        never kept waiting on an owner that may itself be stuck ignoring cancellation (R1:
        "Start must report failure and exit rather than await a worker that joins Start"),
        before claiming the shared cleanup that stops the owner and confirms native halt.
        Never touches its own task via `_ensure_cleanup`'s claim (the old self-cancel bug:
        `cancel()` cancelling `inv.deadline_task`, which was this very coroutine, would
        interrupt the engine cancel it had just started awaiting)."""
        if inv.terminal:
            return
        if not inv.started.done():
            inv.started.set_exception(TimeoutError(reason))
        await self._ensure_cleanup(inv, reason, "failed", join=lambda: self._stop_owner(inv))

    def _release_session_lock(self, inv: _Invocation) -> None:
        """The sole release operation for an invocation-owned reservation: reads and clears
        `inv.reservation` synchronously (no intervening await), so a concurrent caller can
        never observe a half-released state, and releases that captured lock exactly once.
        A missing reservation (already released, or never acquired) is a no-op — this must
        never fall back to re-fetching the session's current lock or checking `.locked()`,
        either of which could release a successor's reservation instead of this owner's."""
        lock = inv.reservation
        inv.reservation = None
        if lock is not None:
            lock.release()

    # ------------------------------------------------------------------ status/cancel

    async def status(self, invocation_id: str) -> RunStatus:
        inv = self._invocations.get(invocation_id)
        if inv is None:
            raise KeyError(invocation_id)
        return RunStatus(
            invocationId=inv.request.invocation_id,
            channel=inv.request.channel,
            workspaceRef=inv.request.workspace_ref,
            sessionRef=inv.request.session_ref,
            runId=inv.run_id,
            configVersion=inv.request.config_version,
            deadlineAt=inv.request.deadline_at,
            state=inv.state,
            error=inv.error,
            result=inv.result,
        )

    async def list_runs(self) -> list[RunStatus]:
        return [await self.status(invocation_id) for invocation_id in list(self._invocations)]

    async def cancel(self, invocation_id: str, reason: str) -> None:
        inv = self._invocations.get(invocation_id)
        if inv is None:
            raise KeyError(invocation_id)
        if inv.cancel_task is not None:
            # A cancel is already retained for this invocation (in flight or finished,
            # whichever trigger claimed it first) — join it instead of taking the terminal
            # shortcut below, which would let a second caller return before the first
            # caller's cleanup actually completes.
            await asyncio.shield(inv.cancel_task)
            return
        if inv.terminal:
            return
        if _remaining_seconds(inv.request.deadline_at) <= 0:
            await asyncio.shield(self._timeout_cancel(inv))
            if inv.cancel_task is not None:
                await asyncio.shield(inv.cancel_task)
            return
        await asyncio.shield(self._claim_explicit_cancel(inv, reason))

    async def close(self) -> None:
        """Stop admission, join invocation cleanup, then close engine-owned sessions."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close_impl())
        await asyncio.shield(self._close_task)

    async def _close_impl(self) -> None:
        cleanup_tasks: set[asyncio.Task[None]] = set()
        for inv in self._invocations.values():
            if inv.cancel_task is not None:
                cleanup_tasks.add(inv.cancel_task)
            elif not inv.terminal:
                cleanup_tasks.add(self._claim_explicit_cancel(inv, "shutdown"))
        if cleanup_tasks:
            done, pending = await asyncio.wait(cleanup_tasks, timeout=15.0)
            if pending:
                raise TimeoutError("invocation cleanup exceeded shutdown bound")
            for task in done:
                task.result()
        uncertain = [
            inv.request.invocation_id
            for inv in self._invocations.values()
            if inv.state == "unknown"
            or (inv.cancel_task is not None and not inv.cancel_task.done())
            or (inv.engine_cancel_task is not None and not inv.engine_cancel_task.done())
            or (inv.owner_stop_task is not None and not inv.owner_stop_task.done())
        ]
        if uncertain:
            raise RuntimeError(
                "invocation cleanup remains uncertain at shutdown: " + ", ".join(uncertain)
            )
        close = getattr(self._engine, "close", None)
        if close is not None:
            task = asyncio.create_task(close())
            done, pending = await asyncio.wait({task}, timeout=10.0)
            if pending:
                raise TimeoutError("native session cleanup exceeded shutdown bound")
            task.result()

    def _claim_explicit_cancel(self, inv: _Invocation, reason: str) -> asyncio.Task[None]:
        """Synchronous claim for an explicit public `cancel()` (no `await` before
        `_ensure_cleanup`'s own claim), settling `started` and arming the owner-stop join
        before any other trigger could observe this invocation as merely terminal."""
        # Settle the pending shared Start as cancellation before claiming cleanup: an owner
        # mid-preparation can still run its own failure handling (e.g. a hook's existing
        # CancelledError -> SessionHookFailed conversion) while being joined below, and that
        # must find `started` already done rather than get to publish its own result.
        if not inv.started.done():
            inv.started.cancel()

        # `engine.cancel()` and the owner join run concurrently under the same halt budget
        # (review focus 2), and release/finalization waits for both (R2) — sequencing the
        # owner join first could deadlock if the owner is itself blocked inside the native
        # call that only `engine.cancel()` can unblock.
        return self._ensure_cleanup(inv, reason, "cancelled", join=lambda: self._stop_owner(inv))

    # ------------------------------------------------------------------ retention (finding #14)

    def _reserve_capacity(self) -> None:
        if len(self._invocations) < MAX_RETAINED_RUNS:
            return
        window = self._public.limits.idempotency_window_seconds if self._public else 0.0
        now = time.monotonic()
        evictable = [
            iid
            for iid, inv in self._invocations.items()
            if inv.terminal
            and inv.terminal_at is not None
            # R8: a retained reservation or an unfinished cleanup owner means this
            # invocation is not actually done yet, regardless of how long ago `terminal`
            # was first set — evicting it would orphan the lock/owner it still holds.
            and inv.reservation is None
            and (inv.cancel_task is None or inv.cancel_task.done())
            # I8: an `unknown` record, or one whose retained cleanup tasks have not
            # themselves proved completion, is incomplete owner/resource proof — never
            # evictable, even once its idempotency window has elapsed.
            and inv.state != "unknown"
            and (inv.engine_cancel_task is None or inv.engine_cancel_task.done())
            and (inv.owner_stop_task is None or inv.owner_stop_task.done())
            and (now - inv.terminal_at) >= window
        ]
        if not evictable:
            raise CapacityExceeded(
                "invocation registry is at capacity and no terminal record has cleared its "
                "configured idempotency window yet"
            )
        for iid in evictable[: len(self._invocations) - MAX_RETAINED_RUNS + 1]:
            self._invocations.pop(iid, None)

    # ------------------------------------------------------------------ snapshot/restore

    async def snapshot(self, session_ref: str) -> SnapshotManifest:
        if self._public is None:
            raise RuntimeError("snapshot called before configure()")
        session = self._sessions.get(session_ref)
        if session is None:
            raise RuntimeError(f"no reserved session for {session_ref}")
        session_invocations = [
            inv for inv in self._invocations.values() if inv.request.session_ref == session_ref
        ]
        if any(not inv.terminal for inv in session_invocations):
            raise RuntimeError("cannot snapshot a session with non-terminal work")

        hook = self._public.hooks.session_suspend
        suspend_hook_error: str | None = None
        suspend_hook_failed = False
        if hook is not None:
            try:
                await run_hook(hook, cwd=session.home, env=dict(self._public.engine.env))
            except SessionHookFailed as exc:
                # Contract §6: a failed Suspend is recorded/alarmed, but must not block the
                # snapshot or hang the close indefinitely.
                suspend_hook_error = str(exc)
                suspend_hook_failed = True
        session.suspend_hook_failed = suspend_hook_failed

        clean = await self._engine.suspend(session_ref)
        if not clean:
            raise RuntimeError(
                "native process did not confirm a clean exit before snapshot (forced stop)"
            )

        agent = {
            "name": self._public.agent.name,
            "namespace": self._public.agent.namespace,
            "uid": self._public.agent.uid,
        }
        workspace_ref = session_invocations[-1].request.workspace_ref if session_invocations else (self._workspace_ref or "")
        registry = {
            "workspace_path": str(self._workspace_dir),
            "config_version": self._public.config_version,
            "suspend_hook_error": suspend_hook_error,
            "runs": [
                {
                    "invocationId": inv.request.invocation_id,
                    "channel": inv.request.channel,
                    "workspaceRef": inv.request.workspace_ref,
                    "sessionRef": inv.request.session_ref,
                    "runId": inv.run_id,
                    "request": inv.request.model_dump(by_alias=True, mode="json"),
                    "started": self._started_record(inv),
                    "configVersion": inv.request.config_version,
                    "deadlineAt": inv.request.deadline_at.isoformat(),
                    "state": inv.state,
                    "error": inv.error,
                    "result": inv.result,
                }
                for inv in session_invocations
            ],
        }
        return await self._engine.snapshot(
            session_ref, agent=agent, workspace_ref=workspace_ref, registry=registry
        )

    @staticmethod
    def _started_record(inv: _Invocation) -> dict[str, Any] | None:
        """Persist only the actual successful ACK; runId is minted before native send."""
        if not inv.started.done() or inv.started.cancelled():
            return None
        try:
            started = inv.started.result()
        except BaseException:
            return None
        return started.model_dump(by_alias=True, mode="json")

    async def restore(
        self,
        session_ref: str,
        workspace_ref: str,
        manifest: SnapshotManifest,
        *,
        restored_home: Path | None = None,
        _reservation_held: bool = False,
        deadline_at: datetime | None = None,
    ) -> None:
        if self._public is None:
            raise RuntimeError("restore called before configure()")
        if self._workspace_ref is not None and workspace_ref != self._workspace_ref:
            raise WorkspaceMismatch("workspaceRef does not match this execution pod")
        expected_agent = {
            "name": self._public.agent.name,
            "namespace": self._public.agent.namespace,
            "uid": self._public.agent.uid,
        }
        if manifest.agent != expected_agent:
            raise ValueError("snapshot agent identity does not match this execution pod")
        if manifest.session_ref != session_ref or manifest.workspace_ref != workspace_ref:
            raise ValueError("snapshot identity does not match the requested destination")
        if restored_home is not None:
            staged_manifest = json.loads(
                (restored_home / ".ach-registry.json").read_text(encoding="utf-8")
            )
            if staged_manifest.get("sessionRef") != session_ref:
                raise ValueError("staged HOME session identity does not match")
        expected = {
            "engine_package": "@opencode/cli",
            "engine_version": "2.0.20",
            "workspace_ref": workspace_ref,
            "session_ref": session_ref,
            "workspace_path": str(self._workspace_dir),
        }
        async def restore_reserved() -> None:
            session = self._sessions.get_or_create(session_ref)
            await self._engine.restore(
                session_ref,
                manifest,
                expected=expected,
                public=self._public,
                engine_env=dict(self._public.engine.env),
                home=session.home,
                scratch=session.scratch,
                restored_home=restored_home,
                deadline_at=deadline_at,
            )
            session.native_session_id = manifest.native_session_id
            session.started = (session.home / ".ach-session-started").is_file()
            if "suspend_hook_error" in manifest.registry:
                session.suspend_hook_failed = manifest.registry["suspend_hook_error"] is not None
            hook = self._public.hooks.session_restore
            if hook is not None:
                hook_timeout = hook.timeout_seconds
                if deadline_at is not None:
                    hook_timeout = max(0.0, min(hook_timeout, _remaining_seconds(deadline_at)))
                await run_hook(
                    hook, cwd=session.home, env=dict(self._public.engine.env),
                    timeout_seconds=hook_timeout,
                )
            for run in manifest.registry.get("runs", []):
                self._adopt_restored_run(run)

        if _reservation_held:
            await restore_reserved()
        else:
            async with self._sessions.lock(session_ref):
                await restore_reserved()

    def _adopt_restored_run(self, run: dict[str, Any]) -> None:
        raw_request = run.get("request")
        if not isinstance(raw_request, dict):
            raise ValueError("snapshot run is missing its saved request")
        request = StartRequest.model_validate(raw_request)
        invocation_id = request.invocation_id
        if invocation_id in self._invocations:
            return
        if run.get("state") not in {"completed", "failed", "cancelled", "skipped"}:
            raise ValueError("snapshot contains nonterminal or unknown run state")
        run_id = run.get("runId")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("snapshot run is missing its runId")
        future: asyncio.Future[Started] = asyncio.get_event_loop().create_future()
        raw_started = run.get("started")
        if raw_started is None:
            future.set_exception(
                RuntimeError(run.get("error") or f"invocation ended as {run['state']} before Started")
            )
        else:
            started = Started.model_validate(raw_started)
            if (
                started.invocation_id != request.invocation_id
                or started.session_ref != request.session_ref
                or started.config_version != request.config_version
            ):
                raise ValueError("saved Started receipt does not match its original request")
            future.set_result(started)
        self._invocations[invocation_id] = _Invocation(
            request=request,
            public=self._public,  # type: ignore[arg-type]
            run_id=run_id,
            started=future,
            terminal=True,
            state=run["state"],
            error=run.get("error"),
            result=run.get("result"),
            terminal_at=time.monotonic(),
        )

    async def session_info(self, session_ref: str) -> dict[str, Any]:
        state = self._sessions.get(session_ref)
        if state is None:
            state = next(
                (item for item in self._sessions.inventory() if item.session_ref == session_ref), None
            )
        if state is None:
            raise KeyError(session_ref)
        native = getattr(self._engine, "_sessions", {}).get(session_ref)
        active = bool(native is not None and getattr(native, "alive", False))
        uncertain = any(
            inv.request.session_ref == session_ref
            and (
                inv.state == "unknown"
                or (inv.cancel_task is not None and not inv.cancel_task.done())
                or (inv.owner_stop_task is not None and not inv.owner_stop_task.done())
                or (inv.engine_cancel_task is not None and not inv.engine_cancel_task.done())
            )
            for inv in self._invocations.values()
        )
        return {
            "sessionRef": session_ref,
            "initialized": (state.home / ".ach-session-started").is_file(),
            "nativeSessionId": state.native_session_id,
            "active": active,
            "uncertain": uncertain,
            "suspendHookFailed": state.suspend_hook_failed,
        }

    async def retire_session(self, session_ref: str) -> None:
        """Retire a closed session's local state after its durable close receipt (I6).

        Idempotent: an already-absent (never reserved, or already retired) session is a
        no-op. Rejects active/uncertain/in-flight sessions and an unconfirmed native halt
        before deleting anything; `self._invocations` (the duplicate-Start cache) is never
        touched here.
        """
        if self._sessions.get(session_ref) is None:
            return
        lock = self._sessions.lock(session_ref)
        if lock.locked():
            raise RuntimeError(f"session {session_ref!r} is reserved by in-flight work")
        async with lock:
            if self._sessions.get(session_ref) is None:
                return
            session_invocations = [
                inv for inv in self._invocations.values() if inv.request.session_ref == session_ref
            ]
            if any(not inv.terminal for inv in session_invocations):
                raise RuntimeError(f"cannot retire session {session_ref!r} with non-terminal work")
            info = await self.session_info(session_ref)
            if info["active"] or info["uncertain"]:
                raise RuntimeError(
                    f"cannot retire session {session_ref!r}: active or unconfirmed native state"
                )
            await self._engine.retire_session(session_ref)
            await self._sessions.retire(session_ref)

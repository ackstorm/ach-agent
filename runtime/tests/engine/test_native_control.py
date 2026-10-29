# SPDX-License-Identifier: MIT
"""Native adapter unit control: a scripted httpx transport stands in for the opencode2
HTTP/SSE wire (receipt-validation, cross-invocation correlation and the bounded-cancel
sequence) so these specific failure shapes — a malformed/mismatched receipt, a non-200 SSE
handshake, a stalled cancel phase — can be proven deterministically. The real pinned
`opencode2` binary's own behavior for the happy paths is proven separately in
test_opencode_adapter.py (contract §4); process ownership/kill mechanics are proven with
real subprocesses in test_process_kill.py."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine import opencode as opencode_module
from ach_runtime.engine.opencode import (
    EngineInvocation,
    NativeLaunchFailed,
    OpenCodeEngine,
    _InvocationState,
    _managed_fingerprint,
)
from ach_runtime.engine.config import build_env, managed_content, to_fixture

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"
_LOOPBACK_SERVER = Path(__file__).parent / "fixtures" / "fake_native_server.py"


class _LoopbackHandle:
    """A real `_NativeSession` launched against `fake_native_server.py` (a real subprocess,
    real sockets/SSE — not `httpx.MockTransport`), plus a command channel into it."""

    def __init__(self, session: opencode_module._NativeSession) -> None:
        self.session = session

    async def command(self, cmd: dict) -> None:
        assert self.session._proc is not None and self.session._proc.stdin is not None
        self.session._proc.stdin.write((json.dumps(cmd) + "\n").encode())
        await self.session._proc.stdin.drain()

    async def control_state(self) -> dict:
        status, body = await self.session.request("GET", "/_control/state")
        assert status == 200
        return body


@pytest.fixture
async def loopback(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_module, "OPENCODE_BIN", str(_LOOPBACK_SERVER))
    session = opencode_module._NativeSession("loopback-session", tmp_path / "home", tmp_path / "scratch")
    await session.ensure_started(
        env={"OPENCODE_SERVER_PASSWORD": "x"}, config_path=tmp_path / "cfg.json"
    )
    try:
        yield _LoopbackHandle(session)
    finally:
        with contextlib.suppress(Exception):
            await session._kill(confirm_timeout=5.0)


@pytest.fixture
def public():
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config = RuntimeConfig.model_validate(wire)
    return public_config(config, engine_env=config.engine.env)


async def _fake_wait() -> None:
    return None


def _scripted_session(engine, session_ref, handler, *, sid=None, invocation=None, home=None, scratch=None):
    """A fake-live `_NativeSession` using scripted HTTP instead of a real process.

    Callers exercising `start` can supply the invocation and paths to model its known
    applied configuration; controls that only exercise session internals need not.
    """
    session = opencode_module._NativeSession(session_ref, Path("/tmp/nc-home"), Path("/tmp/nc-scratch"))
    session._proc = SimpleNamespace(returncode=None, pid=999999, wait=_fake_wait)
    session._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://fake-opencode"
    )
    if sid is not None:
        session.sid = sid
    if invocation is not None:
        assert home is not None and scratch is not None
        fixture = to_fixture(
            invocation.public,
            managed_agent=invocation.managed_agent,
            catalog=invocation.catalog,
            resolved_system_prompt=invocation.resolved_system_prompt,
        )
        if invocation.mcp:
            fixture["mcp"] = invocation.mcp
        if invocation.provider_base_url_override:
            fixture["providers"][invocation.public.model.type]["baseURL"] = (
                invocation.provider_base_url_override
            )
        content = managed_content(invocation.public, fixture)
        env = build_env(
            home=home,
            scratch=scratch,
            approved_env=invocation.engine_env,
            password="scripted-session",
        )
        session._managed_fingerprint = _managed_fingerprint(content, env)
    engine._sessions[session_ref] = session
    return session


def _invocation(public, tmp_path, *, invocation_id, session_ref, prompt="do the thing"):
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir(exist_ok=True)
    return EngineInvocation(
        invocation_id=invocation_id,
        session_ref=session_ref,
        workspace_dir=workspace_dir,
        prompt=prompt,
        public=public,
        engine_env=dict(public.engine.env),
    )


@pytest.mark.parametrize(
    "status,data,expected_ambiguous",
    [
        (
            200,
            {
                "id": "msg_1",
                "sessionID": "ses_fake1",
                "type": "user",
                "delivery": "queue",
                "payload": {"text": "WRONG TEXT"},
            },
            True,
        ),
        (
            200,
            {
                "id": "msg_1",
                "sessionID": "wrong-sid",
                "type": "user",
                "delivery": "queue",
                "payload": {"text": "do the thing"},
            },
            True,
        ),
        (
            200,
            {
                "id": "msg_1",
                "sessionID": "ses_fake1",
                "type": "assistant",
                "delivery": "queue",
                "payload": {"text": "do the thing"},
            },
            True,
        ),
        (
            200,
            {
                "id": "msg_1",
                "sessionID": "ses_fake1",
                "type": "user",
                "delivery": "steer",
                "payload": {"text": "do the thing"},
            },
            True,
        ),
        (
            200,
            {
                "id": "",
                "sessionID": "ses_fake1",
                "type": "user",
                "delivery": "queue",
                "payload": {"text": "do the thing"},
            },
            True,
        ),
        (
            200,
            {
                "sessionID": "ses_fake1",
                "type": "user",
                "delivery": "queue",
                "payload": {"text": "do the thing"},
            },
            True,
        ),
        (400, {"error": "rejected"}, False),
    ],
    ids=[
        "wrong-payload-text",
        "wrong-sid",
        "wrong-type",
        "wrong-delivery",
        "blank-id",
        "malformed-missing-id",
        "clean-400-rejection",
    ],
)
async def test_receipt_must_match_submitted_text(
    tmp_path, public, status, data, expected_ambiguous
) -> None:
    """Finding: "Start checks type/SID/delivery/ID but omits submitted payload" — a 200
    whose payload text disagrees with what was actually sent must never be trusted as our
    own accepted prompt (an ambiguous, ignored response), while a clean non-2xx rejection
    stays a definite, non-ambiguous failure."""

    invocation = _invocation(
        public, tmp_path, invocation_id="nc-1", session_ref="native-control-session"
    )
    home, scratch = tmp_path / "home", tmp_path / "scratch"
    prompt_submissions = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/session":
            return httpx.Response(200, json={"data": {"id": "ses_fake1"}})
        if request.method == "GET" and request.url.path == "/api/session/ses_fake1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "ses_fake1",
                        "location": {"directory": str(invocation.workspace_dir)},
                        "model": {
                            "providerID": public.model.type,
                            "id": public.model.name,
                        },
                        "agent": invocation.managed_agent,
                    }
                },
            )
        if request.method == "POST" and request.url.path == "/api/session/ses_fake1/prompt":
            prompt_submissions.append(json.loads(request.content))
            body = {"data": data} if status == 200 else data
            return httpx.Response(status, json=body)
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    engine = OpenCodeEngine()
    _scripted_session(
        engine,
        "native-control-session",
        handler,
        sid="ses_fake1",
        invocation=invocation,
        home=home,
        scratch=scratch,
    )

    with pytest.raises(NativeLaunchFailed) as excinfo:
        await engine.start(invocation, home=home, scratch=scratch)
    assert excinfo.value.ambiguous is expected_ambiguous
    assert prompt_submissions == [{"text": invocation.prompt, "delivery": "queue", "resume": True}]


async def test_sse_non_200_never_marks_ready(public) -> None:
    """Finding: SSE non-200 must never be treated as a ready subscription."""

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/event":
            return httpx.Response(403, text="forbidden")
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    engine = OpenCodeEngine()
    session = _scripted_session(engine, "sse-session", handler)

    await asyncio.wait_for(session._consume_events(), timeout=5.0)
    assert not session._events_ready.is_set()


def _emit(session, sid, seq, type_, data):
    session._record_and_dispatch(
        {"type": type_, "data": data, "durable": {"aggregateID": sid, "seq": seq, "version": 1}}
    )


async def test_receipt_race_binds_owned_busy_period() -> None:
    """Review focus 3: native durable sequence — not local arrival order — decides which
    busy interval a delivery belongs to, so a start/delivery/terminal cycle that all races
    ahead of the invocation's own HTTP receipt still correlates correctly."""
    session = opencode_module._NativeSession(
        "correlation-session", Path("/tmp/nc-home"), Path("/tmp/nc-scratch")
    )
    session.sid = "ses_shared"
    first = _InvocationState(input_id="msg_first")
    session._invocations["first"] = first

    _emit(session, "ses_shared", 1, "session.execution.started", {"sessionID": "ses_shared"})
    _emit(
        session,
        "ses_shared",
        2,
        "session.inbox.delivered",
        {"sessionID": "ses_shared", "inboxID": "msg_first"},
    )
    _emit(session, "ses_shared", 3, "session.execution.succeeded", {"sessionID": "ses_shared"})

    assert first.delivered
    assert first.settled.is_set()
    assert first.outcome == "completed"


async def test_stale_terminal_after_delivery_cannot_settle() -> None:
    """Review focus 3: a historical terminal — one whose busy interval already closed
    before this input's own delivery — must never finish it; only the interval that
    actually CONTAINS the delivery's own sequence may settle it (finding: "accepts SID-only
    terminal after local arrival baseline", now by native sequence rather than a baseline
    index)."""
    session = opencode_module._NativeSession(
        "correlation-session-2", Path("/tmp/nc-home"), Path("/tmp/nc-scratch")
    )
    session.sid = "ses_shared2"
    first = _InvocationState(input_id="msg_first")
    second = _InvocationState(input_id="msg_second")
    session._invocations["first"] = first
    session._invocations["second"] = second

    # First input's whole busy period (seq 1-3) closes well before second is delivered.
    _emit(session, "ses_shared2", 1, "session.execution.started", {"sessionID": "ses_shared2"})
    _emit(
        session,
        "ses_shared2",
        2,
        "session.inbox.delivered",
        {"sessionID": "ses_shared2", "inboxID": "msg_first"},
    )
    _emit(session, "ses_shared2", 3, "session.execution.succeeded", {"sessionID": "ses_shared2"})
    assert first.settled.is_set()
    assert first.outcome == "completed"

    # Second input's own busy period starts and delivers after that — the already-closed
    # first interval must not satisfy it even though both share the same aggregate.
    _emit(session, "ses_shared2", 4, "session.execution.started", {"sessionID": "ses_shared2"})
    _emit(
        session,
        "ses_shared2",
        5,
        "session.inbox.delivered",
        {"sessionID": "ses_shared2", "inboxID": "msg_second"},
    )
    assert second.delivered
    assert not second.settled.is_set()  # its own interval (seq 4-?) is still open

    _emit(
        session,
        "ses_shared2",
        6,
        "session.execution.failed",
        {"sessionID": "ses_shared2", "error": {"message": "x"}},
    )
    assert second.settled.is_set()
    assert second.outcome == "failed"


async def test_duplicate_or_stale_sequence_is_rejected() -> None:
    """Review focus 3: a duplicate/stale `durable.seq` (replayed or out-of-order delivery)
    must never reopen or re-close an interval — missing/malformed sequence is uncertainty,
    never invented execution identity."""
    session = opencode_module._NativeSession(
        "correlation-session-3", Path("/tmp/nc-home"), Path("/tmp/nc-scratch")
    )
    session.sid = "ses_shared3"
    state = _InvocationState(input_id="msg_x")
    session._invocations["x"] = state

    _emit(session, "ses_shared3", 5, "session.execution.started", {"sessionID": "ses_shared3"})
    _emit(
        session,
        "ses_shared3",
        6,
        "session.inbox.delivered",
        {"sessionID": "ses_shared3", "inboxID": "msg_x"},
    )
    _emit(session, "ses_shared3", 7, "session.execution.succeeded", {"sessionID": "ses_shared3"})
    assert state.settled.is_set()
    assert state.outcome == "completed"

    # A stale replay of an earlier (already-superseded) sequence must not reopen anything.
    _emit(session, "ses_shared3", 3, "session.execution.started", {"sessionID": "ses_shared3"})
    assert len(session._busy) == 1  # no new interval was opened from the stale replay


async def test_cancel_uses_wrapped_inbox_and_absolute_force_deadline(
    public, monkeypatch
) -> None:
    """Finding: cancel must decode the real `{data:[...]}` inbox envelope (not a bare
    list), require DELETE204/wait204, and bound the whole interrupt/delete/wait/confirm
    sequence under ONE absolute monotonic deadline — a stalled HTTP phase must not delay
    the force escalation past that bound (no HTTP/client-close can push it later)."""
    monkeypatch.setattr(opencode_module, "CANCEL_KILL_BOUND_SECONDS", 0.3)
    monkeypatch.setattr(opencode_module, "CANCEL_REQUEST_TIMEOUT_SECONDS", 5.0)
    stall = asyncio.Event()  # never set: the DELETE phase hangs well past the force deadline

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/api/session/ses_fake1/interrupt":
            return httpx.Response(200, json={"interrupted": True})
        if request.method == "DELETE" and request.url.path.startswith(
            "/api/session/ses_fake1/inbox/"
        ):
            await stall.wait()
            return httpx.Response(204)
        if request.method == "POST" and request.url.path == "/api/experimental/session/ses_fake1/wait":
            return httpx.Response(204)
        if request.method == "GET" and request.url.path == "/api/session/ses_fake1/inbox":
            return httpx.Response(200, json={"data": [{"id": "msg_pending"}]})
        raise AssertionError(f"unexpected request: {request.method} {request.url.path}")

    engine = OpenCodeEngine()
    _scripted_session(engine, "cancel-session", handler, sid="ses_fake1")
    session = engine._sessions["cancel-session"]
    state = _InvocationState(input_id="msg_pending", delivered=True)
    session._invocations["nc-cancel"] = state

    start = time.monotonic()
    await asyncio.wait_for(engine.cancel("nc-cancel", "test cancellation"), timeout=5.0)
    elapsed = time.monotonic() - start

    assert elapsed < 2.0  # bounded by the force deadline, not the stalled DELETE's 5s timeout
    assert state.outcome == "cancelled"
    assert state.settled.is_set()


async def test_terminal_requires_wait204_and_owned_inbox_absence(loopback) -> None:
    """Review focus 3: a candidate terminal from the SSE stream is not yet a safe
    settlement — `wait_settled` must additionally confirm native wait204 and inbox absence
    of the exact input; wait204 alone (with the input still queued) must not finish it, and
    a non-204/malformed inbox response raises ambiguity rather than trusting the stream."""
    engine = OpenCodeEngine()
    session = loopback.session
    engine._sessions[session.session_ref] = session

    status, body = await session.request("POST", "/api/session", json_body={})
    sid = body["data"]["id"]
    session.sid = sid
    status, body = await session.request(
        "POST", f"/api/session/{sid}/prompt", json_body={"text": "hi", "delivery": "queue", "resume": True}
    )
    input_id = body["data"]["id"]

    state = _InvocationState(input_id=input_id, event_seq_baseline=len(session._event_log))
    session._invocations["inv-wait204"] = state

    await loopback.command(
        {"op": "emit", "sid": sid, "type": "session.execution.started", "data": {"sessionID": sid}}
    )
    await loopback.command(
        {
            "op": "emit",
            "sid": sid,
            "type": "session.inbox.delivered",
            "data": {"sessionID": sid, "inboxID": input_id},
        }
    )
    await loopback.command(
        {"op": "emit", "sid": sid, "type": "session.execution.succeeded", "data": {"sessionID": sid}}
    )
    await asyncio.wait_for(state.settled.wait(), timeout=5.0)
    assert state.outcome == "completed"
    # The fixture's inbox bookkeeping only clears on an explicit DELETE (our SSE `emit`
    # commands are a raw event-log lever, disconnected from it) — clear it to model native
    # actually finishing processing, so the absence check below is the real thing under
    # test rather than an artifact of the fixture never having removed it.
    await session.request("DELETE", f"/api/session/{sid}/inbox/{input_id}")

    # Stall the wait204 confirmation: `wait_settled` must not trust the stream alone.
    await loopback.command(
        {"op": "stall", "method": "POST", "path": f"/api/experimental/session/{sid}/wait", "token": "t1"}
    )
    wait_task = asyncio.ensure_future(engine.wait_settled("inv-wait204"))
    await asyncio.sleep(0.2)
    assert not wait_task.done()
    await loopback.command({"op": "release", "token": "t1", "status": 204, "body": None})
    outcome = await asyncio.wait_for(wait_task, timeout=5.0)
    assert outcome == "completed"

    # wait204 alone, with the exact input still reported pending in the inbox, must not
    # finish it — a second settlement check (a fresh state tied to the same still-queued
    # input) must raise ambiguity rather than trust the stream+wait204 combination.
    pending_state = _InvocationState(input_id=input_id, outcome="completed")
    pending_state.settled.set()
    session._invocations["inv-pending"] = pending_state
    await loopback.command(
        {
            "op": "respond_once",
            "method": "GET",
            "path": f"/api/session/{sid}/inbox",
            "status": 200,
            "body": {"data": [{"id": input_id}]},
        }
    )
    with pytest.raises(NativeLaunchFailed) as excinfo:
        await asyncio.wait_for(engine.wait_settled("inv-pending"), timeout=5.0)
    assert excinfo.value.ambiguous is True


async def test_confirm_no_pending_input_treats_missing_input_id_as_unsafe() -> None:
    """Finding: "missing input ID is incorrectly considered safe" — an ambiguous send with
    no known input ID can never be proven to carry no pending owned work, so it must force
    a full owned-session halt rather than a quiet, confirmed-safe return."""
    engine = OpenCodeEngine()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make any request when input_id is unknown")

    session = _scripted_session(engine, "unsafe-session", handler, sid="ses_fake1")
    state = _InvocationState(input_id=None)

    assert await engine._confirm_no_pending_input(session, state) is False


@pytest.mark.parametrize(
    "failing_phase",
    ["interrupt_rejected", "interrupt_not_acknowledged", "delete_rejected", "wait_rejected"],
)
async def test_cancel_forces_on_any_unacknowledged_graceful_phase(public, failing_phase) -> None:
    """R4: a failed/unacknowledged interrupt, DELETE, or wait must each stop graceful
    cancellation right there and force a whole-session halt — none may be silently
    skipped and treated as success."""

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/session/ses_fake1/interrupt":
            if failing_phase == "interrupt_rejected":
                return httpx.Response(500, json={"error": "boom"})
            if failing_phase == "interrupt_not_acknowledged":
                return httpx.Response(200, json={"interrupted": False})
            return httpx.Response(200, json={"interrupted": True})
        if request.method == "DELETE" and path.startswith("/api/session/ses_fake1/inbox/"):
            if failing_phase == "delete_rejected":
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(204)
        if request.method == "POST" and path == "/api/experimental/session/ses_fake1/wait":
            if failing_phase == "wait_rejected":
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(204)
        if request.method == "GET" and path == "/api/session/ses_fake1/inbox":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    engine = OpenCodeEngine()
    _scripted_session(engine, "force-on-failure-session", handler, sid="ses_fake1")
    session = engine._sessions["force-on-failure-session"]
    state = _InvocationState(input_id="msg_pending", delivered=True)
    session._invocations["nc-force"] = state

    await asyncio.wait_for(engine.cancel("nc-force", "test cancellation"), timeout=5.0)

    assert state.outcome == "cancelled"
    assert state.settled.is_set()
    # Only the FORCE path touches `_confirmed_halted` — proves graceful success was
    # never (wrongly) claimed despite the unacknowledged/failed phase.
    assert session._confirmed_halted is True


async def test_receipt_race_replay_rejects_stale_and_foreign_evidence() -> None:
    """R5: replay must apply the SAME acceptance rules live processing used — a stale
    duplicate delivery (superseded by a later accepted sequence) and a foreign-aggregate
    event must never retroactively change an invocation's bound outcome via
    `reconcile_from_baseline`."""
    session = opencode_module._NativeSession(
        "receipt-race-session", Path("/tmp/nc-home"), Path("/tmp/nc-scratch")
    )
    session.sid = "ses_race"
    state = _InvocationState(event_seq_baseline=0)
    session._invocations["race"] = state

    # Buffered before the receipt lands: start/delivery/terminal for THIS input's own
    # busy period (seq 10-12), plus a stale duplicate delivery (seq 2, already
    # superseded) and a foreign-aggregate event sharing a local arrival slot.
    _emit(session, "ses_race", 10, "session.execution.started", {"sessionID": "ses_race"})
    _emit(
        session,
        "ses_race",
        11,
        "session.inbox.delivered",
        {"sessionID": "ses_race", "inboxID": "msg_race"},
    )
    _emit(session, "ses_race", 12, "session.execution.succeeded", {"sessionID": "ses_race"})
    session._record_and_dispatch(
        {
            "type": "session.inbox.delivered",
            "data": {"sessionID": "ses_race", "inboxID": "msg_race"},
            "durable": {"aggregateID": "ses_race", "seq": 2, "version": 1},
        }
    )
    session._record_and_dispatch(
        {
            "type": "session.inbox.delivered",
            "data": {"sessionID": "ses_race", "inboxID": "msg_race"},
            "durable": {"aggregateID": "ses_other", "seq": 999, "version": 1},
        }
    )

    assert not state.delivered  # receipt has not landed yet — no input_id to match against

    # The receipt lands: bind the exact owned input id and replay only accepted evidence.
    state.input_id = "msg_race"
    session.reconcile_from_baseline(state)

    assert state.delivered
    assert state.delivered_seq == 11  # the real accepted delivery, not the stale/foreign ones
    assert state.settled.is_set()
    assert state.outcome == "completed"


async def test_stop_cleanly_proves_clean_exit_and_repeats_on_suspend(loopback) -> None:
    """R6: a genuine requested-EOF exit0 with no live descendants and EOF-drained
    readers/observer/client must retain True, including on a repeated suspend call."""
    session = loopback.session
    confirmed = await session.stop_cleanly(timeout_seconds=5.0)
    assert confirmed is True
    assert session.clean_exit is True
    assert not session.alive

    again = await session.stop_cleanly(timeout_seconds=5.0)
    assert again is True


async def test_stop_cleanly_accepts_shutdown_truncated_sse(loopback, monkeypatch) -> None:
    """A requested exit0 with drained process pipes and joined resources is clean even
    when HTTP chunk framing is incomplete because native shutdown closed its SSE socket."""
    session = loopback.session
    kill_calls = []

    async def unexpected_kill(**kwargs):
        kill_calls.append(kwargs)
        return False

    monkeypatch.setattr(session, "_kill", unexpected_kill)
    confirmed = await session.stop_cleanly(timeout_seconds=5.0)

    assert confirmed is True
    assert session._proc.returncode == 0
    assert session._events_eof is False
    assert session._stdout_eof is True
    assert session._stderr_eof is True
    assert session._event_task.done()
    assert session._stdout_task.done()
    assert session._stderr_task.done()
    assert session._ownership_task.done()
    assert session._client.is_closed
    assert session._owned._live() == []
    assert session.clean_exit is True
    assert kill_calls == []


async def test_stop_cleanly_rejects_nonzero_exit(loopback) -> None:
    session = loopback.session
    await loopback.command({"op": "exit", "code": 7})

    confirmed = await session.stop_cleanly(timeout_seconds=5.0)

    assert confirmed is False
    assert session._proc.returncode == 7
    assert session.clean_exit is False


@pytest.mark.parametrize("failure", ["close", "unjoined-resource"])
async def test_stop_cleanly_rejects_failed_or_unjoined_resource(loopback, monkeypatch, failure) -> None:
    session = loopback.session
    if failure == "close":
        async def failed_close():
            raise RuntimeError("close failed")

        monkeypatch.setattr(session._client, "aclose", failed_close)
    else:
        original = session._ownership_task
        original.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await original
        release = asyncio.Event()

        async def cancellation_resistant_resource():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        session._ownership_task = asyncio.create_task(cancellation_resistant_resource())
        kill_calls = []

        async def skip_fallback_for_control(**kwargs):
            kill_calls.append(kwargs)
            return False

        monkeypatch.setattr(session, "_kill", skip_fallback_for_control)
        await asyncio.sleep(0)

    confirmed = await session.stop_cleanly(timeout_seconds=0.15)

    assert confirmed is False
    assert session.clean_exit is False
    if failure == "unjoined-resource":
        assert not session._ownership_task.done()
        assert kill_calls
        release.set()
        await asyncio.wait_for(session._ownership_task, timeout=1.0)


async def test_stop_cleanly_returns_false_when_a_tracked_child_survives(loopback) -> None:
    """R6: a live tracked descendant at exit must force False, never a false clean
    proof — and the fallback force path must actually halt the process and its child."""
    session = loopback.session
    await loopback.command({"op": "spawn_detached_child"})
    await asyncio.sleep(0.2)  # let the fork actually happen and get tracked
    session._owned._refresh()
    assert session._owned._owned, "the detached child must be tracked before the stop"

    confirmed = await session.stop_cleanly(timeout_seconds=2.0)
    assert confirmed is False
    assert session.clean_exit is False
    assert not session.alive  # the fallback force path still actually halted it


async def test_relaunch_resets_clean_and_halt_proof(loopback, tmp_path) -> None:
    """R7: per-process clean/halt proof must never leak into a new process generation —
    a relaunch after a proved-clean stop starts with no proof of its own."""
    session = loopback.session
    await session.stop_cleanly(timeout_seconds=5.0)
    assert session.clean_exit is True
    assert session._confirmed_halted is True

    await session.ensure_started(
        env={"OPENCODE_SERVER_PASSWORD": "x"}, config_path=tmp_path / "cfg2.json"
    )
    assert session.clean_exit is False
    assert session._confirmed_halted is False


async def test_force_signal_is_independent_of_a_cancellation_resistant_graceful_phase(
    loopback, monkeypatch
) -> None:
    """Step1 review focus 4: the actual SIGKILL must be armed on an independent timer, not
    merely triggered by cancelling the graceful HTTP phase — a graceful coroutine that
    swallows its own cancellation and keeps running must never delay the real force signal
    past the budget (the "not cooperative wait_for equivalence" requirement)."""
    monkeypatch.setattr(opencode_module, "CANCEL_KILL_BOUND_SECONDS", 0.5)
    session = loopback.session
    engine = OpenCodeEngine()
    engine._sessions[session.session_ref] = session

    status, body = await session.request("POST", "/api/session", json_body={})
    sid = body["data"]["id"]
    session.sid = sid
    status, body = await session.request(
        "POST",
        f"/api/session/{sid}/prompt",
        json_body={"text": "hi", "delivery": "queue", "resume": True},
    )
    input_id = body["data"]["id"]
    state = _InvocationState(input_id=input_id, delivered=True)
    session._invocations["resistant"] = state

    resistant_release = asyncio.Event()
    interrupt_entered = asyncio.Event()
    real_request = session.request

    async def _resistant_request(method, path, **kwargs):
        if method == "POST" and path.startswith(f"/api/session/{sid}/interrupt"):
            interrupt_entered.set()
            with contextlib.suppress(asyncio.CancelledError):
                await resistant_release.wait()
            await resistant_release.wait()
        return await real_request(method, path, **kwargs)

    monkeypatch.setattr(session, "request", _resistant_request)

    start = time.monotonic()
    pid = session._proc.pid
    cancel_task = asyncio.ensure_future(engine.cancel("resistant", "test cancellation"))
    await asyncio.wait_for(interrupt_entered.wait(), timeout=5)

    # The actual OS-level SIGKILL must land within budget + generous scheduling margin,
    # independent of the still-resistant graceful phase above.
    deadline = start + 0.5 + 1.0
    killed = False
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            killed = True
            break
        await asyncio.sleep(0.02)
    assert killed, "actual SIGKILL did not land within the independent force budget"

    resistant_release.set()
    await asyncio.wait_for(cancel_task, timeout=5)
    assert state.outcome == "cancelled"


async def test_kill_stamps_confirmation_deadline_only_when_forcing_a_live_target(
    loopback,
) -> None:
    """I5: `_halt_budget.mark_forced()` must run exactly when a live owned identity is
    about to be signaled — never preemptively (before the guarded syscall), and never
    invented against an absent target."""
    session = loopback.session
    live_budget = opencode_module._HaltBudget(
        cleanup_started_at=time.monotonic(), force_deadline=time.monotonic() + 5.0
    )
    before = time.monotonic()
    confirmed = await session._kill(confirm_timeout=5.0, _halt_budget=live_budget)
    assert confirmed is True
    assert live_budget.confirmation_deadline is not None
    # Stamped at/after the actual force, not invented ahead of it.
    assert live_budget.confirmation_deadline >= before + 5.0 - 0.5

    # A never-launched session has no live target: `_kill` must not invent a force
    # timestamp when there is nothing to signal.
    session2 = opencode_module._NativeSession("i5-absent", session.home, session.scratch)
    absent_budget = opencode_module._HaltBudget(
        cleanup_started_at=time.monotonic(), force_deadline=time.monotonic() + 5.0
    )
    confirmed2 = await session2._kill(confirm_timeout=5.0, _halt_budget=absent_budget)
    assert confirmed2 is True
    assert absent_budget.confirmation_deadline is None


async def test_cancel_consumes_committed_halt_result_despite_graceful_success(
    public, monkeypatch
) -> None:
    """I4: once the independent force has already committed to an actual halt, a racing
    graceful acknowledgement that goes on to succeed must still consume that committed
    halt's real result — never silently cancel an executing halt worker on HTTP/terminal
    evidence alone and claim the session clean/reusable instead."""
    monkeypatch.setattr(opencode_module, "CANCEL_KILL_BOUND_SECONDS", 1.0)
    interrupt_gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/session/ses_fake1/interrupt":
            await interrupt_gate.wait()
            return httpx.Response(200, json={"interrupted": True})
        if request.method == "DELETE" and path.startswith("/api/session/ses_fake1/inbox/"):
            return httpx.Response(204)
        if request.method == "POST" and path == "/api/experimental/session/ses_fake1/wait":
            session._invocations["committed"].settled.set()
            return httpx.Response(204)
        if request.method == "GET" and path == "/api/session/ses_fake1/inbox":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    engine = OpenCodeEngine()
    _scripted_session(engine, "committed-halt-session", handler, sid="ses_fake1")
    session = engine._sessions["committed-halt-session"]
    state = _InvocationState(input_id="msg_pending", delivered=True)
    session._invocations["committed"] = state

    cancel_task = asyncio.ensure_future(engine.cancel("committed", "test cancellation"))

    async def _wait_for(predicate) -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(_wait_for(lambda: session._force_trigger is not None), timeout=3.0)
    session._force_trigger.set()  # commit the force immediately, well inside the 1.0s window
    await asyncio.wait_for(_wait_for(lambda: session._halt_task is not None), timeout=3.0)
    assert session._halt_task is not None  # force has committed to an actual halt

    interrupt_gate.set()  # let the racing graceful sequence go on to "succeed"
    await asyncio.wait_for(cancel_task, timeout=5.0)

    assert state.outcome == "cancelled"
    assert state.settled.is_set()
    assert session._halt_task.done()
    assert session._confirmed_halted is True


async def test_kill_treats_a_stuck_resource_join_as_unconfirmed(loopback) -> None:
    """I3: a reader/observer resource that never joins (outlives its own cancellation)
    must prevent a successful halt confirmation — bounded by the shared deadline, never
    a silent True and never an indefinite hang."""
    session = loopback.session

    async def _stuck() -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.Event().wait()
        await asyncio.Event().wait()  # keeps running past its own cancellation forever

    if session._stderr_task is not None:
        session._stderr_task.cancel()
    session._stderr_task = asyncio.ensure_future(_stuck())

    start = time.monotonic()
    confirmed = await asyncio.wait_for(session._kill(confirm_timeout=0.3), timeout=5.0)
    elapsed = time.monotonic() - start

    assert confirmed is False
    assert elapsed < 2.0, "a stuck resource join must be bounded, not block _kill indefinitely"


async def test_stop_cleanly_rejects_a_task_that_finished_without_genuine_eof(loopback) -> None:
    """I7: a reader task that completes successfully but never reached genuine pipe/SSE
    EOF (e.g. it swallowed its own cancellation and returned early) must not count as
    drained proof — exit0 and no live children are not enough on their own."""
    session = loopback.session

    async def _finishes_without_eof() -> None:
        return None

    if session._stdout_task is not None:
        session._stdout_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await session._stdout_task
    session._stdout_task = asyncio.ensure_future(_finishes_without_eof())
    session._stdout_eof = False

    confirmed = await asyncio.wait_for(session.stop_cleanly(timeout_seconds=5.0), timeout=10.0)

    assert confirmed is False
    assert session.clean_exit is False


def test_only_this_sessions_events_count_as_engine_progress() -> None:
    """The stall watchdog's signal: a `session.*` event for this native session stamps
    progress; a permission ask or another session's event does not."""
    session = opencode_module._NativeSession(
        "progress-session", Path("/tmp/nc-home"), Path("/tmp/nc-scratch")
    )
    session.sid = "ses_p"
    state = _InvocationState(event_seq_baseline=0)
    session._invocations["inv"] = state
    before = state.last_progress_at
    time.sleep(0.01)
    session._record_and_dispatch({"type": "permission.asked", "data": {"sessionID": "ses_p"}})
    session._record_and_dispatch({"type": "session.tool.called", "data": {"sessionID": "ses_x"}})
    assert state.last_progress_at == before
    session._record_and_dispatch({"type": "session.tool.called", "data": {"sessionID": "ses_p"}})
    assert state.last_progress_at > before

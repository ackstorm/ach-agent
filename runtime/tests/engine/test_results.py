# SPDX-License-Identifier: MIT
"""Owned native final-text capture through durable session events."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ach_runtime.engine.opencode import OpenCodeEngine, _InvocationState, _NativeSession


def _event(kind: str, seq: int, **data: object) -> dict[str, object]:
    return {
        "type": kind,
        "data": {"sessionID": "sid-result", **data},
        "durable": {"aggregateID": "sid-result", "seq": seq},
    }


def _bind_engine() -> tuple[OpenCodeEngine, _NativeSession, _InvocationState]:
    engine = OpenCodeEngine()
    session = _NativeSession("session-result", Path("/tmp/home"), Path("/tmp/scratch"))
    session.sid = "sid-result"
    state = _InvocationState(event_seq_baseline=0, input_id=None)
    session._invocations["inv-result"] = state
    engine._sessions["session-result"] = session
    return engine, session, state


async def test_native_result_uses_final_step_and_reconciles_before_receipt() -> None:
    """A fast terminal buffered before the POST receipt keeps only final-step text."""
    engine, session, state = _bind_engine()
    events = [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="input-result"),
        _event("session.step.started", 3, assistantMessageID="assistant-first"),
        _event(
            "session.text.ended",
            4,
            assistantMessageID="assistant-first",
            ordinal=0,
            text="discard this earlier step",
        ),
        _event("session.step.started", 5, assistantMessageID="assistant-final"),
        _event(
            "session.text.ended",
            6,
            assistantMessageID="assistant-final",
            ordinal=1,
            text=" world",
        ),
        _event(
            "session.text.ended",
            7,
            assistantMessageID="assistant-final",
            ordinal=0,
            text="hello",
        ),
        _event("session.execution.succeeded", 8),
    ]
    for event in events:
        session._record_and_dispatch(event)

    # The native terminal and text beat the HTTP receipt; its state is reconciled using
    # the accepted event log after the native input ID becomes known.
    state.input_id = "input-result"
    session.reconcile_from_baseline(state)
    assert state.outcome == "completed"
    assert engine.result("inv-result") is None  # no independent settlement confirmation yet

    confirmation_entered = asyncio.Event()
    confirmation_release = asyncio.Event()

    async def confirm(*_args: object) -> None:
        confirmation_entered.set()
        await confirmation_release.wait()

    engine._confirm_settlement = confirm  # type: ignore[method-assign]
    settled = asyncio.create_task(engine.wait_settled("inv-result"))
    await confirmation_entered.wait()
    assert engine.result("inv-result") is None
    confirmation_release.set()
    assert await settled == "completed"
    assert engine.result("inv-result") == "hello world"


def test_native_result_excludes_other_inputs_stale_and_foreign_text() -> None:
    """Only text from the step owned by this input's busy interval can be returned."""
    engine, session, state = _bind_engine()
    events = [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="input-other"),
        _event("session.step.started", 3, assistantMessageID="assistant-other"),
        _event(
            "session.text.ended",
            4,
            assistantMessageID="assistant-other",
            ordinal=0,
            text="other input",
        ),
        _event("session.execution.succeeded", 5),
        _event("session.execution.started", 6),
        _event("session.inbox.delivered", 7, inboxID="input-result"),
        _event("session.step.started", 8, assistantMessageID="assistant-result"),
        {
            **_event(
                "session.text.ended",
                99,
                assistantMessageID="assistant-result",
                ordinal=0,
                text="foreign aggregate",
            ),
            "durable": {"aggregateID": "sid-someone-else", "seq": 99},
        },
        _event("session.text.ended", 4, assistantMessageID="assistant-result", ordinal=0, text="stale"),
        _event("session.text.ended", 9, assistantMessageID="assistant-not-current", ordinal=0, text="wrong step"),
        _event("session.text.ended", 10, assistantMessageID="assistant-result", ordinal=0, text="owned result"),
        _event("session.execution.succeeded", 11),
    ]
    for event in events:
        session._record_and_dispatch(event)
    state.input_id = "input-result"
    session.reconcile_from_baseline(state)

    async def confirm(*_args: object) -> None:
        return None

    engine._confirm_settlement = confirm  # type: ignore[method-assign]

    async def settled_result() -> str | None:
        assert await engine.wait_settled("inv-result") == "completed"
        return engine.result("inv-result")

    assert asyncio.run(settled_result()) == "owned result"


async def test_native_result_is_none_for_failed_or_absent_text() -> None:
    engine, session, state = _bind_engine()
    events = [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="input-result"),
        _event("session.execution.failed", 3),
    ]
    for event in events:
        session._record_and_dispatch(event)
    state.input_id = "input-result"
    session.reconcile_from_baseline(state)

    async def confirm(*_args: object) -> None:
        return None

    engine._confirm_settlement = confirm  # type: ignore[method-assign]
    assert await engine.wait_settled("inv-result") == "failed"
    assert engine.result("inv-result") is None


async def test_native_retire_drops_sid_but_keeps_terminal_results_and_usage() -> None:
    """I6: retire clears native SID/config/process identity, but `status`/`result`/`usage`
    must keep working for already-terminal invocations — including a forced-halt
    (cancelled, interval never closed) invocation whose usage is only ever reconstructed
    lazily from `_busy`, which retire must materialize before dropping `_busy`."""
    def _retire_event(kind: str, seq: int, **data: object) -> dict[str, object]:
        return {
            "type": kind,
            "data": {"sessionID": "sid-retire", **data},
            "durable": {"aggregateID": "sid-retire", "seq": seq},
        }

    engine = OpenCodeEngine()
    session = _NativeSession("session-retire", Path("/tmp/home-retire"), Path("/tmp/scratch-retire"))
    session.sid = "sid-retire"
    session.url = "http://127.0.0.1:1"
    session.config_version = "config-v1"
    completed = _InvocationState(event_seq_baseline=0, input_id=None)
    cancelled = _InvocationState(event_seq_baseline=0, input_id=None)
    session._invocations["inv-completed"] = completed
    session._invocations["inv-cancelled"] = cancelled
    engine._sessions["session-retire"] = session

    events = [
        _retire_event("session.execution.started", 1),
        _retire_event("session.inbox.delivered", 2, inboxID="input-completed"),
        _retire_event("session.step.started", 3, assistantMessageID="assistant-completed"),
        _retire_event("session.text.ended", 4, assistantMessageID="assistant-completed",
                      ordinal=0, text="finished turn"),
        _retire_event("session.execution.succeeded", 5),
        _retire_event("session.execution.started", 6),
        _retire_event("session.inbox.delivered", 7, inboxID="input-cancelled"),
        _retire_event("session.step.started", 8, assistantMessageID="assistant-cancelled"),
        _retire_event("session.step.ended", 9, assistantMessageID="assistant-cancelled",
                      tokens={"input": 10, "output": 20, "reasoning": 0, "cache": {"read": 1, "write": 2}},
                      cost=0.05),
        # killed mid-turn: no execution.succeeded/failed/interrupted ever closes interval 2
    ]
    for event in events:
        session._record_and_dispatch(event)

    completed.input_id = "input-completed"
    session.reconcile_from_baseline(completed)
    cancelled.input_id = "input-cancelled"
    session.reconcile_from_baseline(cancelled)
    cancelled.outcome = "cancelled"

    async def confirm(*_args: object) -> None:
        return None

    engine._confirm_settlement = confirm  # type: ignore[method-assign]
    assert await engine.wait_settled("inv-completed") == "completed"
    assert engine.result("inv-completed") == "finished turn"

    session._proc = None
    session._confirmed_halted = True
    assert engine.usage("inv-cancelled") is not None  # lazily reconstructed before retire
    assert cancelled.usage is None  # still lazy, not yet materialized onto the state

    await engine.retire_session("session-retire")

    assert session.sid is None
    assert session.url is None
    assert session.config_version is None
    assert session._event_log == []
    assert session._busy == []
    assert engine.result("inv-completed") == "finished turn"
    usage = engine.usage("inv-cancelled")
    assert usage is not None
    assert usage.input == 10 and usage.output == 20 and usage.steps == 1
    assert cancelled.usage is usage  # materialized onto the state, not recomputed from _busy


async def test_native_result_is_none_when_completed_interval_has_no_text() -> None:
    engine, session, state = _bind_engine()
    events = [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="input-result"),
        _event("session.execution.succeeded", 3),
    ]
    for event in events:
        session._record_and_dispatch(event)
    state.input_id = "input-result"
    session.reconcile_from_baseline(state)

    async def confirm(*_args: object) -> None:
        return None

    engine._confirm_settlement = confirm  # type: ignore[method-assign]
    assert await engine.wait_settled("inv-result") == "completed"
    assert engine.result("inv-result") is None


async def test_native_retire_resets_durable_cursor_for_the_next_sid() -> None:
    """Durable seq is per native aggregate: after retire, a reopened session's new SID
    starts at seq 1 and must be accepted, while an old delivered-but-unsettled invocation
    must not claim the new SID's busy interval."""
    engine = OpenCodeEngine()
    session = _NativeSession("session-reopen", Path("/tmp/home-reopen"), Path("/tmp/scratch-reopen"))
    session.sid = "sid-old"
    stale = _InvocationState(event_seq_baseline=0, input_id="input-stale")
    stale.delivered = True
    stale.delivered_seq = 2
    session._invocations["inv-stale"] = stale
    engine._sessions["session-reopen"] = session
    session._record_and_dispatch(
        {"type": "session.execution.started", "data": {"sessionID": "sid-old"},
         "durable": {"aggregateID": "sid-old", "seq": 100}}
    )
    assert session._last_durable_seq == 100
    session._proc = None
    session._confirmed_halted = True

    await engine.retire_session("session-reopen")

    assert stale.settled.is_set() and stale.outcome == "unknown"
    session.sid = "sid-new"
    for kind, seq in (("session.execution.started", 1), ("session.execution.succeeded", 3)):
        session._record_and_dispatch(
            {"type": kind, "data": {"sessionID": "sid-new"},
             "durable": {"aggregateID": "sid-new", "seq": seq}}
        )
    assert all(accepted for _event, accepted in session._event_log)
    assert session._last_durable_seq == 3
    assert len(session._busy) == 1 and not session._busy[0]["used"]

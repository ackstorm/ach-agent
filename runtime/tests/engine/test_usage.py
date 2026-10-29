# SPDX-License-Identifier: MIT
"""Native usage is accumulated only from owned, correlated durable operations."""

from __future__ import annotations

from pathlib import Path

from ach_runtime.engine.opencode import OpenCodeEngine, _InvocationState, _NativeSession


def _event(kind: str, seq: int, *, aggregate: str = "sid-usage",
           session: str = "sid-usage", **data: object) -> dict[str, object]:
    return {
        "type": kind,
        "data": {"sessionID": session, **data},
        "durable": {"aggregateID": aggregate if aggregate != "sid-usage" else session, "seq": seq},
    }


def _usage(input: float = 1, output: float = 2, reasoning: float = 3,
           read: float = 1, write: float = 2, cost: float = 0.25) -> dict[str, object]:
    return {
        "tokens": {"input": input, "output": output, "reasoning": reasoning,
                   "cache": {"read": read, "write": write}},
        "cost": cost,
    }


def test_usage_is_owned_deduplicated_and_includes_compaction() -> None:
    engine = OpenCodeEngine()
    session = _NativeSession("session-usage", Path("/tmp/home"), Path("/tmp/scratch"))
    session.sid = "sid-usage"
    first = _InvocationState(input_id="input-one")
    session._invocations["inv-one"] = first
    engine._sessions["session-usage"] = session

    events = [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="input-one"),
        _event("session.usage.recorded", 3, **_usage(input=500, output=500, cost=500)),
        _event("session.step.started", 4, assistantMessageID="assistant-one"),
        _event("session.step.ended", 5, assistantMessageID="assistant-one", **_usage()),
        _event("session.compaction.started", 6, inputID="input-one"),
        _event("session.compaction.ended", 7, reason="auto", **_usage(cost=0.5)),
        _event("session.step.started", 8, assistantMessageID="assistant-two"),
        _event("session.step.ended", 9, assistantMessageID="assistant-two", **_usage(cost=0)),
        _event("session.text.ended", 10, assistantMessageID="assistant-two", ordinal=0, text="final"),
        _event("session.execution.succeeded", 11),
    ]
    for event in events:
        session._record_and_dispatch(event)
    # Duplicate, stale, and foreign aggregate events are rejected before reduction.
    session._record_and_dispatch(events[4])
    session._record_and_dispatch(_event("session.step.ended", 2, assistantMessageID="assistant-one", **_usage(cost=999)))
    session._record_and_dispatch(_event("session.step.ended", 12, aggregate="sid-foreign",
                                        assistantMessageID="assistant-one", **_usage(cost=999)))
    assert engine.usage("inv-one") is None
    first.settlement_confirmed = True
    assert engine.usage("inv-one").to_dict() == {
        "source": "opencode", "basis": "engine_estimate", "input": 3,
        "output": 6, "reasoning": 9, "cache_read": 3, "cache_write": 6,
        "cost_usd": 0.75, "steps": 2,
    }
    assert engine.result("inv-one") == "final"

    other_session = _NativeSession("session-other", Path("/tmp/home-2"), Path("/tmp/scratch-2"))
    other_session.sid = "sid-other"
    other_state = _InvocationState(input_id="input-other-session")
    other_session._invocations["inv-other-session"] = other_state
    engine._sessions[other_session.session_ref] = other_session
    for event in [
        _event("session.execution.started", 1, session="sid-other"),
        _event("session.inbox.delivered", 2, session="sid-other", inboxID="input-other-session"),
        _event("session.step.started", 3, session="sid-other", assistantMessageID="other-step"),
        _event("session.step.ended", 4, session="sid-other", assistantMessageID="other-step",
               **_usage(input=10, output=20, cost=0.1)),
        _event("session.execution.succeeded", 5, session="sid-other"),
    ]:
        other_session._record_and_dispatch(event)
    assert engine.usage("inv-other-session") is None
    other_state.settlement_confirmed = True
    assert engine.usage("inv-other-session").input == 10


def test_missing_fields_zero_fractional_and_failed_cancelled_usage() -> None:
    engine = OpenCodeEngine()
    session = _NativeSession("session-usage-2", Path("/tmp/home"), Path("/tmp/scratch"))
    session.sid = "sid-usage"
    state = _InvocationState(input_id="input-two")
    session._invocations["inv-two"] = state
    engine._sessions[session.session_ref] = session
    for event in [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="input-two"),
        _event("session.step.started", 3, assistantMessageID="assistant"),
        _event("session.step.failed", 4, assistantMessageID="assistant", tokens={
            "input": 0, "output": 0.5, "reasoning": 0, "cache": {"read": 0, "write": 0}
        }),
        _event("session.execution.failed", 5),
    ]:
        session._record_and_dispatch(event)
    state.settlement_confirmed = True
    usage = engine.usage("inv-two")
    assert usage is not None
    assert usage.input == 0 and usage.output == 0.5 and usage.cost_usd is None
    assert usage.steps == 1

    unknown = _InvocationState(input_id="missing")
    session._invocations["unknown"] = unknown
    assert engine.usage("unknown") is None

    cancelled = _InvocationState(input_id="input-cancelled")
    session._invocations["cancelled"] = cancelled
    for event in [
        _event("session.execution.started", 6),
        _event("session.inbox.delivered", 7, inboxID="input-cancelled"),
        _event("session.step.started", 8, assistantMessageID="cancelled-step"),
        _event("session.step.ended", 9, assistantMessageID="cancelled-step", **_usage(cost=0.125)),
        _event("session.execution.interrupted", 10),
    ]:
        session._record_and_dispatch(event)
    cancelled.settlement_confirmed = True
    assert engine.usage("cancelled").cost_usd == 0.125


def test_missing_operation_terminal_and_internal_totals_do_not_invent_usage() -> None:
    engine = OpenCodeEngine()
    session = _NativeSession("session-missing", Path("/tmp/home"), Path("/tmp/scratch"))
    session.sid = "sid-usage"
    state = _InvocationState(input_id="input-missing")
    session._invocations["missing-terminal"] = state
    engine._sessions[session.session_ref] = session
    for event in [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="input-missing"),
        _event("session.usage.updated", 3, **_usage(input=90, output=90, cost=90)),
        _event("session.step.started", 4, assistantMessageID="never-ended"),
        _event("session.execution.failed", 5),
    ]:
        session._record_and_dispatch(event)
    state.settlement_confirmed = True
    assert engine.usage("missing-terminal") is None


def test_compaction_usage_is_bound_to_its_declared_input_within_shared_interval() -> None:
    engine = OpenCodeEngine()
    session = _NativeSession("session-compaction-owner", Path("/tmp/home"), Path("/tmp/scratch"))
    session.sid = "sid-usage"
    state = _InvocationState(input_id="owned-input")
    session._invocations["owned-invocation"] = state
    engine._sessions[session.session_ref] = session

    for event in [
        _event("session.execution.started", 1),
        _event("session.inbox.delivered", 2, inboxID="foreign-input"),
        _event("session.compaction.started", 3, inputID="foreign-input"),
        _event("session.compaction.ended", 4, **_usage(input=999, cost=99)),
        _event("session.inbox.delivered", 5, inboxID="owned-input"),
        _event("session.step.started", 6, assistantMessageID="owned-step"),
        _event("session.step.ended", 7, assistantMessageID="owned-step",
               **_usage(input=1, output=0, reasoning=0, read=0, write=0, cost=1)),
        _event("session.execution.succeeded", 8),
    ]:
        session._record_and_dispatch(event)

    assert engine.usage("owned-invocation") is None
    state.settlement_confirmed = True
    usage = engine.usage("owned-invocation")
    assert usage is not None
    assert usage.input == 1
    assert usage.cost_usd == 1

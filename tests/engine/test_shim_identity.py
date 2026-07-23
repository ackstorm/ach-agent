# SPDX-License-Identifier: Apache-2.0
"""Identity checks for the SP1 back-compat shims (engine/client.py, engine/events.py).

Each shim re-exports names via `from X import Name as Name` (mypy --strict explicit
reexport). This must be a pure alias — importing through the old path has to resolve
to the exact same object as importing through the new one, not a copy/rebuild.
"""

from __future__ import annotations


def test_client_shim_reexports_are_identical_objects() -> None:
    from ach_agent.engine import client as shim
    from ach_agent.engine.opencode import client as real

    assert shim.OpenCodeClient is real.OpenCodeClient
    assert shim.find_free_port is real.find_free_port
    assert shim.release_port is real.release_port
    assert shim._reserved_ports is real._reserved_ports


def test_events_shim_reexports_are_identical_objects() -> None:
    from ach_agent.engine import events as shim
    from ach_agent.engine.base import events as base
    from ach_agent.engine.opencode import events as oc

    assert shim.EngineError is base.EngineError
    assert shim.InvocationTimeout is base.InvocationTimeout
    assert shim.OpenCodeToolUpdate is base.OpenCodeToolUpdate
    assert shim.OpenCodeUsage is base.OpenCodeUsage
    assert shim.ToolState is base.ToolState
    assert shim.ToolStateCompleted is base.ToolStateCompleted
    assert shim.ToolStateError is base.ToolStateError
    assert shim.ToolStateRunning is base.ToolStateRunning

    assert shim.OpenCodeEvent is oc.OpenCodeEvent
    assert shim.OpenCodeSessionError is oc.OpenCodeSessionError
    assert shim.OpenCodeSessionIdle is oc.OpenCodeSessionIdle
    assert shim.OpenCodeStreamReady is oc.OpenCodeStreamReady
    assert shim.OpenCodeTextUpdate is oc.OpenCodeTextUpdate
    assert shim.OpenCodeUserMessage is oc.OpenCodeUserMessage
    assert shim.ReplyAccumulator is oc.ReplyAccumulator
    assert shim._consume_events_from_response is oc._consume_events_from_response
    assert shim._SendFailed is oc._SendFailed
    assert shim.parse_opencode_event is oc.parse_opencode_event


def test_opencode_events_reexports_shared_vocab_from_base() -> None:
    # opencode/events.py itself passes EngineError/InvocationTimeout through from
    # base/events.py (it defines neither) — same identity requirement applies.
    from ach_agent.engine.base.events import EngineError, InvocationTimeout
    from ach_agent.engine.opencode import events as oc

    assert oc.EngineError is EngineError
    assert oc.InvocationTimeout is InvocationTimeout

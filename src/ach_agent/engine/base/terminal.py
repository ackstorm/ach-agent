# SPDX-License-Identifier: Apache-2.0
"""Engine-agnostic terminal contract (SP1 §4.3): text-extract + Pydantic + <=1 repair, plus
the step-budget wrap-up turn. Runs ONCE for every engine (matches the "structured output is
harness-validated" constraint). free_form channels (--tui) skip extraction."""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from ach_agent.engine.base.driver import EngineDriver
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer

log = structlog.get_logger(__name__)


def _terminal_object_hint(action: str) -> str:
    """The single terminal JSON object we ask the model to emit on a wrap/repair turn.

    a2a turns demand a2a_reply; async turns demand none. Showing only the ONE action the
    channel expects means an a2a repair turn never re-exposes 'none'."""
    if action == "a2a_reply":
        return '{"action":"a2a_reply","text":"..."}'
    return '{"action":"none","text":"..."}'


async def run_contract_turn(
    driver: EngineDriver,
    server: ManagedServer,
    *,
    conv_key: str,
    prompt: str,
    reuse: bool,
    sessions: MutableMapping[str, str],
    free_form: bool,
    terminal_action: str,
    terminal_retries: int,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
    max_tool_calls: int = 0,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ach_agent.engine.validator import extract_terminal

    stats = stats if stats is not None else {}
    result = await driver.run_turn(
        server, conv_key=conv_key, prompt=prompt, reuse=reuse, sessions=sessions,
        on_text=on_text, on_tool=on_tool, max_tool_calls=max_tool_calls, stats=stats,
    )
    text = result.text

    if result.aborted:
        # Step-budget abort: the turn was cut mid-tool-loop and usually lacks a terminal object.
        # Run ONE wrap-up turn (budget OFF, SAME session) so the model emits a clean terminal
        # object. Throwaway stats so recorded usage/session reflect the first turn (matches old
        # run_invocation, which passed no stats to the wrap-up consume).
        log.warning("step-budget abort — running wrap-up turn", session_id=conv_key)
        wrap = (
            "You have reached your tool-call budget for this turn. Do NOT call any more tools. "
            "Reply now with ONLY the terminal JSON object "
            f"({_terminal_object_hint(terminal_action)}) "
            "summarizing what you found and did."
        )
        result = await driver.run_turn(
            server, conv_key=conv_key, prompt=wrap, reuse=reuse, sessions=sessions,
            session_ref=result.session_ref, on_text=on_text, on_tool=on_tool,
            max_tool_calls=0, stats={},
        )
        text = result.text

    # Free-form (--tui): no terminal contract — return the raw reply verbatim.
    if free_form:
        return {"action": "none", "text": text}

    obj = extract_terminal(text)
    if obj is None and terminal_retries > 0:
        repair = f"Reply with ONLY a terminal JSON object: {_terminal_object_hint(terminal_action)}."
        result = await driver.run_turn(
            server, conv_key=conv_key, prompt=repair, reuse=reuse, sessions=sessions,
            session_ref=result.session_ref, on_text=None, on_tool=None, max_tool_calls=0, stats={},
        )
        obj = extract_terminal(result.text)
        text = result.text
    return obj if obj is not None else {"action": "none", "text": text}

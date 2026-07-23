# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.base.terminal import run_contract_turn


class _ScriptedDriver:
    """Returns queued TurnResults; records every run_turn call for assertions."""

    engine_type = "opencode"

    def __init__(self, results: list[TurnResult]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def run_turn(self, server: Any, **kw: Any) -> TurnResult:
        self.calls.append(kw)
        return self._results.pop(0)


async def test_happy_path_extracts_terminal_no_repair() -> None:
    drv = _ScriptedDriver([TurnResult(text='ok {"action":"none","text":"done"}', session_ref="ses_1")])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=False, terminal_action="none", terminal_retries=1, max_tool_calls=0, stats={},
    )
    assert obj == {"action": "none", "text": "done"}
    assert len(drv.calls) == 1  # no repair


async def test_free_form_returns_raw_text_no_extraction() -> None:
    drv = _ScriptedDriver([TurnResult(text="plain reply", session_ref="ses_1")])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=True, terminal_action="none", terminal_retries=1, max_tool_calls=0, stats={},
    )
    assert obj == {"action": "none", "text": "plain reply"}


async def test_aborted_runs_wrapup_on_same_session_ref() -> None:
    drv = _ScriptedDriver([
        TurnResult(text="partial, no terminal", session_ref="ses_9", aborted=True),
        TurnResult(text='{"action":"none","text":"wrapped"}', session_ref="ses_9"),
    ])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=False, terminal_action="none", terminal_retries=1, max_tool_calls=80, stats={},
    )
    assert obj == {"action": "none", "text": "wrapped"}
    assert drv.calls[1]["session_ref"] == "ses_9"      # wrap-up continued the SAME session
    assert drv.calls[1]["max_tool_calls"] == 0          # budget off on wrap-up


async def test_missing_terminal_triggers_one_repair() -> None:
    drv = _ScriptedDriver([
        TurnResult(text="no json here", session_ref="ses_2"),
        TurnResult(text='{"action":"a2a_reply","text":"fixed"}', session_ref="ses_2"),
    ])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=False, terminal_action="a2a_reply", terminal_retries=1, max_tool_calls=0, stats={},
    )
    assert obj == {"action": "a2a_reply", "text": "fixed"}
    assert drv.calls[1]["session_ref"] == "ses_2" and drv.calls[1]["on_text"] is None

# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import inspect
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


def _run_callable(drv: _ScriptedDriver) -> Any:
    session_ref: str | None = None

    async def run_turn(**kw: Any) -> TurnResult:
        nonlocal session_ref
        result = await drv.run_turn(object(), session_ref=session_ref, **kw)
        session_ref = result.session_ref
        return result

    return run_turn


async def test_happy_path_extracts_terminal_no_repair() -> None:
    drv = _ScriptedDriver(
        [TurnResult(text='ok {"action":"none","text":"done"}', session_ref="ses_1")]
    )
    obj = await run_contract_turn(
        _run_callable(drv),
        prompt="p",
        free_form=False,
        terminal_action="none",
        terminal_retries=1,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert obj == {"action": "none", "text": "done", "thoughts": ""}
    assert len(drv.calls) == 1  # no repair


async def test_free_form_returns_raw_text_no_extraction() -> None:
    drv = _ScriptedDriver([TurnResult(text="plain reply", session_ref="ses_1")])
    obj = await run_contract_turn(
        _run_callable(drv),
        prompt="p",
        free_form=True,
        terminal_action="none",
        terminal_retries=1,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert obj == {"action": "none", "text": "plain reply"}


async def test_aborted_runs_wrapup_on_same_session_ref() -> None:
    drv = _ScriptedDriver(
        [
            TurnResult(text="partial, no terminal", session_ref="ses_9", aborted=True),
            TurnResult(text='{"action":"none","text":"wrapped"}', session_ref="ses_9"),
        ]
    )
    obj = await run_contract_turn(
        _run_callable(drv),
        prompt="p",
        free_form=False,
        terminal_action="none",
        terminal_retries=1,
        on_text=None,
        on_tool=None,
        max_tool_calls=80,
        stats={},
    )
    assert obj == {"action": "none", "text": "wrapped", "thoughts": ""}
    assert drv.calls[1]["session_ref"] == "ses_9"  # wrap-up continued the SAME session
    assert drv.calls[1]["max_tool_calls"] == 0  # budget off on wrap-up


async def test_missing_terminal_triggers_one_repair() -> None:
    drv = _ScriptedDriver(
        [
            TurnResult(text="no json here", session_ref="ses_2"),
            TurnResult(text='{"action":"a2a_reply","text":"fixed"}', session_ref="ses_2"),
        ]
    )
    obj = await run_contract_turn(
        _run_callable(drv),
        prompt="p",
        free_form=False,
        terminal_action="a2a_reply",
        terminal_retries=1,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert obj == {"action": "a2a_reply", "text": "fixed", "thoughts": ""}
    assert drv.calls[1]["session_ref"] == "ses_2" and drv.calls[1]["on_text"] is None


async def test_terminal_accepts_the_http_run_turn_callable() -> None:
    calls: list[dict[str, Any]] = []

    async def run_turn(**kwargs: Any) -> TurnResult:
        calls.append(kwargs)
        return TurnResult(text='{"action":"none","text":"done"}', session_ref="ses-http")

    obj = await run_contract_turn(
        run_turn,
        prompt="p",
        free_form=False,
        terminal_action="none",
        terminal_retries=1,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert obj["text"] == "done"
    assert set(calls[0]) == {"prompt", "max_tool_calls", "on_text", "on_tool", "stats"}


def test_signature_canonical_matches_spec() -> None:
    sig = inspect.signature(run_contract_turn)
    kw_only_names = [
        p.name for p in sig.parameters.values() if p.kind == inspect.Parameter.KEYWORD_ONLY
    ]
    expected_kw_only = [
        "prompt",
        "free_form",
        "terminal_action",
        "terminal_retries",
        "on_text",
        "on_tool",
        "max_tool_calls",
        "stats",
    ]
    assert kw_only_names == expected_kw_only
    for name in ["on_text", "on_tool", "max_tool_calls", "stats"]:
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty, f"Parameter {name} must have no default"

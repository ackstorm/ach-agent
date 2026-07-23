# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import patch

import pytest

from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.opencode.driver import OpencodeDriver


class _FakeClient:
    """Stands in for OpenCodeClient — isinstance() check in run_turn is bypassed via patch."""


class _FakeServer:
    def __init__(self) -> None:
        self._client = _FakeClient()

    def is_alive(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _accept_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_turn does `isinstance(client, OpenCodeClient)`; make the fake pass.
    import ach_agent.engine.opencode.driver as drv

    monkeypatch.setattr(drv, "OpenCodeClient", _FakeClient, raising=False)


async def test_run_turn_reuse_creates_and_records_session() -> None:
    sessions: dict[str, str] = {}
    stats: dict[str, Any] = {}
    with (
        patch("ach_agent.engine.lifecycle._create_oc_session", return_value="ses_new") as mk,
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", return_value="hello") as cs,
    ):
        result = await OpencodeDriver().run_turn(
            _FakeServer(),
            conv_key="k1",
            prompt="p",
            reuse=True,
            sessions=sessions,
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats=stats,
        )
    assert result == TurnResult(text="hello", session_ref="ses_new", aborted=False)
    assert sessions["k1"] == "ses_new"
    assert stats["session_ref"] == "ses_new" and stats["oc_session_id"] == "ses_new"
    mk.assert_awaited_once()
    cs.assert_awaited_once()


async def test_run_turn_with_session_ref_bypasses_map() -> None:
    sessions: dict[str, str] = {}
    with (
        patch("ach_agent.engine.lifecycle._create_oc_session") as mk,
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", return_value="wrapped"),
    ):
        result = await OpencodeDriver().run_turn(
            _FakeServer(),
            conv_key="k1",
            prompt="wrap",
            reuse=True,
            sessions=sessions,
            session_ref="ses_fixed",
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats={},
        )
    assert result.session_ref == "ses_fixed"
    assert result.text == "wrapped"
    assert sessions == {}          # map never touched on the session_ref path
    mk.assert_not_awaited()        # no create on the continue path


def test_signature_canonical_matches_protocol() -> None:
    sig = inspect.signature(OpencodeDriver.run_turn)
    kw_only_names = [p.name for p in sig.parameters.values() if p.kind == inspect.Parameter.KEYWORD_ONLY]
    expected_kw_only = [
        "conv_key",
        "prompt",
        "reuse",
        "sessions",
        "session_ref",
        "on_text",
        "on_tool",
        "max_tool_calls",
        "stats",
    ]
    assert kw_only_names == expected_kw_only
    for name in ["on_text", "on_tool", "max_tool_calls", "stats"]:
        param = sig.parameters[name]
        assert param.default is inspect.Parameter.empty, f"Parameter {name} must have no default"

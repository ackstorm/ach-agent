"""Validator tests: single-object terminal contract extraction.

CONTRACT_v3: the terminal output is a single {"action",...,"text",...,"thoughts"}
object (NOT a list). extract_terminal finds the last such object in accumulated text.
"""
from __future__ import annotations

from ach_agent.engine.validator import A2AReply, NoneAction, extract_terminal


def test_extract_none_action():
    text = 'thinking...\n{"action":"none","text":"done","thoughts":"ok"}'
    obj = extract_terminal(text)
    assert obj == {"action": "none", "text": "done", "thoughts": "ok"}


def test_extract_a2a_reply():
    text = '{"action":"a2a_reply","text":"hello peer"}'
    obj = extract_terminal(text)
    assert obj["action"] == "a2a_reply"
    assert obj["text"] == "hello peer"


def test_extract_returns_none_when_absent():
    assert extract_terminal("no json here") is None


def test_none_action_model_defaults():
    m = NoneAction(action="none")
    assert m.text == "" and m.thoughts == ""


def test_a2a_reply_requires_text():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        A2AReply(action="a2a_reply")

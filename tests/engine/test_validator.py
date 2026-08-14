"""Validator tests: single-object terminal contract extraction.

Operator contract: the terminal output is a single {"action",...,"text",...,"thoughts"}
object (NOT a list). extract_terminal finds the last such object in accumulated text.
"""
from __future__ import annotations

from ach_agent.engine.validator import extract_terminal


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


def test_extract_terminal_tolerates_whitespace_after_brace():
    text = 'preamble\n{\n  "action": "none",\n  "text": "done"\n}'
    obj = extract_terminal(text)
    assert obj == {"action": "none", "text": "done"}


def test_extract_terminal_still_takes_the_last_object():
    text = '{"action":"none","text":"first"}\nthen\n{ "action":"none","text":"last"}'
    obj = extract_terminal(text)
    assert obj is not None
    assert obj["text"] == "last"


def test_extract_terminal_skips_trailing_prose_that_looks_like_an_opener():
    # A real terminal object followed by prose that merely mentions '{ "action"' (not
    # valid JSON) must not steal the match — the real object should still be returned.
    text = (
        '{"action":"none","text":"done","thoughts":"ok"}\n'
        'Note: a well-formed reply looks like { "action": "none", ... } — see the docs.'
    )
    obj = extract_terminal(text)
    assert obj == {"action": "none", "text": "done", "thoughts": "ok"}

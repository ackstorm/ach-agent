# SPDX-License-Identifier: Apache-2.0
"""CONTRACT §8: the harness validates the extracted terminal object against the Pydantic
model. A single object, NOT a list; unknown actions and wrong types are a MISS."""

from ach_agent.engine.validator import validate_terminal


def test_none_action_normalizes_missing_optional_fields():
    assert validate_terminal({"action": "none"}) == {
        "action": "none",
        "text": "",
        "thoughts": "",
    }


def test_a2a_reply_requires_text():
    assert validate_terminal({"action": "a2a_reply", "text": "done"}) == {
        "action": "a2a_reply",
        "text": "done",
        "thoughts": "",
    }
    assert validate_terminal({"action": "a2a_reply"}) is None


def test_unknown_action_is_a_miss():
    assert validate_terminal({"action": "delete_repo", "text": "x"}) is None


def test_extra_fields_are_a_miss():
    assert validate_terminal({"action": "none", "text": "x", "rm": "-rf"}) is None


def test_wrong_type_is_a_miss():
    assert validate_terminal({"action": "none", "text": 42}) is None


def test_none_input_passes_through():
    assert validate_terminal(None) is None

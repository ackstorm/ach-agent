# SPDX-License-Identifier: Apache-2.0
"""run_contract_turn must validate, retry ONCE on a miss, and log — never silently
promote an unvalidated blob to a terminal result (CONTRACT §8)."""

from dataclasses import dataclass

from ach_agent.engine.base.terminal import run_contract_turn


@dataclass
class _Result:
    text: str
    aborted: bool = False
    session_ref: str | None = None


class _FakeDriver:
    """Returns each scripted text in order, recording every prompt it was given."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = list(texts)
        self.prompts: list[str] = []

    async def run_turn(self, server, *, prompt, **kwargs):
        self.prompts.append(prompt)
        return _Result(text=self._texts.pop(0))


async def _run(driver, action="none"):
    return await run_contract_turn(
        driver,
        server=object(),
        conv_key="k",
        prompt="do the thing",
        reuse=False,
        sessions={},
        free_form=False,
        terminal_action=action,
        terminal_retries=1,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )


async def test_valid_object_is_returned_normalized():
    driver = _FakeDriver(['prose {"action":"none","text":"ok"}'])
    assert await _run(driver) == {"action": "none", "text": "ok", "thoughts": ""}
    assert len(driver.prompts) == 1  # no repair turn


async def test_unknown_action_triggers_exactly_one_repair():
    driver = _FakeDriver(
        ['{"action":"nuke","text":"x"}', '{"action":"none","text":"repaired"}']
    )
    assert await _run(driver) == {"action": "none", "text": "repaired", "thoughts": ""}
    assert len(driver.prompts) == 2
    assert "terminal JSON object" in driver.prompts[1]


async def test_invalid_after_retry_falls_back_to_none_with_raw_text():
    driver = _FakeDriver(["no json at all", "still no json"])
    out = await _run(driver, action="a2a_reply")
    assert out["action"] == "none"  # a2a path maps this to a FAILED callback
    assert out["text"] == "still no json"
    assert len(driver.prompts) == 2


async def test_free_form_skips_validation_entirely():
    driver = _FakeDriver(["just prose, no object"])
    out = await run_contract_turn(
        driver,
        server=object(),
        conv_key="k",
        prompt="p",
        reuse=False,
        sessions={},
        free_form=True,
        terminal_action="none",
        terminal_retries=1,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert out == {"action": "none", "text": "just prose, no object"}

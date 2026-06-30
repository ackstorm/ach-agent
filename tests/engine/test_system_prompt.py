import pytest
from pathlib import Path
from ach_agent.config.schema import PromptBlock
from ach_agent.main import resolve_system_prompt


def test_text_form_returns_inline(tmp_path):
    pb = PromptBlock.model_validate({"system": {"type": "text", "text": "persona X"}})
    assert resolve_system_prompt(pb, tmp_path) == "persona X"


def test_none_returns_empty(tmp_path):
    assert resolve_system_prompt(PromptBlock.model_validate({}), tmp_path) == ""
    assert resolve_system_prompt(None, tmp_path) == ""


def test_file_form_reads_under_state(tmp_path):
    f = tmp_path / "prompts" / "p" / "x.md"
    f.parent.mkdir(parents=True)
    f.write_text("from file", encoding="utf-8")
    pb = PromptBlock.model_validate({"system": {"type": "file", "file": "prompts/p/x.md"}})
    assert resolve_system_prompt(pb, tmp_path) == "from file"


def test_file_missing_exits(tmp_path):
    pb = PromptBlock.model_validate({"system": {"type": "file", "file": "prompts/none.md"}})
    with pytest.raises(SystemExit):
        resolve_system_prompt(pb, tmp_path)


def test_file_symlink_escape_exits(tmp_path):
    # a file that resolves outside .ach-state via a symlink is rejected at read time
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "evil.md").symlink_to(outside)
    pb = PromptBlock.model_validate({"system": {"type": "file", "file": "evil.md"}})
    with pytest.raises(SystemExit):
        resolve_system_prompt(pb, state)

# SPDX-License-Identifier: MIT
from ach_runtime.engine.opencode import OpenCodeEngine


async def test_tui_command_is_none_until_the_session_is_started(tmp_path) -> None:
    engine = OpenCodeEngine()
    assert await engine.tui_command("ref") is None
    session = engine._session_for("ref", tmp_path / "home", tmp_path / "scratch")
    session.url = "http://127.0.0.1:41000"
    assert await engine.tui_command("ref") is None  # no sid / password yet
    session.sid = "ses_1"
    session.password = "pw"
    command = await engine.tui_command("ref")
    assert command.args == ["--server", "http://127.0.0.1:41000", "--session", "ses_1"]
    assert command.env == {"OPENCODE_SERVER_PASSWORD": "pw"}
    assert command.clean_env is False

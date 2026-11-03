# SPDX-License-Identifier: MIT
from ach_runtime.engine.opencode import OpenCodeEngine


def test_attach_target_is_none_until_the_session_is_started(tmp_path) -> None:
    engine = OpenCodeEngine()
    assert engine.attach_target("ref") is None
    session = engine._session_for("ref", tmp_path / "home", tmp_path / "scratch")
    session.url = "http://127.0.0.1:41000"
    assert engine.attach_target("ref") is None  # no sid / password yet
    session.sid = "ses_1"
    session.password = "pw"
    assert engine.attach_target("ref") == ("http://127.0.0.1:41000", "ses_1", "pw")

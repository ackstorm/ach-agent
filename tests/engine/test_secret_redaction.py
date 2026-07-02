# SPDX-License-Identifier: Apache-2.0
from ach_agent.engine.sanitized_env import make_redact_secret_env_processor


def test_secret_env_value_redacted(monkeypatch):
    monkeypatch.setenv("ACH_SECRET_X", "topsecret")
    proc = make_redact_secret_env_processor(["ACH_SECRET_X"])
    out = proc(None, "info", {"event": "leak topsecret here"})
    assert "topsecret" not in out["event"]
    assert "[REDACTED]" in out["event"]

# SPDX-License-Identifier: Apache-2.0
from ach_agent.engine.sanitized_env import make_redact_secret_env_processor


def test_secret_env_value_redacted(monkeypatch):
    monkeypatch.setenv("ACH_SECRET_X", "topsecret")
    proc = make_redact_secret_env_processor(["ACH_SECRET_X"])
    out = proc(None, "info", {"event": "leak topsecret here"})
    assert "topsecret" not in out["event"]
    assert "[REDACTED]" in out["event"]


def test_multiple_secrets_in_one_field_all_redacted(monkeypatch):
    """finding 3: redacting the second registered secret must not undo the first's
    redaction — each replace() must accumulate onto the previous result."""
    monkeypatch.setenv("ACH_SECRET_X", "synthetic-first")
    monkeypatch.setenv("ACH_SECRET_Y", "synthetic-second")
    proc = make_redact_secret_env_processor(["ACH_SECRET_X", "ACH_SECRET_Y"])
    out = proc(None, "info", {"event": "synthetic-first synthetic-second"})
    assert out["event"] == "[REDACTED] [REDACTED]"

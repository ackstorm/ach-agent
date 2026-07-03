"""ek_ redaction tests: SEC-01.

Implements the CI secret-leakage test required by the plan's threat model (T-00-EK).
"""
from __future__ import annotations

import os

import pytest


# ---------------------------------------------------------------------------
# SEC-01: fake ek_ value never appears in log output
# ---------------------------------------------------------------------------


def test_ek_never_logged(capsys: pytest.CaptureFixture[str], fake_ek_env: None) -> None:
    """SEC-01: the fake ek_ sentinel never appears in captured stdout/stderr.

    CI secret-leakage test. The fake_ek_env fixture injects
    "ek_test_sentinel_do_not_log" as ACH_API_KEY. Any code path that logs
    env dicts, tracebacks, or subprocess launch args must not leak the value.

    redact_ek_processor protects against this.
    """
    import structlog

    from ach_agent.engine.sanitized_env import configure_logging, redact_ek_processor

    # Configure structlog with redaction processor
    configure_logging()

    # Build env from os.environ (which has the fake ek_ key via fake_ek_env fixture)
    env = os.environ.copy()

    # Log the raw env dict — redact_ek_processor must scrub the sentinel
    log = structlog.get_logger("test")
    log.info("env dict", env=env)

    # Also log a dict directly that contains an ek_ value
    log.info("env dict logging", env={"ACH_API_KEY": "ek_test_sentinel_do_not_log"})

    out, err = capsys.readouterr()
    assert "ek_test_sentinel_do_not_log" not in out, (
        "Sentinel leaked in stdout — redact_ek_processor not applied"
    )
    assert "ek_test_sentinel_do_not_log" not in err, (
        "Sentinel leaked in stderr — redact_ek_processor not applied"
    )


def test_redact_ek_processor_string_value() -> None:
    """redact_ek_processor replaces ek_ token in string values."""
    from ach_agent.engine.sanitized_env import redact_ek_processor

    event_dict = {"key": "ek_abc123_some_token", "other": "normal"}
    result = redact_ek_processor(None, "info", event_dict)
    assert result["key"] == "[REDACTED]"
    assert result["other"] == "normal"


def test_redact_ek_processor_nested_dict() -> None:
    """redact_ek_processor recurses one level into dict values."""
    from ach_agent.engine.sanitized_env import redact_ek_processor

    event_dict = {"env": {"ACH_API_KEY": "ek_nested_value_xyz", "OTHER": "ok"}}
    result = redact_ek_processor(None, "info", event_dict)
    assert result["env"]["ACH_API_KEY"] == "[REDACTED]"
    assert result["env"]["OTHER"] == "ok"


# ---------------------------------------------------------------------------
# CR-03: ek_ redaction must catch mid-token secrets (gap-closure 02-05)
# ---------------------------------------------------------------------------


def test_redact_ek_processor_mid_token_secret() -> None:
    """CR-03: ek_ embedded after a word character must still be redacted.

    The leading \\b in the old pattern _EK_PATTERN = re.compile(r"\\bek_[A-Za-z0-9_\\-]+")
    requires a word boundary before 'ek_'. When ek_ is preceded by another word
    character (e.g. 'tokenek_live_ABC123') the \\b fails and the token is NOT redacted.

    This test FAILS against the old pattern (\\b present) and passes only after
    the fix (drop leading \\b: re.compile(r"ek_[A-Za-z0-9_\\-]+")).
    """
    from ach_agent.engine.sanitized_env import redact_ek_processor

    # Mid-token: ek_ is preceded by a word character ('n') — old \\b fails here
    event_dict = {"error": "upstream error: tokenek_live_ABC123 rejected"}
    result = redact_ek_processor(None, "info", event_dict)
    assert "[REDACTED]" in result["error"], (
        "CR-03: ek_ token embedded mid-word must be redacted"
    )
    assert "ek_live_ABC123" not in result["error"], (
        "CR-03: raw ek_ value must NOT appear in redacted output"
    )

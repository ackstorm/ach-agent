"""SanitizedEnv and ek_ redaction tests: SEC-01.

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

    SanitizedEnv and redact_ek_processor protect against this.
    """
    import structlog

    from ach_agent.engine.sanitized_env import (
        SanitizedEnv,
        configure_logging,
        redact_ek_processor,
    )

    # Configure structlog with redaction processor
    configure_logging()

    # Build env from os.environ (which has the fake ek_ key via fake_ek_env fixture)
    env = os.environ.copy()

    # Log the SanitizedEnv repr — must not leak the sentinel
    sanitized = SanitizedEnv(env)
    log = structlog.get_logger("test")
    log.info("env repr", env_repr=repr(sanitized))

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


def test_sanitized_env_repr_masks_ek() -> None:
    """SanitizedEnv.__repr__ masks ek_ values."""
    from ach_agent.engine.sanitized_env import SanitizedEnv

    env = {"ACH_API_KEY": "ek_super_secret_value", "NORMAL_VAR": "visible"}
    sanitized = SanitizedEnv(env)
    repr_str = repr(sanitized)
    assert "ek_super_secret_value" not in repr_str, "repr must not leak ek_ value"
    assert "[REDACTED]" in repr_str, "repr must show [REDACTED] for ek_ values"
    assert "NORMAL_VAR" in repr_str, "repr should show non-secret keys"
    assert "visible" in repr_str, "repr should show non-secret values"


def test_sanitized_env_as_dict_returns_real_values() -> None:
    """SanitizedEnv.as_dict() returns the real env dict (for subprocess launch)."""
    from ach_agent.engine.sanitized_env import SanitizedEnv

    env = {"ACH_API_KEY": "ek_real_key_for_subprocess", "NORMAL": "value"}
    sanitized = SanitizedEnv(env)
    real = sanitized.as_dict()
    # The real dict must have the actual value (subprocess needs it)
    assert real["ACH_API_KEY"] == "ek_real_key_for_subprocess"
    assert real["NORMAL"] == "value"


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

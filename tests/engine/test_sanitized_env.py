"""ek_ redaction tests: SEC-01.

Implements the CI secret-leakage test required by the plan's threat model (T-00-EK).
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_public_log_level_enables_debug(monkeypatch: pytest.MonkeyPatch) -> None:
    from ach_agent.engine.sanitized_env import _resolve_log_level

    monkeypatch.delenv("ACH_LOG_LEVEL", raising=False)
    monkeypatch.setenv("LOG_LEVEL", "debug")

    assert _resolve_log_level() == logging.DEBUG


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

    from ach_agent.engine.sanitized_env import configure_logging

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
    assert "[REDACTED]" in result["error"], "CR-03: ek_ token embedded mid-word must be redacted"
    assert "ek_live_ABC123" not in result["error"], (
        "CR-03: raw ek_ value must NOT appear in redacted output"
    )


@pytest.mark.parametrize("builder", ["opencode", "pi"])
def test_native_child_receives_only_explicit_engine_env(
    builder: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ach_agent.engine.base.driver import EngineConfig

    monkeypatch.setenv("E_OWNED_MARKER", "engine-value")
    monkeypatch.setenv("H_MANAGED_MARKER", "must-not-cross")
    config = EngineConfig(engine_env={"E_OWNED_MARKER": "engine-value"})
    if builder == "opencode":
        from ach_agent.engine.lifecycle import build_opencode_env

        env = build_opencode_env(tmp_path / "home", config, tmp_path / "config.json")
    else:
        from ach_agent.engine.pi.config import build_pi_env

        env = build_pi_env(tmp_path / "pi", config)
    observed = subprocess.check_output(
        [
            sys.executable,
            "-c",
            "import os; print(os.getenv('E_OWNED_MARKER','')); "
            "print(os.getenv('H_MANAGED_MARKER',''))",
        ],
        env=env,
        text=True,
    ).splitlines()
    assert observed == ["engine-value", ""]


def test_split_engine_values_reach_native_child_from_e_ambient_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """H-resolved values reach the child even when E has no ambient values."""
    from ach_agent.boot.roles import build_role_configs
    from ach_agent.config.schema import AgentConfig

    cfg = AgentConfig.model_validate(
        {
            "schemaVersion": "1",
            "agent": {"name": "split-env"},
            "model": {"name": "openai.gpt-5", "type": "openai"},
            "capability": {"ach": {"baseUrl": "https://ach.example.test"}},
            "engine": {"forwardEnv": ["DEBUG", "CUSTOM_TOOL_TOKEN", "ACH_TOKEN"]},
            "channels": [],
        }
    )
    monkeypatch.setenv("DEBUG", "harness-value")
    monkeypatch.setenv("CUSTOM_TOOL_TOKEN", "harness-token")
    _channels, public = build_role_configs(cfg)
    assert public["engineEnvNames"] == ["DEBUG", "CUSTOM_TOOL_TOKEN"]
    engine_env = os.environ.copy()
    engine_env["DEBUG"] = "engine-value"
    engine_env["CUSTOM_TOOL_TOKEN"] = "engine-token"
    engine_env["ACH_TOKEN"] = "managed-token"
    script = (
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        "from ach_agent.execution.service import _engine_config\n"
        "from ach_agent.execution.wire import PublicEngineConfig\n"
        "from ach_agent.engine.lifecycle import build_opencode_env\n"
        "public = PublicEngineConfig.model_validate_json(sys.argv[3])\n"
        "env = build_opencode_env(Path(sys.argv[1]), "
        "_engine_config(public), Path(sys.argv[2]))\n"
        "print(subprocess.check_output([sys.executable, '-c', "
        '\'import os; print(os.getenv("DEBUG", "")); '
        'print(os.getenv("CUSTOM_TOOL_TOKEN", "")); '
        'print(os.getenv("ACH_TOKEN", ""))\'], '
        "env=env, text=True), end='')\n"
    )
    observed = subprocess.check_output(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "home"),
            str(tmp_path / "config.json"),
            json.dumps(public),
        ],
        env=engine_env,
        text=True,
    )
    assert observed == "engine-value\nengine-token\n\n"

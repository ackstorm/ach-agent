"""CONTRACT §6.10: secret-hygiene invariant (the headline v3 invariant).

Invariant: the ek_ bearer NEVER appears in opencode.json, opencode's env, or logs.
opencode points only at the localhost proxies (127.0.0.1); the proxy injects the ek_.

This is a regression lock over Plan 2 (write_opencode_config localhost mode +
the redact_ek_processor installed by configure_logging).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def test_ek_never_in_opencode_json(tmp_path: Path, monkeypatch: Any) -> None:
    """§6.10: with the localhost-proxy fields set, opencode.json carries no ek_ / ACH URL.

    The proxy injects the ek_; opencode.json must point only at 127.0.0.1.
    """
    monkeypatch.setenv("ACH_TOKEN", "ek_conformance_secret_value")
    from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config

    cfg = EngineConfig(
        model="openai.gpt-5",
        provider="openai",
        model_base_url="http://127.0.0.1:9001/v1",
        mcp_local_urls={"m": "http://127.0.0.1:9002/mcp/m"},
    )
    write_opencode_config(tmp_path, cfg)
    blob = (tmp_path / ".config" / "opencode" / "opencode.json").read_text(encoding="utf-8")

    assert "ek_conformance_secret_value" not in blob, "§6.10: ek_ must never be in opencode.json"
    assert "127.0.0.1" in blob, "§6.10: opencode must point at the local proxy"


def test_ek_redacted_in_logs(capsys: Any) -> None:
    """§6.10 / SEC-01: an ek_ token passed to a log call is redacted before render.

    The redact_ek_processor (installed by configure_logging) matches the `ek_`
    bearer prefix and substitutes [REDACTED] before the renderer runs.
    """
    import structlog

    from ach_agent.engine.sanitized_env import configure_logging

    configure_logging()
    # Both separators must be redacted: real ACH keys use `ek-` (dash); earlier code
    # only matched `ek_` (underscore) and leaked the live bearer (confirmed vs real ACH).
    structlog.get_logger("conformance").info(
        "boot", token="ek_underscore_secret", bearer="ek-DASH-real-format-secret"
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "ek_underscore_secret" not in combined, "§6.10: ek_ token must be redacted in logs"
    assert "ek-DASH-real-format-secret" not in combined, (
        "§6.10: real ACH ek- (dash) key must be redacted in logs"
    )
    assert "[REDACTED]" in combined, "§6.10: the redaction marker must be present"

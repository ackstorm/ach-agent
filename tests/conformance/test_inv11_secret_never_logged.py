"""CONTRACT §6 / SC#2 superset: Secret never logged (authoritative conformance test).

Invariant (SC#2 extra): ek_ and GITLAB_TOKEN values never appear in log output.
"""
from __future__ import annotations

from io import StringIO

import pytest
import structlog


def _configure_json_logging_with_redaction(stream: StringIO) -> None:
    """Configure structlog with ek_ and GITLAB_TOKEN redaction processors, JSON output.

    Mirrors the helper in tests/actions/test_gitlab_comment.py — reused for the
    authoritative conformance assertion (SEC-01 + SEC-03).
    """
    from ach_agent.engine.sanitized_env import (
        redact_ek_processor,
        redact_gitlab_token_processor,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_ek_processor,              # SEC-01: ek_ redaction
            redact_gitlab_token_processor,    # SEC-03: GITLAB_TOKEN redaction
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
    )


def test_inv11_secret_never_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    """SC#2 extra: ek_ and GITLAB_TOKEN sentinels never in log output (authoritative).

    CONTRACT perspective (SEC-01 + SEC-03): both secret patterns must be scrubbed
    from the structlog output BEFORE the rendered log line reaches any sink.
    This test asserts the observable behavior: neither sentinel value appears in
    the captured log output, regardless of how many fields or log levels are used.
    """
    # ek_ sentinel: models ACH_API_KEY (SEC-01)
    ek_sentinel = "ek_test_sentinel_do_not_log"
    monkeypatch.setenv("ACH_API_KEY", ek_sentinel)

    # GITLAB_TOKEN sentinel: models GitLab PAT (SEC-03)
    gl_sentinel = "fake_gl_token_sentinel_do_not_log"
    monkeypatch.setenv("GITLAB_TOKEN", gl_sentinel)

    stream = StringIO()
    _configure_json_logging_with_redaction(stream)

    log = structlog.get_logger("conformance.sec")

    # Emit log events that include both sentinels in various field positions.
    # The redaction pipeline must scrub them before they reach the stream.
    log.info(
        "test.event.sec01",
        api_key=ek_sentinel,
        detail=f"using key {ek_sentinel}",
    )
    log.error(
        "test.event.sec03",
        token=gl_sentinel,
        response=f"invalid token {gl_sentinel}",
    )
    log.warning(
        "test.event.combined",
        api_key=ek_sentinel,
        token=gl_sentinel,
        msg=f"auth failed ek={ek_sentinel} gl={gl_sentinel}",
    )

    output = stream.getvalue()

    # Neither sentinel must appear in any rendered log line.
    assert ek_sentinel not in output, (
        f"SEC-01 violated (SC#2): ek_ sentinel '{ek_sentinel}' found in log output. "
        "The redact_ek_processor must scrub all ek_ values before render.\n"
        f"Log output:\n{output}"
    )
    assert gl_sentinel not in output, (
        f"SEC-03 violated (SC#2): GITLAB_TOKEN sentinel '{gl_sentinel}' found in log output. "
        "The redact_gitlab_token_processor must scrub all GITLAB_TOKEN values before render.\n"
        f"Log output:\n{output}"
    )

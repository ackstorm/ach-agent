# SPDX-License-Identifier: MIT
"""Every log record of the control process is scrubbed: ek_/ek-, compact JWS (an ACH JWT, a
ServiceAccount token) and provider keys, in the message, its arguments and tracebacks."""

from __future__ import annotations

import logging

import pytest

from ach_runtime.observability.runs import install_redaction, redact

JWT = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJla2lkXzEifQ.c2lnbmF0dXJl"
SA = "eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJpc3MiOiJrdWJlcm5ldGVzIn0.sig-part_1"
EK = "ek_live_0123456789abcdef"


@pytest.fixture
def redacting():
    previous = logging.getLogRecordFactory()
    logging.setLogRecordFactory(logging.LogRecord)
    install_redaction()
    yield
    logging.setLogRecordFactory(previous)


def test_redact_scrubs_jws_and_keys() -> None:
    assert redact(f"a {JWT} b {SA} c {EK} d sk-abc123") == "a [redacted] b [redacted] c [redacted] d [redacted]"


def test_every_logger_message_argument_and_traceback_is_scrubbed(redacting, caplog) -> None:
    caplog.set_level(logging.INFO)
    for name in ("ach_runtime.facades._memory_client", "ach_runtime.facades.a2a",
                 "ach_runtime.harness.keys", "uvicorn.error", "httpx"):
        logging.getLogger(name).warning("call failed (%s) %s", RuntimeError(f"bad header {JWT}"), EK)
    try:
        raise RuntimeError(f"token {SA}")
    except RuntimeError:
        logging.getLogger("ach_runtime.harness.control").exception("boom %s", JWT)
    text = caplog.text + "".join(record.exc_text or "" for record in caplog.records)
    for secret in (JWT, SA, EK, "eyJ"):
        assert secret not in text
    assert "[redacted]" in text and "RuntimeError" in text


def test_install_is_idempotent(redacting) -> None:
    factory = logging.getLogRecordFactory()
    install_redaction()
    assert logging.getLogRecordFactory() is factory


@pytest.mark.parametrize("text", ["week-long", "task-runner", "disk-usage", "risk-free", "desk-3"])
def test_redact_leaves_words_containing_ek_or_sk_alone(text: str) -> None:
    assert redact(text) == text


@pytest.mark.parametrize("text", [EK, f"x-ach-key={EK.replace('ek_', 'ek-')}", "Bearer sk-abc123", JWT])
def test_redact_still_scrubs_credentials_in_context(text: str) -> None:
    assert "[redacted]" in redact(text) and "abc123" not in redact(text) and "0123456789" not in redact(text)

# SPDX-License-Identifier: MIT
"""Safe, process-local reporting records for admitted invocations."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from ach_runtime.execution.wire import JsonValue

_SECRET = re.compile(
    r"((?<![A-Za-z0-9])ek[-_][A-Za-z0-9_\-]+|(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]+"
    # A compact JWS (an ACH JWT, a ServiceAccount token): the base64url JSON header starts eyJ.
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*)"
)


_registered: tuple[str, ...] = ()
_MIN_REGISTERED_SECRET = 8


def register_secrets(values: Iterable[str]) -> None:
    """Add process secrets (resolved upstream headers) to log redaction. Values shorter
    than 8 characters are ignored: replacing them would corrupt ordinary text."""
    global _registered
    parts: set[str] = set()
    for value in values:
        parts.add(value)
        # `Bearer <token>` style values: the token alone can reach a log without its scheme.
        if " " in value.strip():
            parts.add(value.strip().split(" ", 1)[1].strip())
    merged = set(_registered) | {value for value in parts if len(value) >= _MIN_REGISTERED_SECRET}
    _registered = tuple(sorted(merged, key=len, reverse=True))


def redact(text: str) -> str:
    """Scrub ACH keys, compact JWS and provider keys, plus registered process secrets, without truncating the value."""
    text = _SECRET.sub("[redacted]", text)
    for value in _registered:
        text = text.replace(value, "[redacted]")
    return text


# Explicit allowlist of record_run/alarm's own fields (policy gap): the formatter below
# renders only these -- never arbitrary LogRecord extras, a request body, an exception, or
# an authenticated URL, regardless of what a future caller might pass through `extra=`.
_SAFE_LOG_FIELDS = (
    "agent", "environment", "invocation_id", "run_id", "actor", "event_id", "channel",
    "workspace_ref", "session_ref", "config_version", "model", "outcome", "duration_ms",
    "usage_source", "usage_basis", "usage_input", "usage_output", "usage_reasoning",
    "usage_cache_read", "usage_cache_write", "usage_cost_usd", "kind",
)
_OWNED_HANDLER_ATTR = "_ach_owned_handler"


class _SafeRecordFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "level": record.levelname, "logger": record.name, "message": record.getMessage(),
        }
        for field in _SAFE_LOG_FIELDS:
            if field in record.__dict__:
                value = record.__dict__[field]
                payload[field] = redact(value) if isinstance(value, str) else value
        return json.dumps(payload, default=str)


class _StderrHandler(logging.StreamHandler):
    """Rebinds `sys.stderr` on every emit rather than once at construction, so a process
    that reopens stderr (or a test harness that redirects it) still reaches the live
    stream instead of a stale, possibly-closed one."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


_REDACTING = "_ach_redacting"
_TRACEBACK = logging.Formatter()


def install_redaction() -> None:
    """Scrub every log record of the process when it is created: message, arguments, traceback
    and stack. A record factory rather than a handler filter, so uvicorn's handlers and any
    handler configured later are covered too. Idempotent."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, _REDACTING, False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed format string must not break the caller
            message = str(record.msg)
        record.msg, record.args = redact(message), None
        if record.exc_info:
            record.exc_text = redact(_TRACEBACK.formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return record

    setattr(factory, _REDACTING, True)
    logging.setLogRecordFactory(factory)


def configure_logging() -> None:
    """Install one owned INFO stderr handler on this module's logger, rendering only the
    safe allowlisted record_run/alarm fields through a current stdlib formatter (no
    structlog, no logging package/platform). Idempotent -- a repeated call never installs
    a second owned handler. Never touches uvicorn's own loggers, and leaves propagation on
    so caplog (which attaches at the root) keeps working in tests. Installs the process-wide
    redaction first."""
    install_redaction()
    # httpx logs every request at INFO; the control loop polls each sandbox every few
    # seconds, which buries every useful line.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)
    if any(getattr(handler, _OWNED_HANDLER_ATTR, False) for handler in logger.handlers):
        return
    handler = _StderrHandler(stream=sys.stderr)
    handler.setFormatter(_SafeRecordFormatter())
    setattr(handler, _OWNED_HANDLER_ATTR, True)
    logger.addHandler(handler)

UsageSource = Literal["opencode"]
UsageBasis = Literal["engine_estimate"]
RunOutcome = Literal["completed", "failed", "cancelled", "skipped", "unknown"]
AnomalyKind = Literal[
    "snapshot_failed", "persistence_failed", "restore_failed", "start_failed",
    "start_outcome_unknown", "run_outcome_unknown", "pod_unhealthy",
    "configuration_invalid", "limit_reached", "artifact_publication_failed",
    "suspend_hook_failed", "usage_invalid", "workspace_start_failed", "workspace_stop_failed",
    "workspace_unhosted", "workspace_retire_timeout", "lifecycle_tick_failed",
    "gateway_unavailable",
]


@dataclass(frozen=True, slots=True)
class RunUsage:
    source: UsageSource
    basis: UsageBasis
    input: float | None
    output: float | None
    reasoning: float | None
    cache_read: float | None
    cache_write: float | None
    cost_usd: float | None
    steps: int | None

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "source": self.source,
            "basis": self.basis,
            "input": self.input,
            "output": self.output,
            "reasoning": self.reasoning,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
            "cost_usd": self.cost_usd,
            "steps": self.steps,
        }


@dataclass(frozen=True, slots=True)
class RunRecord:
    agent: str
    environment: str
    invocation_id: str
    run_id: str | None
    actor: str | None
    event_id: str | None
    channel: str
    workspace_ref: str
    session_ref: str
    config_version: str
    model: str
    duration_ms: float | None
    outcome: RunOutcome
    usage: RunUsage | None


def finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def record_run(record: RunRecord) -> None:
    """Emit a completion record containing only correlation and numeric usage fields."""
    from ach_runtime.observability.metrics import observe

    fields = {
        "agent": record.agent,
        "environment": record.environment,
        "invocation_id": record.invocation_id,
        "run_id": record.run_id,
        "actor": record.actor,
        "event_id": record.event_id,
        "channel": record.channel,
        "workspace_ref": record.workspace_ref,
        "session_ref": record.session_ref,
        "config_version": record.config_version,
        "model": record.model,
        "outcome": record.outcome,
        "duration_ms": record.duration_ms,
        "usage_source": record.usage.source if record.usage else None,
        "usage_basis": record.usage.basis if record.usage else None,
        "usage_input": record.usage.input if record.usage else None,
        "usage_output": record.usage.output if record.usage else None,
        "usage_reasoning": record.usage.reasoning if record.usage else None,
        "usage_cache_read": record.usage.cache_read if record.usage else None,
        "usage_cache_write": record.usage.cache_write if record.usage else None,
        "usage_cost_usd": record.usage.cost_usd if record.usage else None,
    }
    logging.getLogger(__name__).info(
        "invocation completed",
        extra={key: redact(value) if isinstance(value, str) else value for key, value in fields.items()},
    )
    observe(record)


def alarm(
    kind: AnomalyKind,
    *,
    invocation_id: str | None = None,
    workspace_ref: str | None = None,
    session_ref: str | None = None,
) -> None:
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL

    ANOMALIES_TOTAL.labels(kind=kind).inc()
    logging.getLogger(__name__).error(
        "ACH operational anomaly",
        extra={
            "kind": kind,
            "invocation_id": invocation_id,
            "workspace_ref": workspace_ref,
            "session_ref": session_ref,
        },
    )

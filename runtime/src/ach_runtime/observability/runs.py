# SPDX-License-Identifier: MIT
"""Safe, process-local reporting records for admitted invocations."""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from dataclasses import dataclass
from typing import Literal

from ach_runtime.execution.wire import JsonValue

_SECRET = re.compile(r"(ek[-_][A-Za-z0-9_\-]+|sk-[A-Za-z0-9_\-]+)")


def redact(text: str) -> str:
    """Scrub legacy ACH bearer and provider tokens without truncating the value."""
    return _SECRET.sub("[redacted]", text)


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


def configure_logging() -> None:
    """Install one owned INFO stderr handler on this module's logger, rendering only the
    safe allowlisted record_run/alarm fields through a current stdlib formatter (no
    structlog, no logging package/platform). Idempotent -- a repeated call never installs
    a second owned handler. Never touches uvicorn's own loggers, and leaves propagation on
    so caplog (which attaches at the root) keeps working in tests."""
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

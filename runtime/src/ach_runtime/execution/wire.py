# SPDX-License-Identifier: MIT
"""Task 4 wire contract: StartRequest/Started/RunStatus (contract §§3-4).

Replaces old-ach-agent's Acquire/Turn controller protocol — this is a mini-harness that
starts one event per session and reports its status, not a long-lived controller stream.
Retains `_WireModel`'s strict/frozen/extra-forbid/finite-JSON discipline from
old-ach-agent/src/ach_agent/execution/wire.py, and this repo's camelCase-wire /
alias-only-construction convention (`config/public.py`'s `validate_by_alias=True,
validate_by_name=False`); `workspace_ref`/`session_ref` are opaque strings minted by
Task 3's harness.refs — never imported here.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator


def _finite_json(value: JsonValue) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for child in value.values():
            _finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _finite_json(child)
    return value


class _WireModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        validate_by_alias=True,
        validate_by_name=False,
        hide_input_in_errors=True,
    )


def _parse_iso_datetime(value: object) -> object:
    # `strict=True` blocks pydantic's usual lenient str->datetime coercion; this "before"
    # validator restores JSON-wire ISO-8601 parsing (contract wire is JSON, not a Python
    # datetime) while every other field stays strict.
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    return value


def _tz_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("deadline_at must be timezone-aware")
    return value


RunState = Literal[
    "queued",
    "preparing",
    "sending",
    "running",
    "completed",
    "failed",
    "cancelled",
    # sessionStart exited 78: the run was deliberately not started (no engine call).
    "skipped",
    "unknown",
]


class StartRequest(_WireModel):
    """One admitted event, ready for its single OpenCode prompt send.

    `deadline_at` is the original UTC deadline stamped once by Admission (contract §3) —
    the mini-harness enforces it independently and never renews it on retry/restart.
    """

    invocation_id: str = Field(min_length=1, alias="invocationId")
    channel: str = Field(min_length=1)
    workspace_ref: str = Field(min_length=1, alias="workspaceRef")
    session_ref: str = Field(min_length=1, alias="sessionRef")
    # Routed keys, for scripts only (ACH_WORKSPACE_KEY / ACH_SESSION_KEY); refs stay the identity.
    workspace_key: str = Field(min_length=1, alias="workspaceKey")
    session_key: str = Field(min_length=1, alias="sessionKey")
    config_version: str = Field(pattern=r"^[0-9a-f]{64}$", alias="configVersion")
    prompt: str
    deadline_at: datetime = Field(alias="deadlineAt")
    # Root-approved single optional trusted internal field (I7): false removes only the
    # memory facade from this invocation's captured catalog; true/None leave it unchanged.
    # No generic capability state, no global lock.
    memory_available: bool | None = Field(default=None, alias="memoryAvailable")

    _deadline_parse = field_validator("deadline_at", mode="before")(_parse_iso_datetime)
    _deadline_aware = field_validator("deadline_at")(_tz_aware_utc)


class Started(_WireModel):
    invocation_id: str = Field(alias="invocationId")
    session_ref: str = Field(alias="sessionRef")
    run_id: str = Field(alias="runId")
    config_version: str = Field(alias="configVersion")


class RunStatus(_WireModel):
    """Enough to reconstruct permissions after a Harness restart (contract §3)."""

    invocation_id: str = Field(alias="invocationId")
    channel: str
    workspace_ref: str = Field(alias="workspaceRef")
    session_ref: str = Field(alias="sessionRef")
    run_id: str | None = Field(alias="runId")
    config_version: str = Field(alias="configVersion")
    deadline_at: datetime = Field(alias="deadlineAt")
    state: RunState
    error: str | None = Field(default=None)
    result: JsonValue = None

    _deadline_parse = field_validator("deadline_at", mode="before")(_parse_iso_datetime)
    _deadline_aware = field_validator("deadline_at")(_tz_aware_utc)


class CancelRequest(_WireModel):
    invocation_id: str = Field(min_length=1, alias="invocationId")
    reason: str = Field(min_length=1)

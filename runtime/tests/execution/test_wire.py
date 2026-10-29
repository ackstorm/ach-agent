# SPDX-License-Identifier: MIT
"""Port of old-ach-agent tests/execution/test_wire.py — strict/frozen/finite-JSON contracts,
adapted to the new StartRequest/Started/RunStatus shapes (contract §§3-4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from ach_runtime.execution.wire import CancelRequest, RunStatus, Started, StartRequest

CONFIG_VERSION = "a" * 64


def _deadline() -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=60)


def _start_request(**overrides: object) -> StartRequest:
    fields: dict[str, object] = dict(
        workspaceKey="wk", sessionKey="sk",
        invocationId="inv-1",
        channel="webhook",
        workspaceRef="ws" * 16,
        sessionRef="se" * 16,
        configVersion=CONFIG_VERSION,
        prompt="do the thing",
        deadlineAt=_deadline(),
    )
    fields.update(overrides)
    return StartRequest(**fields)


def test_start_request_round_trips() -> None:
    req = _start_request()
    assert req.invocation_id == "inv-1"
    assert req.deadline_at.tzinfo is not None


def test_start_request_is_frozen() -> None:
    req = _start_request()
    with pytest.raises(ValidationError):
        req.prompt = "changed"  # type: ignore[misc]


def test_start_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _start_request(extraField="nope")


def test_start_memory_available_is_optional_strict_frozen_and_restorable() -> None:
    """I7: Root's single optional trusted field -- default/absent is None, strict bool
    (never a coerced string), frozen like every other field, and an old archived request
    with no `memoryAvailable` key at all still parses as None. Equality (the existing
    duplicate-Start check) includes it like any other field -- a changed value is not the
    same request, an identical one is."""
    assert _start_request().memory_available is None

    with pytest.raises(ValidationError):
        _start_request(memoryAvailable="true")
    with pytest.raises(ValidationError):
        _start_request(memoryAvailable="false")

    true_req = _start_request(memoryAvailable=True)
    assert true_req.memory_available is True
    with pytest.raises(ValidationError):
        true_req.memory_available = False  # type: ignore[misc]

    archived = {
        "invocationId": "inv-archived", "channel": "webhook", "workspaceRef": "ws" * 16,
        "workspaceKey": "wk", "sessionKey": "sk", "sessionRef": "se" * 16, "configVersion": CONFIG_VERSION, "prompt": "do the thing",
        "deadlineAt": _deadline().isoformat(),
    }
    restored = StartRequest.model_validate(archived)
    assert restored.memory_available is None

    shared_deadline = _deadline()
    duplicate = _start_request(invocationId="inv-dup", memoryAvailable=True, deadlineAt=shared_deadline)
    same = _start_request(invocationId="inv-dup", memoryAvailable=True, deadlineAt=shared_deadline)
    different = _start_request(invocationId="inv-dup", memoryAvailable=False, deadlineAt=shared_deadline)
    assert duplicate == same
    assert duplicate != different


def test_start_request_rejects_naive_deadline() -> None:
    with pytest.raises(ValidationError):
        _start_request(deadlineAt=datetime.now())


def test_start_request_rejects_bad_config_version() -> None:
    with pytest.raises(ValidationError):
        _start_request(configVersion="not-a-hash")


def test_start_request_rejects_construction_by_field_name() -> None:
    # This repo's wire convention (config/public.py) is alias-only construction —
    # the Python field name must not also work, or callers drift between the two.
    with pytest.raises(ValidationError):
        StartRequest(
            invocation_id="inv-1",
            channel="webhook",
            workspace_ref="ws" * 16,
            session_ref="se" * 16,
            config_version=CONFIG_VERSION,
            prompt="do the thing",
            deadline_at=_deadline(),
        )


def test_started_round_trips() -> None:
    started = Started(
        invocationId="inv-1", sessionRef="se" * 16, runId="run-1", configVersion=CONFIG_VERSION
    )
    assert started.run_id == "run-1"


def test_run_status_carries_channel_and_deadline() -> None:
    deadline = _deadline()
    status = RunStatus(
        invocationId="inv-1",
        channel="webhook",
        workspaceRef="ws" * 16,
        sessionRef="se" * 16,
        runId="run-1",
        configVersion=CONFIG_VERSION,
        deadlineAt=deadline,
        state="running",
    )
    assert status.channel == "webhook"
    assert status.deadline_at == deadline


def test_run_status_rejects_naive_deadline() -> None:
    with pytest.raises(ValidationError):
        RunStatus(
            invocationId="inv-1",
            channel="webhook",
            workspaceRef="ws" * 16,
            sessionRef="se" * 16,
            runId=None,
            configVersion=CONFIG_VERSION,
            deadlineAt=datetime.now(),
            state="queued",
        )


def test_cancel_request_requires_reason() -> None:
    with pytest.raises(ValidationError):
        CancelRequest(invocationId="inv-1", reason="")


def test_run_status_accepts_skipped_state() -> None:
    status = RunStatus(
        invocationId="inv-1",
        channel="tick",
        workspaceRef="ws" * 16,
        sessionRef="se" * 16,
        runId="run-1",
        configVersion=CONFIG_VERSION,
        deadlineAt=datetime.now(timezone.utc),
        state="skipped",
    )
    assert status.state == "skipped"

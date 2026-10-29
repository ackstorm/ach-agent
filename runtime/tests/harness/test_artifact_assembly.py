# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from ach_runtime.config.schema import AgentIdentity, ArtifactsBlock
from ach_runtime.facades.artifacts import ArtifactOperations
from ach_runtime.harness.control import Control
from ach_runtime.harness.admission import Admission, RecoveredRun
from ach_runtime.harness.tokens import WorkspaceAuthenticationFailed, facade_token
from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceRef

KEY = b"assembly-test-key"
AGENT_UID = "11111111-1111-1111-1111-111111111111"
WORKSPACE_REF = "a" * 64
SESSION_A = "b" * 64
SESSION_B = "c" * 64
INV_A = "inv-a"
INV_B = "inv-b"
INV_RECOVERED = "inv-recovered"
WORKSPACE_NAME = "ach-workspace"


class _Manager:
    def __init__(self, endpoint: WorkspaceEndpoint) -> None:
        self._active = {WORKSPACE_REF: endpoint}

    def active_endpoint(self, key: str):
        return self._active.get(key)

    def active_endpoints(self):
        return dict(self._active)

    def is_lost(self, instance_id: str) -> bool:
        return False


def _endpoint(instance_id: str = "pod-live") -> WorkspaceEndpoint:
    return WorkspaceEndpoint(
        WorkspaceRef("agents", AGENT_UID, WORKSPACE_REF),
        WORKSPACE_NAME,
        instance_id,
        "http://mini",
    )


def _prepared(invocation_id: str, session_ref: str, *, endpoint: WorkspaceEndpoint | None = None,
              workspace_ref: str = WORKSPACE_REF, deadline_at: datetime | None = None):
    request = SimpleNamespace(
        invocation_id=invocation_id,
        workspace_ref=workspace_ref,
        session_ref=session_ref,
        deadline_at=deadline_at or datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    return endpoint or _endpoint(), request


def _control() -> Control:
    endpoint = _endpoint()
    config = SimpleNamespace(
        agent=AgentIdentity(name="agent", namespace="agents", uid=AGENT_UID),
        artifacts=ArtifactsBlock(enabled=True, maxArtifactBytes=1024, retentionDays=30),
    )
    control = Control.__new__(Control)
    control.config = config
    control.key = KEY
    control.storage = object()
    control.manager = _Manager(endpoint)
    control._hook_endpoints = {}
    async def runner(_event, _ticket):
        return None

    control.admission = Admission(
        namespace="agents",
        agent_uid=AGENT_UID,
        max_concurrent_invocations=2,
        max_queued_total=8,
        idempotency_window_seconds=30,
        max_invocation_seconds=120,
        workspace_max_concurrent_sessions=2,
        channel_concurrency=None,
        clock=lambda: datetime.now(timezone.utc),
        runner=runner,
    )
    control._prepared = {
        INV_A: _prepared(INV_A, SESSION_A),
        INV_B: _prepared(INV_B, SESSION_B),
    }
    return control


@pytest.mark.asyncio
async def test_artifact_context_prepared_and_recovered() -> None:
    assert hasattr(Control, "artifact_operations")
    control = _control()
    assert await control._owned_live(WORKSPACE_NAME)

    first = await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), INV_A)
    second = await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), INV_B)
    assert isinstance(first, ArtifactOperations)
    assert first._owner.workspace_ref == WORKSPACE_REF
    assert first._producer.session_ref == SESSION_A
    assert first._producer.invocation_id == INV_A
    assert second._producer.session_ref == SESSION_B
    assert second._producer.invocation_id == INV_B

    completed = asyncio.get_running_loop().create_future()
    completed.set_result(None)
    recovered = RecoveredRun(
        invocation_id=INV_RECOVERED,
        channel="webhook",
        workspace_ref=WORKSPACE_REF,
        session_ref="d" * 64,
        config_version="f" * 64,
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=2),
        instance_id="pod-live",
        run_id="run-recovered",
        started=asyncio.get_running_loop().create_future(),
        completed=completed,
    )
    control._prepared.clear()
    control.admission._recovered[INV_RECOVERED] = recovered
    recovered_ops = await control.artifact_operations(
        facade_token(KEY, WORKSPACE_NAME), INV_RECOVERED
    )
    assert recovered_ops._producer.session_ref == "d" * 64
    assert recovered_ops._producer.invocation_id == INV_RECOVERED

    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations("forged.token", INV_A)
    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), "unknown")
    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations(facade_token(KEY, "foreign-workspace"), INV_A)

    control._prepared[INV_A] = _prepared(
        INV_A,
        SESSION_A,
        workspace_ref="e" * 64,
    )
    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), INV_A)
    control._prepared[INV_A] = _prepared(
        INV_A,
        SESSION_A,
        deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), INV_A)

    control.admission._released.add(INV_RECOVERED)
    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), INV_RECOVERED)

    control.admission._released.discard(INV_RECOVERED)
    control.admission._recovered[INV_RECOVERED] = RecoveredRun(
        invocation_id=INV_RECOVERED,
        channel="webhook",
        workspace_ref=WORKSPACE_REF,
        session_ref="d" * 64,
        config_version="f" * 64,
        deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        instance_id="pod-live",
        run_id="run-recovered",
        started=asyncio.get_running_loop().create_future(),
        completed=asyncio.get_running_loop().create_future(),
    )
    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), INV_RECOVERED)

    control._prepared[INV_A] = _prepared(
        INV_A,
        SESSION_A,
        endpoint=_endpoint("pod-stale"),
    )
    with pytest.raises(WorkspaceAuthenticationFailed):
        await control.artifact_operations(facade_token(KEY, WORKSPACE_NAME), INV_A)

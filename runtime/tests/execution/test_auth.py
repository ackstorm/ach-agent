# SPDX-License-Identifier: MIT
"""RED-first: fixed-workspace/instance-ID bearer auth wrapping the mini-harness execution app
(owned slice of Task 2 -- workspace-auth-dispatch). Uses a local counted fake service so
no Task 1 (manager/template) import is required."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from ach_runtime.execution.auth import create_authenticated_app
from ach_runtime.execution.wire import RunStatus, Started
from ach_runtime.harness.tokens import engine_bearer, engine_verify_key

KEY = b"2" * 64
VERIFY_KEY = engine_verify_key(KEY)
WORKSPACE = "ws-alpha"
INSTANCE_ID = "pod-abc-123"


@dataclass
class _CountingService:
    start_calls: int = 0
    status_calls: int = 0
    runs_calls: int = 0
    cancel_calls: int = 0

    async def start(self, request):
        self.start_calls += 1
        return Started(
            invocationId=request.invocation_id,
            sessionRef=request.session_ref,
            runId="run-1",
            configVersion=request.config_version,
        )

    async def status(self, invocation_id):
        self.status_calls += 1
        return RunStatus(
            invocationId=invocation_id,
            channel="webhook",
            workspaceRef="ws" * 16,
            sessionRef="se" * 16,
            runId="run-1",
            configVersion="0" * 64,
            deadlineAt=datetime.now(timezone.utc) + timedelta(seconds=60),
            state="running",
        )

    async def list_runs(self):
        self.runs_calls += 1
        return []

    async def cancel(self, invocation_id, reason):
        self.cancel_calls += 1


def _client(service: _CountingService) -> TestClient:
    app = create_authenticated_app(
        service, verify_key=VERIFY_KEY, workspace_name=WORKSPACE, instance_id=INSTANCE_ID
    )
    return TestClient(app)


def _bearer(claim: str = WORKSPACE) -> str:
    return engine_bearer(KEY, claim)


def _tampered_bearer() -> str:
    claim, _, sig = _bearer().rpartition(".")
    tampered_sig = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    return f"{claim}.{tampered_sig}"


def _start_payload() -> dict[str, object]:
    return {
        "invocationId": "inv-1",
        "channel": "webhook",
        "workspaceRef": "ws" * 16,
        "workspaceKey": "wk", "sessionKey": "sk", "sessionRef": "se" * 16,
        "configVersion": "0" * 64,
        "prompt": "do the thing",
        "deadlineAt": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    }


def test_healthz_stays_public() -> None:
    service = _CountingService()
    client = _client(service)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert service.start_calls == 0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer not-a-real-bearer", "X-ACH-Instance-Id": INSTANCE_ID},
        {"Authorization": f"Bearer {_bearer('ws-other')}", "X-ACH-Instance-Id": INSTANCE_ID},
        {"Authorization": f"Bearer {_tampered_bearer()}", "X-ACH-Instance-Id": INSTANCE_ID},
    ],
)
def test_private_routes_reject_bad_bearer_zero_service_calls(headers: dict[str, str]) -> None:
    service = _CountingService()
    client = _client(service)

    assert client.post("/start", json=_start_payload(), headers=headers).status_code == 401
    assert client.get("/status/inv-1", headers=headers).status_code == 401
    assert client.get("/runs", headers=headers).status_code == 401
    assert (
        client.post(
            "/cancel", json={"invocationId": "inv-1", "reason": "x"}, headers=headers
        ).status_code
        == 401
    )
    assert (
        service.start_calls,
        service.status_calls,
        service.runs_calls,
        service.cancel_calls,
    ) == (0, 0, 0, 0)


def test_private_routes_reject_missing_or_stale_instance_id() -> None:
    service = _CountingService()
    client = _client(service)
    good_bearer = {"Authorization": f"Bearer {_bearer()}"}

    for headers in (good_bearer, {**good_bearer, "X-ACH-Instance-Id": "stale-uid"}):
        assert client.post("/start", json=_start_payload(), headers=headers).status_code == 401
        assert client.get("/status/inv-1", headers=headers).status_code == 401
        assert client.get("/runs", headers=headers).status_code == 401
        assert (
            client.post(
                "/cancel", json={"invocationId": "inv-1", "reason": "x"}, headers=headers
            ).status_code
            == 401
        )

    assert (
        service.start_calls,
        service.status_calls,
        service.runs_calls,
        service.cancel_calls,
    ) == (0, 0, 0, 0)


def test_401_and_probes_carry_instance_id() -> None:
    client = _client(_CountingService())
    wrong = {"Authorization": f"Bearer {_bearer()}", "X-ACH-Instance-Id": "other"}
    for response in (
        client.get("/runs", headers=wrong),
        client.get("/runs"),
        client.get("/healthz"),
        client.get("/readyz"),
    ):
        assert response.headers["x-ach-instance-id"] == INSTANCE_ID
    assert client.get("/runs", headers=wrong).status_code == 401


def test_valid_identity_delegates_and_echoes_instance_id() -> None:
    service = _CountingService()
    client = _client(service)
    headers = {"Authorization": f"Bearer {_bearer()}", "X-ACH-Instance-Id": INSTANCE_ID}

    response = client.get("/runs", headers=headers)
    assert response.status_code == 200
    assert response.headers["x-ach-instance-id"] == INSTANCE_ID
    assert service.runs_calls == 1

    started = client.post("/start", json=_start_payload(), headers=headers)
    assert started.status_code == 202
    assert started.headers["x-ach-instance-id"] == INSTANCE_ID
    assert service.start_calls == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"verify_key": "", "workspace_name": WORKSPACE, "instance_id": INSTANCE_ID},
        {"verify_key": "not-base64url-ed25519", "workspace_name": WORKSPACE, "instance_id": INSTANCE_ID},
        {"verify_key": VERIFY_KEY, "workspace_name": "", "instance_id": INSTANCE_ID},
        {"verify_key": VERIFY_KEY, "workspace_name": WORKSPACE, "instance_id": ""},
    ],
)
def test_construction_rejects_empty_or_malformed_identity(kwargs: dict[str, str]) -> None:
    service = _CountingService()
    with pytest.raises(ValueError):
        create_authenticated_app(service, **kwargs)


def test_construction_rejects_contaminated_valid_key() -> None:
    service = _CountingService()
    contaminated = engine_verify_key(b"0" * 64) + "!!!!"
    with pytest.raises(ValueError):
        create_authenticated_app(
            service, verify_key=contaminated, workspace_name=WORKSPACE, instance_id=INSTANCE_ID
        )


def test_construction_accepts_known_legacy_verify_key() -> None:
    service = _CountingService()
    app = create_authenticated_app(
        service,
        verify_key=engine_verify_key(b"0" * 64),
        workspace_name=WORKSPACE,
        instance_id=INSTANCE_ID,
    )
    assert app is not None


def test_non_utf8_auth_headers_are_401_not_500() -> None:
    service = _CountingService()
    client = _client(service)

    headers = {"Authorization": b"Bearer \xff\xfe", "X-ACH-Instance-Id": b"\xff"}
    assert client.get("/runs", headers=headers).status_code == 401
    assert service.runs_calls == 0

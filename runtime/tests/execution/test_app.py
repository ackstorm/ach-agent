# SPDX-License-Identifier: MIT
"""Thin HTTP layer over ExecutionService — validation/error-mapping only; the actual
registry/dedup/deadline semantics are proved in test_service.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import io
import os
import tempfile
import tarfile
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
import ach_runtime

from ach_runtime.config.schema import HookBlock
from ach_runtime.execution.app import create_app
from ach_runtime.execution.auth import create_authenticated_app
from ach_runtime.execution.service import ExecutionService
from ach_runtime.execution.sessions import SessionRegistry
from ach_runtime.harness.tokens import engine_bearer, engine_verify_key


@dataclass
class FakeEngine:
    start_calls: list[str] = field(default_factory=list)

    async def start(self, invocation, *, home, scratch, password=None):
        self.start_calls.append(invocation.invocation_id)
        return object()

    async def status(self, invocation_id):
        return "running"

    async def cancel(self, invocation_id, reason, *, _halt_budget=None):
        return None


@dataclass
class _Invocation:
    invocation_id: str


def _client(tmp_path: Path, public_config_fixture) -> TestClient:
    engine = FakeEngine()
    service = ExecutionService(
        engine=engine,
        sessions=SessionRegistry(tmp_path),
        workspace_dir=tmp_path / "workspace",
        build_invocation=lambda request, public, workspace_dir: _Invocation(request.invocation_id),
    )
    service.configure(public_config_fixture)
    return TestClient(create_app(service))


def _payload(config_version: str) -> dict[str, object]:
    return {
        "invocationId": "inv-1",
        "channel": "webhook",
        "workspaceRef": "ws" * 16,
        "workspaceKey": "wk", "sessionKey": "sk", "sessionRef": "se" * 16,
        "configVersion": config_version,
        "prompt": "do the thing",
        "deadlineAt": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
    }


async def test_configure_accepts_file_and_ach_system_prompts_before_hydration(
    tmp_path: Path, public_config_fixture
) -> None:
    from ach_runtime.config.schema import SystemAch, SystemFile

    engine = FakeEngine()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=tmp_path / "workspace",
    )
    key, workspace, instance_id = b"9" * 64, "ws-alpha", "pod-123"
    app = create_authenticated_app(
        service, verify_key=engine_verify_key(key), workspace_name=workspace, instance_id=instance_id
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mini")
    headers = {"Authorization": f"Bearer {engine_bearer(key, workspace)}", "X-ACH-Instance-Id": instance_id}
    catalog = {
        "providers": {
            public_config_fixture.model.type: {
                "baseURL": public_config_fixture.infrastructure.facade_endpoint,
                "apiKey": "local-proxy",
                "models": {public_config_fixture.model.name: {}},
            }
        },
        "mcp": {},
    }
    for system in (SystemFile(type="file", file="prompts/system.md"),
                   SystemAch(type="ach", ach="review", file="system.md")):
        public = public_config_fixture.model_copy(deep=True)
        public.prompt.system = system
        response = await client.post("/configure", headers=headers, json={
            "public": public.model_dump(by_alias=True, mode="json"), "catalog": catalog,
        })
        assert response.status_code == 200, response.text
        assert response.json() == {"configVersion": public.config_version}
    malformed = {**catalog, "providers": {"unknown": {}}}
    rejected = await client.post("/configure", headers=headers, json={
        "public": public_config_fixture.model_dump(by_alias=True, mode="json"),
        "catalog": malformed,
    })
    assert rejected.status_code == 422
    assert not engine.start_calls
    await client.aclose()


async def test_start_rejects_missing_system_file_before_engine_start(
    tmp_path: Path, public_config_fixture
) -> None:
    from ach_runtime.config.schema import SystemFile

    engine = FakeEngine()
    public = public_config_fixture.model_copy(deep=True)
    public.prompt.system = SystemFile(type="file", file="prompts/missing.md")
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=tmp_path / "workspace",
    )
    service.configure(public)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="http://mini"
    )
    try:
        response = await client.post("/start", json=_payload(public.config_version))
        assert response.status_code == 502
        assert "not found under .ach-state" in response.json()["message"]
        assert not engine.start_calls
    finally:
        await client.aclose()
        await service.close()


def test_healthz_ok_without_a_native_process(tmp_path: Path, public_config_fixture) -> None:
    client = _client(tmp_path, public_config_fixture)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "release": ach_runtime.__version__}


async def test_workspace_export_uses_tempdir_outside_source(tmp_path: Path, monkeypatch) -> None:
    import ach_runtime.execution.snapshots as snapshots

    cold = tmp_path / "cold"
    workspace = cold / "workspace"
    temporary = tmp_path / "archive-temp"
    workspace.mkdir(parents=True)
    temporary.mkdir()
    sentinel = b"workspace sentinel\n"
    result = b"\x00\x01workspace\xff"
    (workspace / "sentinel").write_bytes(sentinel)
    (workspace / "sub").mkdir()
    (workspace / "sub" / "result.bin").write_bytes(result)
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", None)
    assert os.access(workspace, os.W_OK)
    assert os.access(temporary, os.W_OK)

    service = ExecutionService(
        engine=FakeEngine(), sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=workspace,
    )
    archive = None
    try:
        archive = await snapshots.export_workspace(service)
        assert archive.parent == temporary
        assert archive.exists()
        assert not archive.is_relative_to(workspace.resolve())
        assert service._workspace_frozen is False
        with tarfile.open(archive, mode="r:gz") as tar:
            assert set(tar.getnames()) == {".", "./sentinel", "./sub", "./sub/result.bin"}
            assert tar.extractfile("./sentinel").read() == sentinel
            assert tar.extractfile("./sub/result.bin").read() == result
        assert (workspace / "sentinel").read_bytes() == sentinel
        assert (workspace / "sub" / "result.bin").read_bytes() == result
        assert not list(workspace.rglob(".ach-archive-*"))
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
        await service.close()
    assert list(temporary.iterdir()) == []


@pytest.mark.parametrize("failure", [pytest.param("byte-cap", id="byte-cap"),
                                     pytest.param("missing-source", id="missing-source")])
async def test_workspace_export_pack_failure_cleans_temp_and_thaws(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    import ach_runtime.execution.snapshots as snapshots
    from ach_runtime.storage.archive import ArchiveTooLarge

    workspace = tmp_path / "cold" / "workspace"
    temporary = tmp_path / "archive-temp"
    workspace.mkdir(parents=True)
    temporary.mkdir()
    sentinel = b"workspace sentinel\n"
    if failure == "byte-cap":
        (workspace / "sentinel").write_bytes(sentinel)
        monkeypatch.setattr(snapshots, "MAX_SNAPSHOT_BYTES", 1)
    else:
        workspace.rmdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", None)
    service = ExecutionService(
        engine=FakeEngine(), sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=workspace,
    )
    try:
        if failure == "byte-cap":
            with pytest.raises(ArchiveTooLarge, match="archive exceeds 1 bytes during creation"):
                await snapshots.export_workspace(service)
            assert (workspace / "sentinel").read_bytes() == sentinel
        else:
            with pytest.raises(FileNotFoundError):
                await snapshots.export_workspace(service)
            assert not workspace.exists()
        assert service._workspace_frozen is False
        assert not list(temporary.glob(".ach-archive-*"))
        assert not list(workspace.parent.glob(".ach-archive-*"))
    finally:
        await service.close()
    assert list(temporary.iterdir()) == []


async def test_workspace_archive_response_unlinks_only_after_body(tmp_path: Path, monkeypatch) -> None:
    from fastapi.responses import FileResponse

    workspace = tmp_path / "cold" / "workspace"
    temporary = tmp_path / "archive-temp"
    workspace.mkdir(parents=True)
    temporary.mkdir()
    sentinel = b"workspace sentinel\n"
    (workspace / "sentinel").write_bytes(sentinel)
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(tempfile, "tempdir", None)
    service = ExecutionService(
        engine=FakeEngine(), sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=workspace,
    )
    archive = None
    try:
        route = next(route for route in create_app(service).routes
                     if getattr(route, "path", None) == "/workspace/archive"
                     and "GET" in getattr(route, "methods", set()))
        response = await route.endpoint()
        assert isinstance(response, FileResponse)
        archive = Path(response.path)
        assert response.status_code == 200
        assert response.media_type == "application/gzip"
        assert response.headers["content-disposition"] == 'attachment; filename="archive.tar.gz"'
        assert archive.parent == temporary
        assert archive.exists()

        messages = []
        body = bytearray()

        async def send(message):
            assert archive.exists(), "archive must remain available through the response body"
            messages.append(message)
            if message["type"] == "http.response.body":
                body.extend(message.get("body", b""))

        async def receive():
            await anyio.sleep_forever()  # the client stays connected while the body streams

        await response({"type": "http", "method": "GET", "headers": [], "extensions": {}},
                       receive, send)
        body_messages = [message for message in messages if message["type"] == "http.response.body"]
        assert body_messages[-1]["more_body"] is False
        with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
            assert set(tar.getnames()) == {".", "./sentinel"}
            assert tar.extractfile("./sentinel").read() == sentinel
        assert not archive.exists()
        assert list(temporary.iterdir()) == []
        assert (workspace / "sentinel").read_bytes() == sentinel
    finally:
        if archive is not None:
            archive.unlink(missing_ok=True)
        await service.close()


async def test_authenticated_public_probes_include_instance_id_without_authentication(
    tmp_path: Path, public_config_fixture
) -> None:
    engine = FakeEngine()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=tmp_path / "workspace",
    )
    instance_id = "pod-public-probes"
    app = create_authenticated_app(
        service, verify_key=engine_verify_key(b"p" * 64),
        workspace_name="workspace", instance_id=instance_id,
    )
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mini")
    try:
        health = await client.get("/healthz")
        ready = await client.get("/readyz")
        assert health.status_code == 200
        assert ready.status_code == 503
        assert health.headers["x-ach-instance-id"] == instance_id
        assert ready.headers["x-ach-instance-id"] == instance_id
        assert not engine.start_calls
    finally:
        await client.aclose()
        await service.close()


def test_start_then_status_then_cancel(tmp_path: Path, public_config_fixture) -> None:
    client = _client(tmp_path, public_config_fixture)
    payload = _payload(public_config_fixture.config_version)

    started = client.post("/start", json=payload)
    assert started.status_code == 202
    assert started.json()["invocationId"] == "inv-1"

    status = client.get("/status/inv-1")
    assert status.status_code == 200
    assert status.json()["channel"] == "webhook"

    runs = client.get("/runs")
    assert runs.status_code == 200
    assert len(runs.json()) == 1

    cancel = client.post("/cancel", json={"invocationId": "inv-1", "reason": "operator"})
    assert cancel.status_code == 200


def test_conflicting_start_rejected_with_409(tmp_path: Path, public_config_fixture) -> None:
    client = _client(tmp_path, public_config_fixture)
    payload = _payload(public_config_fixture.config_version)
    client.post("/start", json=payload)

    conflicting = dict(payload, prompt="a different prompt")
    response = client.post("/start", json=conflicting)
    assert response.status_code == 409


def test_status_for_unknown_invocation_is_404(tmp_path: Path, public_config_fixture) -> None:
    client = _client(tmp_path, public_config_fixture)
    response = client.get("/status/does-not-exist")
    assert response.status_code == 404


def test_private_archive_routes_are_uid_bound_and_exact(tmp_path: Path, public_config_fixture) -> None:
    engine = FakeEngine()
    service = ExecutionService(engine=engine, sessions=SessionRegistry(tmp_path),
                               workspace_dir=tmp_path / "workspace", workspace_ref="ws-ref")
    service.configure(public_config_fixture)
    key, workspace, instance_id = b"9" * 64, "ws-alpha", "pod-123"
    client = TestClient(create_authenticated_app(
        service, verify_key=engine_verify_key(key), workspace_name=workspace, instance_id=instance_id
    ))
    paths = {(route.path, method) for route in client.app.routes for method in getattr(route, "methods", set())}
    assert {("/sessions", "GET"), ("/sessions/{session_ref}/info", "GET"),
            ("/sessions/{session_ref}/archive", "GET"), ("/sessions/{session_ref}/archive", "PUT"),
            ("/workspace/archive", "GET"), ("/workspace/archive", "PUT"),
            ("/execution/v1/workspace/handoff", "PUT"),
            ("/execution/v1/sandbox/archive/hydration", "PUT")} <= paths
    assert client.get("/sessions").status_code == 401
    headers = {"Authorization": f"Bearer {engine_bearer(key, workspace)}", "X-ACH-Instance-Id": instance_id}
    missing_uid = {"Authorization": headers["Authorization"]}
    assert client.get("/sessions", headers=missing_uid).status_code == 401
    assert client.get("/sessions/unknown/info", headers=headers).status_code == 404
    assert client.get("/sessions", headers=headers).headers["x-ach-instance-id"] == instance_id


def test_workspace_handoff_uses_configured_destination_and_rejects_escape(
    tmp_path: Path, public_config_fixture
) -> None:
    engine = FakeEngine()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    service = ExecutionService(
        engine=engine, sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=workspace_dir, workspace_ref="ws-ref",
    )
    service.configure(public_config_fixture)
    key, workspace, instance_id = b"9" * 64, "ws-alpha", "pod-123"
    client = TestClient(create_authenticated_app(
        service, verify_key=engine_verify_key(key), workspace_name=workspace, instance_id=instance_id
    ))
    headers = {"Authorization": f"Bearer {engine_bearer(key, workspace)}", "X-ACH-Instance-Id": instance_id}
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        content = b"handoff payload"
        info = tarfile.TarInfo("result.txt")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    body = archive.getvalue()

    root_inode = workspace_dir.stat().st_ino
    marker = workspace_dir / "preserve-me"
    marker.write_text("workspace root contents")
    root = client.put(
        "/execution/v1/workspace/handoff?destination=.", headers=headers, content=body
    )
    assert root.status_code == 422
    assert workspace_dir.stat().st_ino == root_inode
    assert marker.read_text() == "workspace root contents"
    assert list(workspace_dir.glob(".ach-handoff-*")) == []

    default = client.put("/execution/v1/workspace/handoff", headers=headers, content=body)
    assert default.status_code == 200, default.text
    assert (workspace_dir / "handoff" / "result.txt").read_bytes() == b"handoff payload"

    configured = client.put(
        "/execution/v1/workspace/handoff?destination=handoff-v2/context",
        headers=headers, content=body,
    )
    assert configured.status_code == 200, configured.text
    assert (workspace_dir / "handoff-v2/context/result.txt").read_bytes() == b"handoff payload"

    outside = client.put(
        "/execution/v1/workspace/handoff?destination=../outside",
        headers=headers, content=body,
    )
    assert outside.status_code == 502
    assert not (tmp_path / "outside").exists()


def test_workspace_handoff_suspends_idle_historical_sessions(tmp_path: Path, public_config_fixture) -> None:
    class IdleEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self._sessions = {"session-ref": SimpleNamespace(alive=True, sid="retained-native-sid")}
            self.suspend_calls: list[str] = []
            self.suspend_result = True

        async def suspend(self, session_ref: str) -> bool:
            self.suspend_calls.append(session_ref)
            if self.suspend_result:
                self._sessions[session_ref].alive = False
            return self.suspend_result

    engine = IdleEngine()
    sessions = SessionRegistry(tmp_path / "sessions")
    state = sessions.get_or_create("session-ref")
    state.native_session_id = "retained-native-sid"
    preserved_home_file = state.home / "preserve-me"
    preserved_home_file.write_text("home stays intact")
    sessions.get_or_create("older-session-ref")
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    service = ExecutionService(
        engine=engine, sessions=sessions, workspace_dir=workspace_dir, workspace_ref="ws-ref"
    )
    service.configure(public_config_fixture)
    service._invocations["prior-terminal-run"] = SimpleNamespace(
        terminal=True, state="completed", reservation=None,
        cancel_task=None, owner_stop_task=None, engine_cancel_task=None,
    )
    key, workspace, instance_id = b"9" * 64, "ws-alpha", "pod-123"
    client = TestClient(create_authenticated_app(
        service, verify_key=engine_verify_key(key), workspace_name=workspace, instance_id=instance_id
    ))
    headers = {"Authorization": f"Bearer {engine_bearer(key, workspace)}", "X-ACH-Instance-Id": instance_id}

    def archive_bytes(content: bytes) -> bytes:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tar:
            info = tarfile.TarInfo("result.txt")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        return archive.getvalue()

    first = client.put("/execution/v1/workspace/handoff", headers=headers,
                       content=archive_bytes(b"event one"))
    assert first.status_code == 200, first.text
    second = client.put("/execution/v1/workspace/handoff", headers=headers,
                        content=archive_bytes(b"event two"))
    assert second.status_code == 200, second.text
    assert engine.suspend_calls == ["session-ref"]
    assert state.native_session_id == "retained-native-sid"
    assert engine._sessions["session-ref"].sid == "retained-native-sid"
    assert preserved_home_file.read_text() == "home stays intact"
    assert not state.started
    assert not engine.start_calls
    assert (workspace_dir / "handoff" / "result.txt").read_bytes() == b"event two"

    engine._sessions["session-ref"].alive = True
    engine.suspend_result = False
    (workspace_dir / "handoff" / "result.txt").write_bytes(b"preserve on suspend failure")
    failed = client.put("/execution/v1/workspace/handoff", headers=headers,
                        content=archive_bytes(b"must not publish"))
    assert failed.status_code == 409
    assert (workspace_dir / "handoff" / "result.txt").read_bytes() == b"preserve on suspend failure"


async def test_hydration_archive_installs_before_start(tmp_path: Path, public_config_fixture) -> None:
    engine = FakeEngine()
    sessions = SessionRegistry(tmp_path / "sessions")
    service = ExecutionService(engine=engine, sessions=sessions, workspace_dir=tmp_path / "workspace",
                               workspace_ref="ws-ref")
    service.configure(public_config_fixture)
    assert sessions.get("session-ref") is None
    public = service._workspace_dir / ".ach-state"
    stale = public / "skills" / "stale-skill"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("stale")

    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as tar:
        for name, content in (("skills/new-skill/SKILL.md", b"new"),
                              ("prompts/system.md", b"system prompt")):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    key, workspace, instance_id = b"9" * 64, "ws-alpha", "pod-123"
    client = TestClient(create_authenticated_app(
        service, verify_key=engine_verify_key(key), workspace_name=workspace, instance_id=instance_id
    ))
    headers = {"Authorization": f"Bearer {engine_bearer(key, workspace)}", "X-ACH-Instance-Id": instance_id}
    response = client.put("/execution/v1/sandbox/archive/hydration?session_ref=session-ref",
                          headers=headers, content=payload.getvalue())
    assert response.status_code == 200, response.text
    state = sessions.get("session-ref")
    assert state is not None and not state.started
    assert not sessions.lock("session-ref").locked()
    assert not engine.start_calls
    assert (public / "skills/new-skill/SKILL.md").read_text() == "new"
    assert not stale.exists()
    assert (public / "prompts/system.md").read_text() == "system prompt"
    home_context = state.home / ".ach-state"
    assert home_context.is_dir() and not home_context.is_symlink()
    assert (home_context / "skills/new-skill/SKILL.md").read_text() == "new"
    assert (home_context / "prompts/system.md").read_text() == "system prompt"
    skills_link = state.home / ".config" / "opencode" / "skills"
    assert skills_link.is_symlink()
    assert not Path(skills_link.readlink()).is_absolute()
    assert skills_link.resolve() == (home_context / "skills").resolve()


def _hooked_client(tmp_path: Path, public_config_fixture, **hooks: HookBlock) -> TestClient:
    service = ExecutionService(
        engine=FakeEngine(),
        sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=tmp_path / "workspace",
        build_invocation=lambda request, public, workspace_dir: _Invocation(request.invocation_id),
    )
    (tmp_path / "workspace").mkdir()
    public = public_config_fixture.model_copy(
        update={"hooks": public_config_fixture.hooks.model_copy(update=hooks)}
    )
    service.configure(public, catalog={"mcp": {"srv": {"url": "http://facade/mcp/srv", "headers": {}}}})
    return TestClient(create_app(service))


def test_workspace_start_hook_runs_in_the_workspace(tmp_path: Path, public_config_fixture) -> None:
    script = 'pwd > out.txt; echo "$ACH_WORKSPACE_RESTORED $ACH_MCP_URLS" >> out.txt'
    client = _hooked_client(tmp_path, public_config_fixture, workspace_start=HookBlock(script=script))
    response = client.post("/workspace/hooks/start", json={"restored": True})
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "ok"}
    lines = (tmp_path / "workspace" / "out.txt").read_text().splitlines()
    assert lines == [str(tmp_path / "workspace"), 'true {"srv": "http://facade/mcp/srv"}']


def test_workspace_stop_hook_runs(tmp_path: Path, public_config_fixture) -> None:
    client = _hooked_client(
        tmp_path, public_config_fixture, workspace_stop=HookBlock(script="touch stopped")
    )
    response = client.post("/workspace/hooks/stop")
    assert response.status_code == 200, response.text
    assert (tmp_path / "workspace" / "stopped").is_file()


def test_workspace_hook_not_configured_is_skipped(tmp_path: Path, public_config_fixture) -> None:
    client = _hooked_client(tmp_path, public_config_fixture)
    assert client.post("/workspace/hooks/start", json={"restored": False}).json() == {"status": "skipped"}
    assert client.post("/workspace/hooks/stop").json() == {"status": "skipped"}


def test_workspace_hook_failure_is_502(tmp_path: Path, public_config_fixture) -> None:
    client = _hooked_client(
        tmp_path, public_config_fixture, workspace_stop=HookBlock(script="echo nope >&2; exit 4")
    )
    response = client.post("/workspace/hooks/stop")
    assert response.status_code == 502
    assert "exited 4" in response.json()["message"]


def test_workspace_hook_unknown_phase_is_404(tmp_path: Path, public_config_fixture) -> None:
    client = _hooked_client(tmp_path, public_config_fixture)
    assert client.post("/workspace/hooks/restart").status_code == 404


async def test_workspace_archive_policy_rejection_is_not_retryable(tmp_path: Path, monkeypatch) -> None:
    """An export the restore policy would refuse is deterministic: the close path must not
    burn its retry window re-packing the same tree."""
    import json

    import httpx

    from ach_runtime.harness.lifecycle import _retryable_archive_error

    workspace = tmp_path / "cold" / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "escape").symlink_to("/etc/passwd")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    service = ExecutionService(
        engine=FakeEngine(), sessions=SessionRegistry(tmp_path / "sessions"),
        workspace_dir=workspace,
    )
    try:
        route = next(route for route in create_app(service).routes
                     if getattr(route, "path", None) == "/workspace/archive"
                     and "GET" in getattr(route, "methods", set()))
        response = await route.endpoint()
    finally:
        await service.close()

    assert response.status_code == 422
    assert json.loads(response.body)["type"] == "ArchiveRejected"
    request = httpx.Request("GET", "http://mini/workspace/archive")
    error = httpx.HTTPStatusError(
        "rejected", request=request, response=httpx.Response(422, request=request)
    )
    assert _retryable_archive_error(error) is False

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.config.schema import RuntimeConfig, parse_config
from ach_runtime.execution.boot import build_execution_app
from ach_runtime.execution.wire import Started
from ach_runtime.harness.app import create_app
from ach_runtime.harness.control import Control, assemble, create_facade_app
from ach_runtime.harness.tokens import host_token
from test_s3_integration import client
from fake_facades import FakeFacades, fake_facades

try:
    from fake_host import FakeHost
except ModuleNotFoundError:
    # The bounded host collection helper supplies existing tests/engine, tests/facades,
    # and tests/storage import roots; this conventional package import covers its deliberate
    # omission of tests/harness without adding a loader or modifying the helper.
    from tests.harness.fake_host import FakeHost


class _SwitchApp:
    """ASGI indirection so a restarted Control can take over the facade listener."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        await self.app(scope, receive, send)


@dataclass
class E2EStack:
    control: Control
    ingress: httpx.AsyncClient
    host: FakeHost
    minis: dict[str, FastAPI]  # instance ID -> execution app
    upstream: FakeFacades
    requests: list[httpx.Request] = field(default_factory=list)
    started: list[Started] = field(default_factory=list)
    start_payloads: list[dict[str, Any]] = field(default_factory=list)
    start_raw_bodies: list[bytes] = field(default_factory=list)
    start_instance_ids: list[str] = field(default_factory=list)
    configure_receipts: list[tuple[str, str]] = field(default_factory=list)
    configure_responses: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)
    start_responses: list[tuple[str, bytes, dict[str, str]]] = field(default_factory=list)
    archive_restores: list[tuple[str, str]] = field(default_factory=list)
    workspace_restore_calls: list[str] = field(default_factory=list)
    status_observations: list[Any] = field(default_factory=list)
    owned_inventory_ready: bool = False
    ready_after_discovery: bool = False
    repo_heads: dict[tuple[str, int], str] = field(default_factory=dict)
    repo_root: Path | None = None
    archives: list[bytes] = field(default_factory=list)
    handoff_suspensions: list[dict[str, bool]] = field(default_factory=list)
    handoff_import_observations: list[dict[str, Any]] = field(default_factory=list)
    handoff_entry_processes: dict[str, Any] = field(default_factory=dict)
    handoff_entry_head: str | None = None
    handoff_import_original: Any = None
    handoff_import_wrapper: Any = None
    faults: dict[str, Any] = field(default_factory=dict)
    healthz_failures: int = 0
    units: Any = None
    closed_instances: list[str] = field(default_factory=list)
    host_task: asyncio.Task[None] | None = None
    facade_server: uvicorn.Server | None = None
    facade_task: asyncio.Task[None] | None = None
    _workspace_roots: dict[str, Path] = field(default_factory=dict)
    restart_control: Callable[..., Awaitable[None]] | None = None
    retired: list[Control] = field(default_factory=list)

    async def post_event(
        self,
        *,
        project_id: int = 1,
        project_path: str = "team/service",
        iid: int = 4,
        event_type: str = "merge_request",
        text: str | None = None,
        event_uuid: str | None = None,
        wait_started: bool = True,
    ) -> httpx.Response:
        event_uuid = event_uuid or str(uuid.uuid4())
        if event_type == "note":
            payload: dict[str, Any] = {
                "object_kind": "note",
                "project": {"id": project_id, "path_with_namespace": project_path},
                "object_attributes": {"iid": int(event_uuid.replace("-", "")[:8], 16), "noteable_type": "MergeRequest", "note": text or "Please recheck the latest commit."},
                "merge_request": {"iid": iid, "last_commit": {"id": self.repo_heads[(project_path, iid)]}},
                "user": {"username": "juancarlos.moreno"},
            }
        else:
            payload = {
                "object_kind": "merge_request",
                "project": {"id": project_id, "path_with_namespace": project_path},
                "object_attributes": {"iid": iid, "state": "opened", "description": text or f"e2e-event-{event_uuid}", "last_commit": {"id": self.repo_heads[(project_path, iid)]}},
                "user": {"username": "juancarlos.moreno"},
            }
        response = await self.ingress.post(
            "/channels/gitlab-mr-review/events",
            json=payload,
            headers={"X-Gitlab-Token": "e2e-webhook-secret", "X-Gitlab-Event-UUID": event_uuid},
        )
        # These end-to-end callers inspect native state immediately afterwards.
        # Synchronize the fixture separately: HTTP ACK itself is admission-only.
        if response.status_code == 202 and wait_started:
            ticket = next(
                ticket for ticket in self.control.admission._tickets.values()
                if ticket.routed_event.event_id == event_uuid
            )
            done, _ = await asyncio.wait({ticket.started}, timeout=30)
            assert done, "admitted fixture event did not resolve its internal Started outcome"
        return response

    async def captured(self) -> dict[str, Any]:
        return await self.upstream.control({"command": "capture"})

    async def release_model(self) -> None:
        await self.upstream.control({"command": "release"})

    def advance_mr(self, project_path: str, iid: int) -> str:
        if self.repo_root is None:
            raise RuntimeError("fixture repository root is unavailable")
        project_id = 1 if project_path == "team/service" else 2
        work = self.repo_root / f"work-{project_id}"
        (work / "README.md").write_text(f"project {project_id}, MR {iid}, updated\n", encoding="utf-8")
        _git("add", "README.md", cwd=work)
        _git("commit", "-m", f"MR {iid} updated fixture state", cwd=work)
        head = _git("rev-parse", "HEAD", cwd=work)
        bare = self.repo_root / f"{project_path}.git"
        _git("push", "origin", f"HEAD:refs/merge-requests/{iid}/head", cwd=work)
        self.repo_heads[(project_path, iid)] = head
        return head

    def start_instance(self, started: Started) -> str:
        index = next(i for i, payload in enumerate(self.start_payloads) if payload["invocationId"] == started.invocation_id)
        return self.start_instance_ids[index]

    def current_session(self, started: Started):
        instance_id = self.start_instance(started)
        endpoint = next(
            (item for item in self.control.manager.active_endpoints().values() if item.instance_id == instance_id), None
        )
        if endpoint is None:
            raise AssertionError(
                f"Started invocation {started.invocation_id} no longer belongs to current instance {instance_id}"
            )
        app = self.minis[endpoint.instance_id]
        state = app.state.service._sessions.get(started.session_ref)
        assert state is not None
        native = app.state.service._engine._sessions[started.session_ref]
        return endpoint, state, native

    async def status_for(self, started: Started):
        endpoint, _state, _native = self.current_session(started)
        status = await self.control.delivery.status(endpoint, started.invocation_id)
        self.status_observations.append(status)
        return status


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
    )
    return result.stdout.strip()


def _capture_handoff_entry(stack: E2EStack, service: Any, checkout: Path) -> None:
    """Capture only live sessions; a cold import has no checkout yet."""
    stack.handoff_entry_processes = {
        session_ref: native for session_ref, native in service._engine._sessions.items()
        if getattr(native, "alive", False)
    }
    stack.handoff_entry_head = None
    if stack.handoff_entry_processes:
        stack.handoff_entry_head = _git("rev-parse", "HEAD", cwd=checkout)


async def _observe_handoff_import(
    stack: E2EStack,
    service: Any,
    destination: str,
    chunks: AsyncIterator[bytes],
    original_import: Callable[..., Awaitable[Any]],
) -> Any:
    captured = stack.handoff_entry_processes
    if not captured:
        return await original_import(service, destination, chunks)

    current_sessions = service._engine._sessions
    suspended = {
        session_ref: (
            current_sessions.get(session_ref) is native
            and not getattr(native, "alive", True)
            and bool(getattr(native, "clean_exit", False))
        )
        for session_ref, native in captured.items()
    }
    boundary_head = _git("rev-parse", "HEAD", cwd=Path(destination) / "repo")
    stack.handoff_import_observations.append({
        "entry_processes": tuple(captured),
        "suspended": suspended,
        "entry_head": stack.handoff_entry_head,
        "boundary_head": boundary_head,
    })
    assert all(suspended.values()), "all entry native processes must be cleanly suspended before import"
    assert boundary_head == stack.handoff_entry_head, "checkout HEAD must remain old before import"
    return await original_import(service, destination, chunks)


def _fixture_repositories(root: Path) -> dict[tuple[str, int], str]:
    heads: dict[tuple[str, int], str] = {}
    for path, project_id in (("team/service", 1), ("team/other", 2)):
        bare = root / f"{path}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", str(bare))
        work = root / f"work-{project_id}"
        _git("init", str(work))
        _git("config", "user.name", "E2E Fixture", cwd=work)
        _git("config", "user.email", "e2e@example.invalid", cwd=work)
        (work / "README.md").write_text(f"project {project_id}\n", encoding="utf-8")
        _git("add", "README.md", cwd=work)
        _git("commit", "-m", "initial fixture state", cwd=work)
        _git("remote", "add", "origin", f"file://{bare}", cwd=work)
        for iid in (4, 5):
            (work / "README.md").write_text(f"project {project_id}, MR {iid}, original\n", encoding="utf-8")
            _git("add", "README.md", cwd=work)
            _git("commit", "-m", f"MR {iid} fixture state", cwd=work)
            head = _git("rev-parse", "HEAD", cwd=work)
            _git("push", "origin", f"HEAD:refs/merge-requests/{iid}/head", cwd=work)
            heads[(path, iid)] = head
    return heads


class UnitTransport(httpx.AsyncBaseTransport):
    """Routes by host name to the execution apps a `FakeHost` started, and keeps every
    recording hook the e2e tests assert on."""

    def __init__(self, stack: E2EStack, root: Path) -> None:
        self.stack = stack
        self.root = root
        self.apps: dict[str, FastAPI] = {}
        self.refs: dict[str, str] = {}

    @staticmethod
    def host_of(name: str) -> str:
        return f"{name}.units"

    async def start(self, name: str, launch: dict[str, Any]) -> str:
        """Build the real execution app from `launch.env` verbatim, behind `<name>.units`.
        A unit already there is replaced (an in-place restart): fresh, empty directories and
        a new instance ID."""
        host = self.host_of(name)
        previous = self.apps.pop(host, None)
        if previous is not None:
            await previous.state.service.close()
        # Every start gets fresh, empty directories at the same paths, as a new container would.
        unit_dir = self.root / "units" / name
        if unit_dir.exists():
            shutil.rmtree(unit_dir)
        workspace = unit_dir / "workspace"
        workspace.mkdir(parents=True)
        env = launch["env"]
        app = build_execution_app(
            ExecutionBootstrap.model_validate_json(env["ACH_BOOTSTRAP_JSON"]),
            environ=env, workspace_dir=workspace, sessions_root=unit_dir / "sessions",
        )
        self.apps[host] = app
        self.refs[host] = env["ACH_WORKSPACE_REF"]
        self.stack.minis[app.state.instance_id] = app
        self.stack._workspace_roots[env["ACH_WORKSPACE_REF"]] = workspace
        return f"http://{host}"

    async def stop(self, name: str) -> None:
        app = self.apps.pop(self.host_of(name), None)
        if app is not None:
            await app.state.service.close()
            self.stack.closed_instances.append(app.state.instance_id)

    def hide(self, name: str) -> str:
        """A dishonest host keeps the process alive and moves it to another name."""
        zombie = self.host_of(f"{name}-zombie")
        self.apps[zombie] = self.apps.pop(self.host_of(name))
        self.refs[zombie] = self.refs[self.host_of(name)]
        return zombie

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.stack.requests.append(request)
        if request.url.path == "/healthz" and self.stack.faults.get("healthz"):
            self.stack.faults["healthz"] = False
            self.stack.healthz_failures += 1
            raise httpx.ConnectError("injected health transport failure", request=request)
        host = request.url.host
        app = self.apps.get(host)
        if app is None:
            raise httpx.ConnectError(f"no execution unit answers at {host!r}", request=request)
        instance_id = app.state.instance_id
        workspace_ref = self.refs[host]
        if request.url.path == "/start":
            payload = json.loads(request.content)
            self.stack.start_payloads.append(payload)
            self.stack.start_raw_bodies.append(request.content)
            self.stack.start_instance_ids.append(instance_id)
        if request.method == "PUT" and (
            request.url.path.endswith("/workspace/archive")
            or ("/sessions/" in request.url.path and request.url.path.endswith("/archive"))
        ):
            self.stack.archive_restores.append((instance_id, request.url.path))
        if request.url.path.startswith("/execution/v1/") and request.method == "PUT":
            await request.aread()
            self.stack.archives.append(request.content)
        if request.url.path == "/execution/v1/workspace/handoff":
            checkout = self.stack._workspace_roots[workspace_ref] / "handoff" / "repo"
            _capture_handoff_entry(self.stack, app.state.service, checkout)
            if self.stack.handoff_import_original is None:
                from ach_runtime.execution import snapshots

                original_import = snapshots.import_handoff
                self.stack.handoff_import_original = original_import

                async def observe_import_handoff(service, destination, chunks):
                    return await _observe_handoff_import(
                        self.stack, service, destination, chunks, original_import
                    )

                self.stack.handoff_import_wrapper = observe_import_handoff
                snapshots.import_handoff = observe_import_handoff
        response = await httpx.ASGITransport(app=app).handle_async_request(request)
        if request.url.path == "/configure" and response.status_code == 200:
            await response.aread()
            self.stack.configure_responses.append((instance_id, response.content, dict(response.headers)))
            self.stack.configure_receipts.append((instance_id, response.json()["configVersion"]))
        if request.url.path == "/execution/v1/workspace/handoff":
            self.stack.handoff_suspensions.append({
                session_ref: bool(getattr(native, "clean_exit", False))
                for session_ref, native in app.state.service._engine._sessions.items()
            })
        if request.url.path == "/start" and response.status_code == 202:
            await response.aread()
            self.stack.start_responses.append((instance_id, response.content, dict(response.headers)))
            self.stack.started.append(Started.model_validate(response.json()))
            if self.stack.faults.get("lost_started"):
                self.stack.faults["lost_started"] = False
                await response.aread()
                accepted = self.stack.faults.get("after_lost_started")
                if accepted is not None:
                    await accepted(self.stack, app)
                raise httpx.ReadError("injected response loss after real Started receipt", request=request)
        return response

    async def aclose(self) -> None:
        """Client shutdown: the units outlive any one Control (a restart reuses them)."""

    async def close_all(self) -> None:
        for app in self.apps.values():
            with contextlib.suppress(Exception):
                await app.state.service.close()


@pytest.fixture(scope="module")
def s3_client(client):
    """Alias the existing real S3 integration client fixture; no in-memory fallback."""
    return client


@pytest.fixture
async def e2e_stack_factory(tmp_path: Path, s3_client, monkeypatch) -> AsyncIterator[Callable[..., Awaitable[E2EStack]]]:
    del s3_client  # Construction of the runtime S3Storage uses the same real endpoint/env.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", os.environ.get("ACH_STORAGE_TEST_S3_ACCESS_KEY", "ach-storage-test"))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", os.environ.get("ACH_STORAGE_TEST_S3_SECRET_KEY", "ach-storage-test-secret"))
    upstream_context = fake_facades("review")
    upstream = await upstream_context.__aenter__()
    stacks: list[E2EStack] = []
    try:
        repo_heads = _fixture_repositories(tmp_path / "git")

        async def make_stack(
            *, scenario: str = "review", persistent: bool = False, handoff_scope: str = "event",
            max_steps: int = 4, max_invocation_seconds: int = 30, max_active_workspaces: int = 4,
            max_queued_total: int = 4, session_idle_seconds: int = 2, workspace_idle_seconds: int = 3,
            shutdown_timeout_seconds: int = 300,
        ) -> E2EStack:
            nonlocal upstream_context, upstream
            await upstream.control({"command": "scenario", "text": "pong"})
            # The script wrapper's initial scenario is fixed at process start. Its
            # scenario control changes text/hold state; tool-loop cases use a dedicated
            # fresh wrapper process so request counts remain deterministic.
            if scenario in {"loop", "hold", "deadline", "cancel"}:
                await upstream_context.__aexit__(None, None, None)
                new_context = fake_facades("hold" if scenario in {"hold", "deadline", "cancel"} else "loop")
                upstream_context = new_context
                upstream = await upstream_context.__aenter__()

            wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text(encoding="utf-8"))
            wire["workspace"]["persistence"]["enabled"] = persistent
            wire["workspace"]["session"]["persistence"]["enabled"] = persistent
            wire["workspace"]["session"]["idleTimeoutSeconds"] = session_idle_seconds
            wire["workspace"]["idleTimeoutSeconds"] = workspace_idle_seconds
            wire["workspace"]["shutdownTimeoutSeconds"] = shutdown_timeout_seconds
            wire["artifacts"]["enabled"] = False
            wire["memory"] = None
            wire["limits"]["maxSteps"] = max_steps
            wire["limits"]["maxInvocationSeconds"] = max_invocation_seconds
            wire["channels"][0]["handoff"]["timeoutSeconds"] = min(
                wire["channels"][0]["handoff"]["timeoutSeconds"], max_invocation_seconds
            )
            wire["limits"]["maxActiveWorkspaces"] = max_active_workspaces
            wire["limits"]["maxQueuedTotal"] = max_queued_total
            wire["channels"][0]["handoff"]["scope"] = handoff_scope
            wire["channels"][0]["handoff"]["env"]["GITLAB_REPO_BASEURL"] = f"file://{tmp_path / 'git'}"
            wire["ach"]["baseUrl"] = upstream.model_url.rsplit("/v1", 1)[0]
            wire["ach"]["capability"]["filter"]["exclude"]["mcpServers"] = [
                item for item in wire["ach"]["capability"]["filter"]["exclude"]["mcpServers"]
                if item != "mcp-gitlab-ro"
            ]
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(128)
            facade_endpoint = f"http://127.0.0.1:{listener.getsockname()[1]}"
            wire["infrastructure"] = {"facadeEndpoint": f"{facade_endpoint}/facades"}
            wire["configVersion"] = compute_config_version(wire)
            config: RuntimeConfig = parse_config(wire)

            def catalog(request: httpx.Request) -> httpx.Response:
                if request.url.path.endswith("/platform/hydrate"):
                    return httpx.Response(200, json={
                        "environment": "engineering-prod",
                        "runtime": {"models": [{"id": config.model.name, "endpoint": upstream.model_url}],
                                    "mcpServers": [{"id": "mcp-gitlab-ro", "endpoint": upstream.mcp_url}], "a2aAgents": []},
                        "context": {"skills": [], "prompts": [], "artifacts": []},
                    })
                if request.url.path.endswith("/v2/model/info"):
                    return httpx.Response(200, json={"data": []})
                raise AssertionError(f"unexpected ACH hydration request: {request.url}")

            env = {
                "ACH_SANDBOX_KEY": "e2e-private-harness-key",
                "ACH_SECRET_IDENTITY": "e2e-identity",
                "ACH_SECRET_GITLAB_WEBHOOK": "e2e-webhook-secret",
                "ACH_SECRET_GITLAB_RO": "private-clone-token-only",
                "ACH_STORAGE_S3_BUCKET": os.environ.get("ACH_STORAGE_TEST_S3_BUCKET", "ach-storage-test"),
                "ACH_STORAGE_S3_REGION": os.environ.get("ACH_STORAGE_TEST_S3_REGION", "us-east-1"),
                "ACH_STORAGE_S3_ENDPOINT_URL": os.environ["ACH_STORAGE_TEST_S3_ENDPOINT"],
                "ACH_STORAGE_S3_PREFIX": f"e2e-{uuid.uuid4().hex}",
            }
            monkeypatch.setenv("ACH_SECRET_GITLAB_WEBHOOK", env["ACH_SECRET_GITLAB_WEBHOOK"])
            control = await assemble(config, environ=env, ach_transport=httpx.MockTransport(catalog))
            ingress_app = create_app(control)
            stack = E2EStack(
                control, httpx.AsyncClient(transport=httpx.ASGITransport(app=ingress_app), base_url="http://harness"),
                None, {}, upstream, repo_heads=repo_heads, repo_root=tmp_path / "git",
            )
            control.facade_transport = None
            facade_app = _SwitchApp(create_facade_app(control))
            server = uvicorn.Server(uvicorn.Config(facade_app, log_level="error", lifespan="off"))
            facade_task = asyncio.create_task(server.serve(sockets=[listener]))
            stack.facade_server, stack.facade_task = server, facade_task
            async with asyncio.timeout(5):
                while not server.started:
                    if facade_task.done():
                        error = facade_task.exception()
                        raise RuntimeError("fixture facade server failed to start") from error
                    await asyncio.sleep(0.01)
            units = UnitTransport(stack, tmp_path)
            stack.units = units
            retired: list[Control] = []
            retired_hosts: list[FakeHost] = []

            async def bring_up(current: Control, current_ingress_app: FastAPI, *, shared_units=None, configure_host=None) -> None:
                """Match the production startup ordering: install the fixture transports, start
                the host (it sends the inventory), adopt, then expose readiness."""
                current.mini_client._transport = units
                current.delivery.client._transport = units
                actual_restore = current.lifecycle.restore_workspace

                async def observe_restore(endpoint):
                    stack.workspace_restore_calls.append(endpoint.instance_id)
                    await actual_restore(endpoint)

                current.manager.bind_callbacks(restore=observe_restore)
                stack.host = FakeHost(
                    current.source, host_token(current.key, config.agent.uid), units.start, units.stop,
                    hide_unit=units.hide, units=shared_units,
                )
                if configure_host is not None:
                    configure_host(stack.host)
                stack.host_task = asyncio.create_task(stack.host.run())
                async with asyncio.timeout(10):
                    await current.manager.adopt()
                stack.owned_inventory_ready = True
                current_ingress_app.state.ready = True
                stack.ready_after_discovery = current_ingress_app.state.ready

            async def restart_control(configure_host=None) -> None:
                """A control restart: new epoch, new Control on the same host, execution units
                and facade port. The old Control stops without closing its facade clients, so
                a model request already in flight through them survives."""
                old, old_host = stack.control, stack.host
                stack.host_task.cancel()
                await asyncio.gather(stack.host_task, return_exceptions=True)
                old.source.close()
                await old.admission.close()
                await old.delivery.close()
                retired.append(old)
                retired_hosts.append(old_host)
                await stack.ingress.aclose()
                new = await assemble(config, environ=env, ach_transport=httpx.MockTransport(catalog))
                new.facade_transport = None
                new_ingress_app = create_app(new)
                stack.control = new
                stack.ingress = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=new_ingress_app), base_url="http://harness"
                )
                facade_app.app = create_facade_app(new)
                await bring_up(new, new_ingress_app, shared_units=old_host.units, configure_host=configure_host)

            stack.restart_control = restart_control
            stack.retired = retired
            await bring_up(control, ingress_app)
            stacks.append(stack)
            return stack

        yield make_stack
    finally:
        for stack in reversed(stacks):
            if stack.handoff_import_original is not None:
                from ach_runtime.execution import snapshots
                if snapshots.import_handoff is stack.handoff_import_wrapper:
                    snapshots.import_handoff = stack.handoff_import_original
            if stack.host_task is not None:
                stack.host_task.cancel()
                await asyncio.gather(stack.host_task, return_exceptions=True)
            await stack.ingress.aclose()
            await stack.control.close()
            for old in stack.retired:
                with contextlib.suppress(Exception):
                    await old.close()
            if stack.units is not None:
                await stack.units.close_all()
            if stack.facade_server is not None:
                stack.facade_server.should_exit = True
            if stack.facade_task is not None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stack.facade_task, timeout=5)
        await upstream_context.__aexit__(None, None, None)


@pytest.fixture(autouse=True)
def require_e2e_prerequisites() -> None:
    """Make missing native/S3 prerequisites a hard failure when a test is executed."""
    if not os.environ.get("ACH_STORAGE_TEST_S3_ENDPOINT"):
        pytest.fail("mandatory E2E prerequisite missing: ACH_STORAGE_TEST_S3_ENDPOINT", pytrace=False)
    if not os.environ.get("OPENCODE_BIN"):
        pytest.fail("mandatory E2E prerequisite missing: OPENCODE_BIN", pytrace=False)

from __future__ import annotations

import json
import signal
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.admission import Admission
from ach_runtime.harness.boot import assemble, run_control
from ach_runtime.harness.control import Control
from ach_runtime.harness.control import create_facade_app
from ach_runtime.harness.delivery import Delivery
from ach_runtime.harness.hydrate import HydratedCatalog
from ach_runtime.harness.hosted import HostedSource
from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.harness.tokens import engine_bearer, facade_token, host_token
from ach_runtime.harness.workspaces import WorkspaceEndpoint, WorkspaceManager, WorkspaceRef
from ach_runtime.__main__ import main


def _supported_config():
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["channels"][0].pop("handoff", None)
    wire["channels"][0]["prompt"] = "Hello {{ payload.title }}"
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def _ach_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/platform/hydrate":
        return httpx.Response(200, json={
            "environment": "engineering-prod",
            "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                        "mcpServers": [{"id": "ach-memory", "endpoint": "https://memory.example/mcp"}],
                        "a2aAgents": []},
            "context": {"skills": [], "prompts": [], "artifacts": []},
        })
    if request.url.path == "/v2/model/info":
        return httpx.Response(200, json={"data": []})
    raise AssertionError(request.url)


class _Mountable:
    """Stands in for the facade app: records what `run_control` mounts on it."""

    def __init__(self) -> None:
        self.mounts: list[tuple[str, object]] = []
        self.router = SimpleNamespace(lifespan_context=_noop_lifespan)

    def mount(self, path: str, app: object) -> None:
        self.mounts.append((path, app))


def _source(config) -> HostedSource:
    return HostedSource(
        ExecutionBootstrap(
            bootstrapVersion="workspace-v3", agent=config.agent, facadeEndpoint=config.infrastructure.facade_endpoint
        ),
        verify_key="verify-key",
    )


class _Closing:
    """A source whose `close()` is observable."""

    def __init__(self, events: list, name: str = "source-close") -> None:
        self._events, self._name = events, name

    def close(self) -> None:
        self._events.append(self._name)


async def test_assembly_uses_real_components_and_disabled_storage_path() -> None:
    config = _supported_config()
    control = await assemble(
        config,
        environ={"ACH_SANDBOX_KEY": "harness-key", "ACH_SECRET_IDENTITY": "ek-secret"},
        ach_transport=httpx.MockTransport(_ach_transport),
        mini_transport=httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError(request.url))),
    )
    try:
        assert isinstance(control, Control)
        assert isinstance(control.catalog, HydratedCatalog)
        assert isinstance(control.source, HostedSource)
        assert isinstance(control.manager, WorkspaceManager)
        assert isinstance(control.delivery, Delivery)
        assert isinstance(control.admission, Admission)
        assert isinstance(control.model_facade, ModelFacade)
        assert isinstance(control.mcp_facade, McpFacade)
        assert control.memory_facade is not None
        assert control.storage is None
        assert control.manager._capacity == config.limits.max_active_workspaces
        assert control.admission._max_concurrent_invocations == config.limits.max_concurrent_invocations
        assert control.admission._max_queued_total == config.limits.max_queued_total
        assert control.admission._channel_concurrency["gitlab-mr-review"] == 4
        assert control.verify_key
        assert control.public.config_version == config.config_version
        workspace = WorkspaceEndpoint(
            WorkspaceRef(config.agent.namespace, config.agent.uid, "a" * 64),
            "ach-workspace", "pod-1", "http://mini",
        )
        catalog = control._catalog_for(workspace)
        assert "ach-memory" not in catalog["mcp"]
        assert catalog["mcp"]["memory"]["url"].startswith(
            f"{config.infrastructure.facade_endpoint}/memory/t/"
        )
        facade_app = create_facade_app(control)
        assert {route.path for route in facade_app.router.routes} == {
            "/facades/model", "/facades/mcp", "/facades/memory"
        }
        async with facade_app.router.lifespan_context(facade_app):
            pass
    finally:
        await control.close()


async def test_invalid_boot_configuration_emits_finite_safe_anomaly(tmp_path, caplog) -> None:
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL

    config = tmp_path / "invalid-config.json"
    config.write_text('{"secret":"boot-secret"}', encoding="utf-8")
    before = ANOMALIES_TOTAL.labels(kind="configuration_invalid")._value.get()
    with pytest.raises(Exception):
        await run_control(config)
    after = ANOMALIES_TOTAL.labels(kind="configuration_invalid")._value.get()
    assert after == before + 1
    assert "boot-secret" not in caplog.text


async def test_app_health_readiness_stays_closed_until_discovery():
    from ach_runtime.harness.app import create_app

    control = await assemble(
        _supported_config(), environ={"ACH_SANDBOX_KEY": "key", "ACH_SECRET_IDENTITY": "ek"},
        ach_transport=httpx.MockTransport(_ach_transport),
    )
    app = create_app(control)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        assert (await client.get("/healthz")).status_code == 200
        assert (await client.get("/readyz")).status_code == 503
        app.state.ready = True
        assert (await client.get("/readyz")).status_code == 200
    await control.close()


async def test_run_control_keeps_root_signal_owner_and_facades_through_ingress_drain(
    tmp_path: Path, monkeypatch,
) -> None:
    import asyncio
    import sys
    from ach_runtime.harness import boot

    config_path = tmp_path / "control.json"
    wire_config = _supported_config().model_dump(by_alias=True, mode="json")
    wire_config["configVersion"] = compute_config_version(wire_config)
    config_path.write_text(json.dumps(wire_config))
    events: list[tuple[str, int | signal.Signals | None]] = []
    handlers: dict[signal.Signals, object] = {}
    instances = []

    class FakeConfig:
        def __init__(self, app, *, host, port, lifespan, timeout_graceful_shutdown, access_log=True):
            self.app, self.port, self.access_log = app, port, access_log
            self.timeout_graceful_shutdown = timeout_graceful_shutdown

    class FakeServer:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.should_exit = False
            instances.append(self)

        @contextmanager
        def capture_signals(self):
            events.append(("uvicorn-handler", self.config.port))
            yield

        async def serve(self):
            with self.capture_signals():
                self.started = True
                if len([item for item in instances if item.started]) == 2:
                    asyncio.get_running_loop().call_later(0.2, handlers[signal.SIGTERM])
                while not self.should_exit:
                    await asyncio.sleep(0.005)
                events.append(("listener-exit", self.config.port))

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer))
    ingress = SimpleNamespace(state=SimpleNamespace(ready=False))
    facades = _Mountable()
    monkeypatch.setattr(boot, "create_app", lambda control: ingress)
    monkeypatch.setattr(boot, "create_facade_app", lambda control: facades)

    class Control:
        _stopping = False
        admission = SimpleNamespace(queued_total=0)
        key = b"harness-key"
        source = _Closing(events)

        class Manager:
            async def adopt(self):
                events.append(("discovery", None))

        manager = Manager()

        class Lifecycle:
            async def start(self):
                assert ingress.state.ready is False
                events.append(("lifecycle-start", None))

        lifecycle = Lifecycle()

        async def close(self):
            events.append(("close", None))

    control = Control()

    async def assemble(config):
        return control

    loop = asyncio.get_running_loop()

    def add_handler(sig, callback):
        events.append(("root-handler", sig))
        handlers[sig] = callback

    def remove_handler(sig):
        events.append(("remove-root-handler", sig))
        return True

    monkeypatch.setattr(loop, "add_signal_handler", add_handler)
    monkeypatch.setattr(loop, "remove_signal_handler", remove_handler)
    await boot.run_control(config_path, assembly=assemble)

    assert [event[0] for event in events].count("root-handler") == 2
    assert not any(event[0] == "uvicorn-handler" for event in events)
    # The facade listener's URLs carry the Workspace HMAC: no access log there.
    assert [(s.config.app, s.config.access_log) for s in instances] == [(ingress, True), (facades, False)]
    assert max(i for i, event in enumerate(events) if event[0] == "root-handler") < events.index(("discovery", None))
    assert events.index(("discovery", None)) < events.index(("lifecycle-start", None))
    assert events.index(("lifecycle-start", None)) < events.index(("listener-exit", 8080))
    assert ingress.state.ready is False
    assert events.index(("listener-exit", 8080)) < events.index(("listener-exit", 8081))
    assert events.index(("listener-exit", 8081)) < events.index(("close", None))
    # Ingress keeps the configured grace; the drained facade listener stops idle MCP streams fast.
    shutdown_timeout = _supported_config().workspace.shutdown_timeout_seconds
    assert [s.config.timeout_graceful_shutdown for s in instances] == [
        shutdown_timeout, boot.FACADE_DRAINED_GRACE_SECONDS,
    ]
    # The host API is mounted once on the facade listener, before either listener starts.
    assert [path for path, _app in facades.mounts] == ["/host/v1"]
    assert events.index("source-close") < events.index(("listener-exit", 8080)), "the ready path closes the source with `_stopping`"


async def test_recovered_boot_accounting_precedes_ingress(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import sys
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone

    from fastapi import FastAPI
    from ach_runtime.execution.wire import RunStatus
    from ach_runtime.harness import boot
    from ach_runtime.harness.admission import Admission
    from ach_runtime.harness.app import create_app

    config = _supported_config()
    path = tmp_path / "control.json"
    wire = config.model_dump(by_alias=True, mode="json")
    wire["configVersion"] = compute_config_version(wire)
    path.write_text(json.dumps(wire))
    scan_started, scan_gate, observer_gate = asyncio.Event(), asyncio.Event(), asyncio.Event()
    events: list[str] = []
    handlers = {}
    servers = []

    @asynccontextmanager
    async def facade_lifespan(_app):
        events.append("facade-start")
        try:
            yield
        finally:
            events.append("facade-stop")

    facades = FastAPI(lifespan=facade_lifespan)
    monkeypatch.setattr(boot, "create_facade_app", lambda _control: facades)

    class FakeConfig:
        def __init__(self, app, *, host, port, lifespan, timeout_graceful_shutdown, access_log=True):
            self.app, self.port = app, port

    class FakeServer:
        def __init__(self, config):
            self.config, self.started, self.should_exit = config, False, False
            servers.append(self)

        @contextmanager
        def capture_signals(self):
            yield

        async def serve(self):
            app = self.config.app
            async with app.router.lifespan_context(app):
                self.started = True
                events.append(f"listener-start-{self.config.port}")
                while not self.should_exit:
                    await asyncio.sleep(0.005)
                events.append(f"listener-stop-{self.config.port}")

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer))

    async def runner(_event, ticket):
        ticket.mark_started()

    admission = Admission(
        namespace=config.agent.namespace, agent_uid=config.agent.uid,
        max_concurrent_invocations=1, max_queued_total=10,
        idempotency_window_seconds=60, max_invocation_seconds=600,
        workspace_max_concurrent_sessions=1, channel_concurrency={"gitlab-mr-review": 1},
        clock=lambda: datetime.now(timezone.utc), runner=runner,
    )
    inventory = RunStatus(
        invocationId="boot-recovered", channel="gitlab-mr-review", workspaceRef="a" * 64,
        sessionRef="b" * 64, runId="run-1", configVersion="f" * 64,
        deadlineAt=datetime.now(timezone.utc) + timedelta(minutes=5), state="running",
    )
    endpoint = WorkspaceEndpoint(
        WorkspaceRef(config.agent.namespace, config.agent.uid, inventory.workspace_ref),
        "workspace", "pod-1", "http://mini",
    )

    class Control:
        def __init__(self):
            self._stopping = False
            self.config = config
            self.admission = admission
            self.key = b"harness-key"
            self.source = _source(config)

            class Manager:
                async def adopt(inner_self):
                    scan_started.set()
                    await scan_gate.wait()
                    self.admission.recover(
                        invocation_id=inventory.invocation_id, channel=inventory.channel,
                        workspace_ref=inventory.workspace_ref, session_ref=inventory.session_ref,
                        config_version=inventory.config_version, deadline_at=inventory.deadline_at,
                        instance_id=endpoint.instance_id, run_id=inventory.run_id,
                        observer=observer_gate.wait,
                    )
                    events.append("owners-accounted")

            self.manager = Manager()

            class Lifecycle:
                async def start(inner_self):
                    assert self.admission.queued_total == 1
                    assert self.admission.has_work(inventory.workspace_ref, inventory.session_ref)
                    events.append("lifecycle-start")

            self.lifecycle = Lifecycle()

        async def close(self):
            events.append("close")
            await self.admission.close()

    control = Control()
    ingress = create_app(control)
    monkeypatch.setattr(boot, "create_app", lambda _control: ingress)

    async def assembly(_config):
        return control

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)
    task = asyncio.create_task(boot.run_control(path, assembly=assembly))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=ingress), base_url="http://control")
    try:
        for _ in range(200):
            if len(servers) == 2 and all(server.started for server in servers):
                break
            await asyncio.sleep(0.005)
        assert all(server.started for server in servers)
        assert "facade-start" in events
        await scan_started.wait()
        assert (await client.get("/readyz")).status_code == 503
        rejected = await client.post("/channels/gitlab-mr-review/events", json={})
        assert rejected.status_code == 503
        assert "owners-accounted" not in events
        scan_gate.set()
        for _ in range(200):
            if ingress.state.ready:
                break
            await asyncio.sleep(0.005)
        assert ingress.state.ready
        assert admission.queued_total == 1
        assert admission.has_work(inventory.workspace_ref, inventory.session_ref)
        assert not observer_gate.is_set()
        assert (await client.get("/readyz")).status_code == 200
        assert events.index("listener-start-8080") < events.index("owners-accounted")
        assert events.index("listener-start-8081") < events.index("owners-accounted")
        assert events.index("facade-start") < events.index("owners-accounted")
        assert events.index("owners-accounted") < events.index("lifecycle-start")
        handlers[signal.SIGTERM]()
        for _ in range(100):
            if "listener-stop-8080" in events:
                break
            await asyncio.sleep(0.005)
        assert "listener-stop-8080" in events
        assert "facade-stop" not in events
        observer_gate.set()
        await task
        assert events.index("facade-stop") > events.index("owners-accounted")
    finally:
        scan_gate.set()
        observer_gate.set()
        await client.aclose()
        if not task.done():
            for handler in handlers.values():
                handler()
            await task


async def test_recovered_boot_failure_keeps_ingress_closed(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import sys
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone

    from fastapi import FastAPI
    from ach_runtime.execution.wire import RunStatus
    from ach_runtime.harness import boot
    from ach_runtime.harness.admission import Admission
    from ach_runtime.harness.app import create_app

    config = _supported_config()
    path = tmp_path / "control.json"
    wire = config.model_dump(by_alias=True, mode="json")
    wire["configVersion"] = compute_config_version(wire)
    path.write_text(json.dumps(wire))
    observer_cancelled, discovery_gate, discovery_registered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    handlers = {}
    servers = []

    @asynccontextmanager
    async def facade_lifespan(_app):
        yield

    facades = FastAPI(lifespan=facade_lifespan)
    monkeypatch.setattr(boot, "create_facade_app", lambda _control: facades)

    class FakeConfig:
        def __init__(self, app, *, host, port, lifespan, timeout_graceful_shutdown, access_log=True):
            self.app, self.port = app, port

    class FakeServer:
        def __init__(self, config):
            self.config, self.started, self.should_exit = config, False, False
            servers.append(self)

        @contextmanager
        def capture_signals(self):
            yield

        async def serve(self):
            app = self.config.app
            async with app.router.lifespan_context(app):
                self.started = True
                while not self.should_exit:
                    await asyncio.sleep(0.005)

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer))

    async def runner(_event, ticket):
        ticket.mark_started()

    admission = Admission(
        namespace=config.agent.namespace, agent_uid=config.agent.uid,
        max_concurrent_invocations=1, max_queued_total=10, idempotency_window_seconds=60,
        max_invocation_seconds=600, workspace_max_concurrent_sessions=1,
        channel_concurrency={"gitlab-mr-review": 1}, clock=lambda: datetime.now(timezone.utc), runner=runner,
    )
    inventory = RunStatus(
        invocationId="failed-boot-owner", channel="gitlab-mr-review", workspaceRef="a" * 64,
        sessionRef="b" * 64, runId="run-1", configVersion="f" * 64,
        deadlineAt=datetime.now(timezone.utc) + timedelta(minutes=5), state="running",
    )

    class Control:
        def __init__(self):
            self._stopping = False
            self.config = config
            self.admission = admission
            self.key = b"harness-key"
            self.source = _source(config)

            class Manager:
                async def adopt(inner_self):
                    for server in servers:
                        assert server.started
                    self.admission.recover(
                        invocation_id=inventory.invocation_id, channel=inventory.channel,
                        workspace_ref=inventory.workspace_ref, session_ref=inventory.session_ref,
                        config_version=inventory.config_version, deadline_at=inventory.deadline_at,
                        instance_id="pod-1", run_id=inventory.run_id, observer=observe,
                    )
                    discovery_registered.set()
                    await discovery_gate.wait()
                    raise RuntimeError("discovery failed")

            async def observe():
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    observer_cancelled.set()
                    raise

            self.manager = Manager()
            class Lifecycle:
                async def start(inner_self):
                    pass

            self.lifecycle = Lifecycle()

        async def close(self):
            await self.admission.close()

    control = Control()
    ingress = create_app(control)
    monkeypatch.setattr(boot, "create_app", lambda _control: ingress)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)

    async def assembly(_config):
        return control

    task = asyncio.create_task(boot.run_control(path, assembly=assembly))
    try:
        for _ in range(200):
            if len(servers) == 2 and all(server.started for server in servers):
                break
            await asyncio.sleep(0.005)
        assert all(server.started for server in servers)
        await discovery_registered.wait()
        assert not ingress.state.ready
        assert admission.queued_total == 1
        assert admission.has_work(inventory.workspace_ref, inventory.session_ref)
        discovery_gate.set()
        with pytest.raises(RuntimeError, match="discovery failed"):
            await task
        assert not ingress.state.ready
        assert observer_cancelled.is_set()
        assert admission.queued_total == 0
    finally:
        discovery_gate.set()
        if not task.done():
            for handler in handlers.values():
                handler()
            await asyncio.gather(task, return_exceptions=True)


async def test_all_channel_sources_start_after_discovery_and_stop_before_drain(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    import sys
    from ach_runtime.harness import boot

    config = _supported_config()
    wire = config.model_dump(by_alias=True, mode="json")
    wire["channels"].extend([
        {"name": "morning", "type": "cron", "cron": {"schedule": "0 8 * * *", "timezone": "Europe/Madrid"}},
        {"name": "jobs", "type": "queue", "queue": {"type": "redis", "key": "ach:jobs"}, "routing": {"sessionKey": "main"}},
        {"name": "jobs-secondary", "type": "queue", "queue": {"type": "redis", "key": "ach:jobs-secondary"}, "routing": {"sessionKey": "main"}},
    ])
    wire["configVersion"] = compute_config_version(wire)
    config_path = tmp_path / "control-sources.json"
    config_path.write_text(json.dumps(wire))
    events: list[str] = []
    handlers = {}
    servers = []
    ingress = SimpleNamespace(state=SimpleNamespace(ready=False))
    monkeypatch.setattr(boot, "create_app", lambda _control: ingress)
    monkeypatch.setattr(boot, "create_facade_app", lambda _control: _Mountable())

    class FakeConfig:
        def __init__(self, app, *, host, port, lifespan, timeout_graceful_shutdown, access_log=True):
            self.port = port

    class FakeServer:
        def __init__(self, config):
            self.config, self.started, self.should_exit = config, False, False
            servers.append(self)

        @contextmanager
        def capture_signals(self):
            yield

        async def serve(self):
            self.started = True
            if len(servers) == 2:
                asyncio.get_running_loop().call_later(0.08, handlers[signal.SIGTERM])
            while not self.should_exit:
                await asyncio.sleep(0.002)
            events.append(f"listener-stop-{self.config.port}")

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer))

    class Source:
        def __init__(self, name):
            self.name = name

        async def start(self):
            assert ingress.state.ready is False
            events.append(f"start-{self.name}")

        async def stop(self):
            events.append(f"stop-{self.name}")

    class Cron(Source):
        def __init__(self, channels, _control):
            assert [item.name for item in channels] == ["morning"]
            events.append("cron-instance")
            super().__init__("cron")

    class Queue(Source):
        def __init__(self, channel, _control):
            events.append(f"queue-instance-{channel.name}")
            super().__init__(f"queue-{channel.name}")

    from ach_runtime.channels import cron, queue
    monkeypatch.setattr(cron, "CronScheduler", Cron)
    monkeypatch.setattr(queue, "QueueConsumer", Queue)

    class Admission:
        @property
        def queued_total(self):
            events.append("drain-check")
            return 0

    class Control:
        def __init__(self):
            self.config = config
            self._stopping = False
            self.admission = Admission()
            self.key = b"harness-key"
            self.source = _source(config)

            class Manager:
                async def adopt(inner_self):
                    events.append("discovery")

            class Lifecycle:
                async def start(inner_self):
                    events.append("lifecycle-start")

            self.manager = Manager()
            self.lifecycle = Lifecycle()

        async def close(self):
            events.append("control-close")

    control = Control()
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(asyncio.get_running_loop(), "remove_signal_handler", lambda _sig: True)
    async def assemble(_config):
        return control

    await boot.run_control(config_path, assembly=assemble)
    assert events.count("cron-instance") == 1
    assert events.count("queue-instance-jobs") == 1
    assert events.count("queue-instance-jobs-secondary") == 1
    assert events.index("discovery") < events.index("lifecycle-start") < events.index("cron-instance")
    assert events.index("start-cron") < events.index("start-queue-jobs") < events.index("start-queue-jobs-secondary")
    assert events.index("stop-cron") < events.index("drain-check")
    assert events.index("stop-queue-jobs") < events.index("drain-check")
    assert events.index("stop-queue-jobs-secondary") < events.index("drain-check")
    assert events.index("listener-stop-8081") < events.index("control-close")


async def test_source_start_failure_rolls_back_started_sources(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import sys
    from ach_runtime.harness import boot

    config = _supported_config()
    wire = config.model_dump(by_alias=True, mode="json")
    wire["channels"].extend([
        {"name": "morning", "type": "cron", "cron": {"schedule": "0 8 * * *"}},
        {"name": "jobs", "type": "queue", "queue": {"type": "redis", "key": "ach:jobs"}, "routing": {"sessionKey": "main"}},
    ])
    wire["configVersion"] = compute_config_version(wire)
    config_path = tmp_path / "control-source-failure.json"
    config_path.write_text(json.dumps(wire))
    events: list[str] = []
    ingress = SimpleNamespace(state=SimpleNamespace(ready=False))
    monkeypatch.setattr(boot, "create_app", lambda _control: ingress)
    monkeypatch.setattr(boot, "create_facade_app", lambda _control: _Mountable())

    class FakeConfig:
        def __init__(self, app, *, host, port, lifespan, timeout_graceful_shutdown, access_log=True):
            self.port = port

    class FakeServer:
        def __init__(self, config):
            self.config, self.started, self.should_exit = config, False, False

        @contextmanager
        def capture_signals(self):
            yield

        async def serve(self):
            self.started = True
            while not self.should_exit:
                await asyncio.sleep(0.002)

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer))

    class Cron:
        def __init__(self, channels, _control):
            pass

        async def start(self):
            events.append("cron-start")

        async def stop(self):
            events.append("cron-stop")

    class Queue:
        def __init__(self, channel, _control):
            pass

        async def start(self):
            events.append("queue-start")
            raise RuntimeError("source startup failed")

        async def stop(self):
            events.append("queue-stop")

    class A2ABridge:
        async def stop(self):
            events.append("a2a-stop")

    ingress.state.a2a_bridges = {"review": A2ABridge()}

    from ach_runtime.channels import cron, queue
    monkeypatch.setattr(cron, "CronScheduler", Cron)
    monkeypatch.setattr(queue, "QueueConsumer", Queue)
    handlers = {}
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)

    class Control:
        def __init__(self):
            self.config = config
            self._stopping = False
            self.admission = SimpleNamespace(queued_total=0)
            self.key = b"harness-key"
            self.source = _source(config)

            class Manager:
                async def adopt(inner_self):
                    events.append("discovery")

            self.manager = Manager()
            class Lifecycle:
                async def start(inner_self):
                    pass

            self.lifecycle = Lifecycle()

        async def close(self):
            events.append("close")

    async def assemble(_config):
        return Control()

    with pytest.raises(RuntimeError, match="source startup failed"):
        await boot.run_control(config_path, assembly=assemble)
    assert ingress.state.ready is False
    assert events == ["discovery", "cron-start", "queue-start", "queue-stop", "cron-stop", "a2a-stop", "close"]


async def test_abnormal_listener_stops_sources_before_facade_shutdown(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    import sys
    from ach_runtime.channels import cron
    from ach_runtime.harness import boot

    config = _supported_config()
    wire = config.model_dump(by_alias=True, mode="json")
    wire["channels"].append({"name": "morning", "type": "cron", "cron": {"schedule": "0 8 * * *"}})
    wire["configVersion"] = compute_config_version(wire)
    config_path = tmp_path / "control-abnormal-listener.json"
    config_path.write_text(json.dumps(wire))
    events: list[str] = []
    handlers = {}
    ingress = SimpleNamespace(state=SimpleNamespace(ready=False))
    monkeypatch.setattr(boot, "create_app", lambda _control: ingress)
    monkeypatch.setattr(boot, "create_facade_app", lambda _control: _Mountable())

    class FakeConfig:
        def __init__(self, app, *, host, port, lifespan, timeout_graceful_shutdown, access_log=True):
            self.port = port

    class FakeServer:
        def __init__(self, config):
            self.config, self.started, self.should_exit = config, False, False

        @contextmanager
        def capture_signals(self):
            yield

        async def serve(self):
            self.started = True
            if self.config.port == 8080:
                while not ingress.state.ready:
                    await asyncio.sleep(0.002)
                events.append("listener-abnormal-8080")
                return
            while not self.should_exit:
                await asyncio.sleep(0.002)
            events.append("listener-stop-8081")

    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer))

    class Source:
        def __init__(self, _channels, _control):
            pass

        async def start(self):
            events.append("source-start")

        async def stop(self):
            events.append("source-stop")

    class A2ABridge:
        async def stop(self):
            events.append("a2a-stop")

    ingress.state.a2a_bridges = {"review": A2ABridge()}

    monkeypatch.setattr(cron, "CronScheduler", Source)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: handlers.__setitem__(sig, callback))
    monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)

    class Control:
        def __init__(self):
            self.config = config
            self._stopping = False
            self.admission = SimpleNamespace(queued_total=0)
            self.key = b"harness-key"
            self.source = _source(config)

            class Manager:
                async def adopt(inner_self):
                    events.append("discovery")

            class Lifecycle:
                async def start(inner_self):
                    events.append("lifecycle-start")

            self.manager = Manager()
            self.lifecycle = Lifecycle()

        async def close(self):
            events.append("control-close")

    async def assemble(_config):
        return Control()

    with pytest.raises(RuntimeError, match="listener stopped unexpectedly"):
        await boot.run_control(config_path, assembly=assemble)

    assert events.index("source-stop") < events.index("listener-stop-8081")
    assert events.index("a2a-stop") < events.index("listener-stop-8081")
    assert events.index("listener-stop-8081") < events.index("control-close")


def test_control_cli_help_and_dispatch(monkeypatch, capsys) -> None:
    import asyncio
    from ach_runtime.harness import boot

    with pytest.raises(SystemExit) as help_exit:
        main(["control", "--help"])
    assert help_exit.value.code == 0
    help_text = capsys.readouterr().out
    assert "--config" in help_text

    called: list[Path] = []

    async def run_control(config_path: Path) -> None:
        called.append(config_path)

    monkeypatch.setattr(boot, "run_control", run_control)
    assert main(["control"]) == 0
    assert called == [Path("/etc/ach-runtime/config.json")]


# ---- the host API and adoption, with the real components ------------------------------------------

_ENV = {"ACH_SANDBOX_KEY": "harness-key", "ACH_SECRET_IDENTITY": "ek-secret"}


class _Listeners:
    """Fake uvicorn: records the two apps `run_control` serves and lets the test drive them."""

    def __init__(self, monkeypatch) -> None:
        import asyncio
        import sys

        self.servers: list = []
        owner = self

        class FakeConfig:
            def __init__(self, app, *, host, port, lifespan, timeout_graceful_shutdown, access_log=True):
                self.app, self.port = app, port

        class FakeServer:
            def __init__(self, config):
                self.config, self.started, self.should_exit = config, False, False
                owner.servers.append(self)

            @contextmanager
            def capture_signals(self):
                yield

            async def serve(self):
                app = self.config.app
                async with app.router.lifespan_context(app):
                    self.started = True
                    while not self.should_exit:
                        await asyncio.sleep(0.005)

        monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=FakeConfig, Server=FakeServer))
        self.handlers: dict = {}
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", lambda sig, callback: self.handlers.__setitem__(sig, callback))
        monkeypatch.setattr(loop, "remove_signal_handler", lambda _sig: True)

    async def started(self) -> None:
        import asyncio

        async with asyncio.timeout(5):
            while len(self.servers) < 2 or not all(server.started for server in self.servers):
                await asyncio.sleep(0.005)

    def client(self, index: int) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.servers[index].config.app), base_url="http://control"
        )

    def terminate(self) -> None:
        self.handlers[signal.SIGTERM]()


def _real_assembly(controls: list):
    async def assembly(config):
        control = await assemble(
            config, environ=_ENV, ach_transport=httpx.MockTransport(_ach_transport),
        )
        controls.append(control)
        return control

    return assembly


def _config_file(tmp_path: Path) -> Path:
    path = tmp_path / "control.json"
    wire = _supported_config().model_dump(by_alias=True, mode="json")
    wire["configVersion"] = compute_config_version(wire)
    path.write_text(json.dumps(wire))
    return path


async def test_host_api_answers_before_readiness_and_readiness_waits_for_adoption(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    from ach_runtime.harness import boot

    listeners = _Listeners(monkeypatch)
    controls: list[Control] = []
    config = _supported_config()
    host_auth = {"authorization": f"Bearer {host_token(_ENV['ACH_SANDBOX_KEY'].encode(), config.agent.uid)}"}
    task = asyncio.create_task(boot.run_control(_config_file(tmp_path), assembly=_real_assembly(controls)))
    try:
        await listeners.started()
        ingress, facades = listeners.client(0), listeners.client(1)
        async with ingress, facades:
            # not ready: the inventory has not arrived
            assert (await ingress.get("/healthz")).status_code == 200
            assert (await ingress.get("/readyz")).status_code == 503
            listed = await facades.get("/host/v1/workspaces", headers=host_auth)
            assert listed.status_code == 200
            assert listed.json()["adopting"] is True and listed.json()["workspaces"] == []
            # another credential is not accepted on a host route
            key = _ENV["ACH_SANDBOX_KEY"].encode()
            for token in (facade_token(key, "ws"), engine_bearer(key, "ws")):
                assert (await facades.get(
                    "/host/v1/workspaces", headers={"authorization": f"Bearer {token}"}
                )).status_code == 401
            assert (await facades.get("/host/v1/workspaces")).status_code == 401
            # the other half: a host token is not a facade credential
            host_as_facade = await facades.post(
                f"/facades/model/t/{host_auth['authorization'].split()[1]}/v1/models", json={}
            )
            assert host_as_facade.status_code == 403
            assert (await ingress.get("/readyz")).status_code == 503

            epoch = listed.json()["runtimeEpoch"]
            sent = await facades.put(
                "/host/v1/inventory", headers=host_auth, json={"runtimeEpoch": epoch, "units": []}
            )
            assert sent.status_code == 202
            async with asyncio.timeout(5):
                while (await ingress.get("/readyz")).status_code != 200:
                    await asyncio.sleep(0.01)
    finally:
        listeners.terminate()
        await task


async def test_shutdown_during_adoption_answers_pending_polls_503_and_cancels_adoption(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    from ach_runtime.harness import boot

    listeners = _Listeners(monkeypatch)
    controls: list[Control] = []
    config = _supported_config()
    host_auth = {"authorization": f"Bearer {host_token(_ENV['ACH_SANDBOX_KEY'].encode(), config.agent.uid)}"}
    task = asyncio.create_task(boot.run_control(_config_file(tmp_path), assembly=_real_assembly(controls)))
    await listeners.started()
    facades = listeners.client(1)
    async with facades:
        cursor = (await facades.get("/host/v1/workspaces", headers=host_auth)).json()["cursor"]
        pending = asyncio.create_task(facades.get(
            "/host/v1/workspaces", headers=host_auth, params={"after": cursor, "waitSeconds": 30}
        ))
        await asyncio.sleep(0.05)
        assert not pending.done(), "the long-poll is held"
        listeners.terminate()
        assert (await asyncio.wait_for(pending, 5)).status_code == 503
        with pytest.raises(RuntimeError, match="stopped during Workspace discovery"):
            await asyncio.wait_for(task, 10)
    assert controls[0].manager._adoption.cancelled(), "adopt() does not outlive the shutdown"
    assert controls[0].source.stopping


async def test_shutdown_with_an_established_poll_answers_503_not_a_snapshot(tmp_path: Path, monkeypatch) -> None:
    import asyncio
    from ach_runtime.harness import boot

    listeners = _Listeners(monkeypatch)
    controls: list[Control] = []
    config = _supported_config()
    host_auth = {"authorization": f"Bearer {host_token(_ENV['ACH_SANDBOX_KEY'].encode(), config.agent.uid)}"}
    task = asyncio.create_task(boot.run_control(_config_file(tmp_path), assembly=_real_assembly(controls)))
    await listeners.started()
    ingress, facades = listeners.client(0), listeners.client(1)
    async with ingress, facades:
        first = (await facades.get("/host/v1/workspaces", headers=host_auth)).json()
        assert (await facades.put(
            "/host/v1/inventory", headers=host_auth, json={"runtimeEpoch": first["runtimeEpoch"], "units": []}
        )).status_code == 202
        async with asyncio.timeout(5):
            while (await ingress.get("/readyz")).status_code != 200:
                await asyncio.sleep(0.01)
        cursor = (await facades.get("/host/v1/workspaces", headers=host_auth)).json()["cursor"]
        pending = asyncio.create_task(facades.get(
            "/host/v1/workspaces", headers=host_auth, params={"after": cursor, "waitSeconds": 30}
        ))
        await asyncio.sleep(0.05)
        assert not pending.done()
        listeners.terminate()
        assert (await asyncio.wait_for(pending, 5)).status_code == 503
        await asyncio.wait_for(task, 10)


async def test_pre_readiness_shutdown_closes_the_source_before_the_listeners_stop(
    tmp_path: Path, monkeypatch
) -> None:
    import asyncio
    from ach_runtime.harness import boot

    listeners = _Listeners(monkeypatch)
    events: list[str] = []
    gate = asyncio.Event()

    class Control:
        _stopping = False
        admission = SimpleNamespace(queued_total=0)
        key = b"harness-key"
        source = _Closing(events)

        class Manager:
            async def adopt(self):
                await gate.wait()

        manager = Manager()

        async def close(self):
            events.append("close")

    async def assembly(_config):
        return Control()

    monkeypatch.setattr(boot, "create_app", lambda _control: SimpleNamespace(
        state=SimpleNamespace(ready=False), router=SimpleNamespace(lifespan_context=_noop_lifespan)))
    monkeypatch.setattr(boot, "create_facade_app", lambda _control: _Mountable())
    task = asyncio.create_task(boot.run_control(_config_file(tmp_path), assembly=assembly))
    try:
        await listeners.started()
        listeners.terminate()
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(task, 10)
        assert events == ["source-close", "close"]
    finally:
        gate.set()


def _noop_lifespan(_app):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan():
        yield

    return lifespan()


async def test_a_config_without_a_facade_endpoint_fails_at_startup_with_configuration_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    from ach_runtime.harness import boot
    from ach_runtime.observability.metrics import ANOMALIES_TOTAL

    path = tmp_path / "control.json"
    wire = _supported_config().model_dump(by_alias=True, mode="json")
    wire["infrastructure"] = {}
    wire["configVersion"] = compute_config_version(wire)
    path.write_text(json.dumps(wire))

    async def assembly(config):
        return await assemble(config, environ=_ENV, ach_transport=httpx.MockTransport(_ach_transport))

    before = ANOMALIES_TOTAL.labels(kind="configuration_invalid")._value.get()
    with pytest.raises(Exception, match="facadeEndpoint"):
        await boot.run_control(path, assembly=assembly)
    assert ANOMALIES_TOTAL.labels(kind="configuration_invalid")._value.get() == before + 1

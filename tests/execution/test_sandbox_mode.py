# SPDX-License-Identifier: Apache-2.0
"""Mini-harness sandbox mode: Ed25519 bearer, archive import, stop path, idle watchdog."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import httpx
import pytest
from aiohttp import web

from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import HookSpec, PublicEngineConfig, WorkspacePrepareRequest
from ach_agent.sandbox.tokens import claim_name, digest, engine_bearer, engine_verify_key
from tests.execution.test_http import _running_server

K = b"k" * 32
VK = engine_verify_key(K)
CLAIM_A = claim_name("bot", digest("a"))
CLAIM_B = claim_name("bot", digest("b"))


def _auth(claim: str = CLAIM_A, key: bytes = K) -> dict[str, str]:
    return {"Authorization": f"Bearer {engine_bearer(key, claim)}"}


def _tar(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class Sink:
    """aiohttp server standing in for the harness gateway's archive route."""

    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        self.runner: web.AppRunner | None = None
        self.url = ""

    async def _put(self, request: web.Request) -> web.Response:
        self.bodies.append(await request.read())
        return web.Response(status=204)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_put("/archive", self._put)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", 0).start()
        self.url = f"http://127.0.0.1:{self.runner.addresses[0][1]}/archive"

    async def stop(self) -> None:
        assert self.runner is not None
        await self.runner.cleanup()


@pytest.fixture
async def sink():
    s = Sink()
    await s.start()
    yield s
    await s.stop()


def _service(fake_driver) -> ExecutionService:
    return ExecutionService(fake_driver, {})


async def _client(url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=url, timeout=5)


async def test_bearer_pins_first_claim_and_rejects_others(fake_driver, monkeypatch) -> None:
    monkeypatch.setenv("ACH_SANDBOX_HOME", "/tmp/unused")
    app = create_execution_app(ExecutionService(None, None), verify_key=VK)
    async with _running_server(app) as url, await _client(url) as c:
        assert (await c.get("/execution/v1/health")).status_code == 200  # no auth
        health = (await c.get("/execution/v1/health")).json()
        assert health["configured"] is False and health["claim"] is None
        assert (await c.post("/execution/v1/sandbox/close")).status_code == 401
        r = await c.post("/execution/v1/sandbox/close", headers=_auth(CLAIM_A))
        assert r.status_code == 409  # authenticated, just not configured
        assert (await c.get("/execution/v1/health")).json()["claim"] == CLAIM_A
        r = await c.post("/execution/v1/sandbox/close", headers=_auth(CLAIM_B))
        assert r.status_code == 401
        r = await c.post("/execution/v1/sandbox/close", headers=_auth(CLAIM_A, key=b"x" * 32))
        assert r.status_code == 401


async def test_home_and_hydration_import_only_before_configure(
    fake_driver, tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("ACH_SANDBOX_HOME", str(home))
    service = ExecutionService(None, None)
    app = create_execution_app(service, verify_key=VK)
    async with _running_server(app) as url, await _client(url) as c:
        r = await c.put(
            "/execution/v1/sandbox/archive/home",
            content=_tar({"workspace/.ach-session-started": b""}),
            headers=_auth(),
        )
        assert r.status_code == 200
        assert (home / "workspace" / ".ach-session-started").exists()
        r = await c.put(
            "/execution/v1/sandbox/archive/hydration",
            content=_tar({"skills/x.md": b"hi"}),
            headers=_auth(),
        )
        path = Path(r.json()["path"])
        assert (path / "skills" / "x.md").read_bytes() == b"hi"
        # the extracted batch must be acceptable to the real installer
        from ach_agent.engine.context import delete_hydration_batch, install_hydration

        install_hydration(path, tmp_path / "eh", tmp_path / "eh" / "skills")
        assert (tmp_path / "eh" / "skills" / "x.md").read_bytes() == b"hi"
        delete_hydration_batch(path)
        bad = await c.put(
            "/execution/v1/sandbox/archive/home", content=b"not a tar", headers=_auth()
        )
        assert bad.status_code == 422


async def test_import_refused_once_configured(fake_driver) -> None:
    app = create_execution_app(_service(fake_driver), verify_key=VK)  # driver => configured
    async with _running_server(app) as url, await _client(url) as c:
        r = await c.put("/execution/v1/sandbox/archive/home", content=b"x", headers=_auth())
        assert r.status_code == 409


async def test_stop_path_runs_suspend_before_packing(
    fake_driver, tmp_path: Path, sink: Sink
) -> None:
    from ach_agent.engine.base.driver import EngineConfig

    home = tmp_path / "home"
    work = home / "workspace"
    service = _service(fake_driver)
    await service.claim_controller("h")
    await service.configure(
        PublicEngineConfig(
            home=str(home),
            work_dir=str(work),
            session_archive_url=sink.url,
            hook_session_suspend=HookSpec(script="touch suspended-marker"),
        )
    )
    request = WorkspacePrepareRequest(
        controller_id="h",
        invocation_id="inv",
        session_key="lane-1",
        event_id="e",
        home=str(home),
        work_dir=str(work),
        remaining_seconds=5,
    )
    await service.prepare_workspace(request)
    await service.pool.acquire(
        "lane-1", EngineConfig(model_base_url="http://127.0.0.1:9/v1", engine_type="opencode")
    )
    await service.stop_and_push()
    assert service.closing
    assert len(sink.bodies) == 1
    with tarfile.open(fileobj=io.BytesIO(sink.bodies[0])) as tar:
        assert any(m.name.endswith("suspended-marker") for m in tar.getmembers())


async def test_close_without_archive_url_pushes_nothing(fake_driver, sink: Sink) -> None:
    service = _service(fake_driver)
    app = create_execution_app(service, verify_key=VK)
    async with _running_server(app) as url, await _client(url) as c:
        r = await c.post("/execution/v1/sandbox/close", headers=_auth())
        assert r.status_code == 200
        assert (await c.get("/execution/v1/health")).json()["closing"] is True
    assert sink.bodies == []


async def test_watchdog_pushes_once_after_idle_and_never_while_busy(
    fake_driver, tmp_path: Path, sink: Sink
) -> None:
    import asyncio

    home = tmp_path / "home"
    home.mkdir()
    service = _service(fake_driver)
    await service.configure(
        PublicEngineConfig(
            home=str(home),
            work_dir=str(home / "workspace"),
            session_archive_url=sink.url,
            idle_seconds=0.2,
        )
    )
    service._workspace_reservations["x"] = object()  # type: ignore[assignment]  # busy
    task = asyncio.create_task(service.idle_watchdog(tick=0.05))
    await asyncio.sleep(0.5)
    assert sink.bodies == [] and not service.closing
    del service._workspace_reservations["x"]
    await asyncio.wait_for(task, timeout=5)
    assert len(sink.bodies) == 1 and service.closing


async def test_closing_rejects_controller_open(fake_driver) -> None:
    service = _service(fake_driver)
    app = create_execution_app(service, verify_key=VK)
    await service.stop_and_push()
    async with _running_server(app) as url, await _client(url) as c:
        r = await c.post(
            "/execution/v1/controller",
            json={"version": 1, "instance_id": service.instance_id, "controller_id": "h"},
            headers=_auth(),
        )
        assert r.status_code == 409 and r.json()["detail"] == "session closing"


async def test_run_engine_tcp_requires_verify_key(monkeypatch) -> None:
    from ach_agent.boot.roles import run_engine

    monkeypatch.setenv("ACH_ENGINE_LISTEN", "tcp")
    monkeypatch.delenv("ACH_SANDBOX_VERIFY_KEY", raising=False)
    with pytest.raises(SystemExit, match="ACH_SANDBOX_VERIFY_KEY"):
        await run_engine()


async def test_controller_stream_ends_when_service_starts_closing(fake_driver) -> None:
    import asyncio

    service = _service(fake_driver)
    app = create_execution_app(service, verify_key=VK)
    async with _running_server(app) as url, await _client(url) as c:
        async with c.stream(
            "POST",
            "/execution/v1/controller",
            json={"version": 1, "instance_id": service.instance_id, "controller_id": "h"},
            headers=_auth(),
        ) as r:
            lines = r.aiter_lines()
            await anext(lines)  # hello
            service.closing = True
            # The held stream must end by itself so the harness sees controller_lost.
            async def drain() -> None:
                async for _ in lines:
                    pass

            await asyncio.wait_for(drain(), timeout=5)


async def test_run_engine_tcp_hardens_itself_first(monkeypatch) -> None:
    from ach_agent.boot.roles import run_engine
    from ach_agent.security import preflight

    calls: list[str] = []
    monkeypatch.setattr(preflight, "harden_self", lambda: calls.append("hardened"))
    monkeypatch.setenv("ACH_ENGINE_LISTEN", "tcp")
    monkeypatch.delenv("ACH_SANDBOX_VERIFY_KEY", raising=False)
    with pytest.raises(SystemExit):
        await run_engine()
    assert calls == ["hardened"]

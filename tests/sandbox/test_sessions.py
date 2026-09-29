# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from ach_agent.boot.execution_client import ExecutionClientError
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import SandboxBlock
from ach_agent.execution.wire import PublicEngineConfig
from ach_agent.sandbox.claims import AGENT_LABEL, PERSISTENT_LABEL, SESSION_LABEL
from ach_agent.sandbox.gateway import FacadeGateway
from ach_agent.sandbox.sessions import Sandbox, SandboxSessions
from ach_agent.sandbox.tokens import claim_name, digest, engine_bearer, facade_token

K = b"k" * 32
PUBLIC = "http://bot.ach.svc:8095"


class FakeClaims:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.touched: list[str] = []
        self.ready_gate: asyncio.Event | None = None

    async def create(self, name: str, labels: dict[str, str], shutdown_at: dt.datetime) -> None:
        self.created.append(name)
        self.items[name] = {"metadata": {"name": name, "labels": labels}}

    async def get(self, name: str) -> dict[str, Any] | None:
        return self.items.get(name)

    async def wait_ready(self, name: str, *, timeout: float, interval: float = 0.5) -> str:
        if self.ready_gate is not None:
            await self.ready_gate.wait()
        return "10.0.0.7"

    async def wait_deleted(self, name: str, *, timeout: float, interval: float = 0.5) -> None:
        self.items.pop(name, None)

    async def touch(self, name: str, *, shutdown_at: dt.datetime) -> None:
        self.touched.append(name)

    async def list_agent(self, agent: str) -> list[dict[str, Any]]:
        return list(self.items.values())

    async def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.items.pop(name, None)


class FakeStore:
    def __init__(self, tmp: Path) -> None:
        self.cache_dir = tmp / "cache"
        self.cache_dir.mkdir()
        self.saved: dict[str, Path] = {}
        self.committed: dict[str, bytes] = {}
        self.fetch_gate: asyncio.Event | None = None
        self.swept = 0

    async def fetch(self, d: str) -> Path | None:
        if self.fetch_gate is not None:
            await self.fetch_gate.wait()
        return self.saved.get(d)

    async def commit(self, d: str, archive: Path) -> None:
        self.committed[d] = archive.read_bytes()

    async def sweep(self) -> None:
        self.swept += 1


class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, url: str, *, controller_id: str, auth_token: str) -> None:
        self.url, self.auth_token = url, auth_token
        self.calls: list[tuple[str, Any]] = []
        self.health: dict[str, Any] = {"configured": False, "closing": False}
        self.connect_error: Exception | None = None
        self.controller_lost = False
        FakeClient.instances.append(self)

    async def import_archive(self, path: Path, kind: str) -> str:
        self.calls.append(("import", kind))
        return f"/tmp/hyd-{kind}"

    async def connect(self, cfg: PublicEngineConfig) -> None:
        if self.connect_error:
            raise self.connect_error
        self.calls.append(("connect", cfg))

    async def sandbox_health(self) -> dict[str, Any]:
        return self.health

    async def close_session(self) -> None:
        self.calls.append(("close_session", None))

    async def close(self) -> None:
        self.calls.append(("close", None))


class Gateway(FacadeGateway):
    def __init__(self) -> None:
        super().__init__(key=K, is_live=self._live, on_archive=self._arch)
        self.allow_port(9000)

    async def _live(self, claim: str) -> bool:
        return True

    async def _arch(self, claim: str, request: web.Request) -> web.StreamResponse:
        return web.Response()


def make_sessions(
    tmp_path: Path,
    *,
    egress_url: str = "",
    hydration: Path | None = None,
    idle: float = 900,
    cfg: PublicEngineConfig | None = None,
) -> tuple[SandboxSessions, Sandbox]:
    FakeClient.instances = []
    sb = SandboxBlock.model_validate(
        {
            "enabled": True,
            "warmPool": "wp",
            "gatewayHost": "bot.ach.svc",
            "idleSeconds": idle,
            "sessions": {"bucket": "b", "maxArchiveBytes": 100},
        }
    )
    sessions = SandboxSessions(
        key=K,
        agent="bot",
        claims=FakeClaims(),  # type: ignore[arg-type]
        store=FakeStore(tmp_path),  # type: ignore[arg-type]
        gateway=Gateway(),
        sandbox=sb,
        max_invocation_seconds=600,
        hydration_archive=hydration,
        public_cfg=cfg or PublicEngineConfig(model_base_url="http://127.0.0.1:9000/v1"),
        egress_url=egress_url,
        client_factory=FakeClient,  # type: ignore[arg-type]
    )
    name = claim_name("bot", digest("s"))
    box = Sandbox(
        name, FakeClient("u", controller_id="c", auth_token="t"), facade_token(K, name), True
    )  # type: ignore[arg-type]
    return sessions, box


def _event(key: str = "lane") -> MessageEvent:
    return MessageEvent(
        idempotency_key="i", session_key=key, channel_name="ch", received_at=dt.datetime.now(dt.UTC)
    )


def _claims(s: SandboxSessions) -> FakeClaims:
    return s._claims  # type: ignore[return-value]


def _store(s: SandboxSessions) -> FakeStore:
    return s._store  # type: ignore[return-value]


async def test_sessionless_lease_creates_closes_and_deletes(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    async with s.lease(_event(), persistent=False) as box:
        client = box.client
        assert not box.persistent
        assert [c[0] for c in client.calls] == ["connect"]  # type: ignore[attr-defined]
        cfg = client.calls[0][1]  # type: ignore[attr-defined]
        assert cfg.idle_seconds == 0 and cfg.session_archive_url == ""
    assert [c[0] for c in client.calls][-2:] == ["close_session", "close"]  # type: ignore[attr-defined]
    assert _claims(s).deleted == [box.claim]
    assert _claims(s).items == {}


async def test_persistent_miss_keeps_claim_and_reuses_client(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    async with s.lease(_event(), persistent=True) as first:
        pass
    assert _claims(s).deleted == []
    _claims(s).items[first.claim]["status"] = {}
    FakeClient.instances[-1].health = {"configured": True, "closing": False}
    async with s.lease(_event(), persistent=True) as second:
        assert second.client is first.client
    assert _claims(s).created == [first.claim]


async def test_persistent_hit_imports_home_then_hydration_before_connect(tmp_path: Path) -> None:
    hyd = tmp_path / "hydration.tar.gz"
    hyd.write_bytes(b"h")
    s, _ = make_sessions(tmp_path, hydration=hyd)
    saved = tmp_path / "saved.tar.gz"
    saved.write_bytes(b"s")
    _store(s).saved[digest("lane")] = saved
    async with s.lease(_event(), persistent=True) as box:
        calls = box.client.calls  # type: ignore[attr-defined]
    assert [c[0:2] if c[0] == "import" else c[0] for c in calls] == [
        ("import", "home"),
        ("import", "hydration"),
        "connect",
    ]
    assert calls[-1][1].hydration_dir == "/tmp/hyd-hydration"
    assert box.client.auth_token == engine_bearer(K, box.claim)  # type: ignore[attr-defined]


async def test_create_and_fetch_run_concurrently(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    _claims(s).ready_gate = asyncio.Event()
    _store(s).fetch_gate = asyncio.Event()

    async def release_when_both_started() -> None:
        while not _claims(s).created:
            await asyncio.sleep(0.01)
        # create has run and is blocked on ready; fetch is blocked too => both in flight
        _claims(s).ready_gate.set()  # type: ignore[union-attr]
        _store(s).fetch_gate.set()  # type: ignore[union-attr]

    async def go() -> None:
        async with s.lease(_event(), persistent=True):
            pass

    await asyncio.wait_for(asyncio.gather(go(), release_when_both_started()), 1)


async def test_harness_restart_reconnects_without_create_or_import(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    name = claim_name("bot", digest("lane"))
    _claims(s).items[name] = {"metadata": {"name": name, "labels": {}}}
    orig = FakeClient.__init__

    def configured(self: FakeClient, *a: Any, **kw: Any) -> None:
        orig(self, *a, **kw)
        self.health = {"configured": True, "closing": False}

    FakeClient.__init__ = configured  # type: ignore[method-assign]
    try:
        s.public_cfg = s.public_cfg.model_copy(update={"egress_proxy_capability": "new-cap"})
        async with s.lease(_event(), persistent=True) as box:
            calls = box.client.calls  # type: ignore[attr-defined]
    finally:
        FakeClient.__init__ = orig  # type: ignore[method-assign]
    assert _claims(s).created == []
    assert [c[0] for c in calls] == ["connect"]
    assert calls[0][1].egress_proxy_capability == "new-cap"
    assert box.client.auth_token == engine_bearer(K, name)  # type: ignore[attr-defined]


async def test_closing_sandbox_is_waited_out_and_reopened(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    name = claim_name("bot", digest("lane"))
    _claims(s).items[name] = {"metadata": {"name": name, "labels": {}}}
    orig = FakeClient.__init__

    def closing(self: FakeClient, *a: Any, **kw: Any) -> None:
        orig(self, *a, **kw)
        if len(FakeClient.instances) == 2:
            self.health = {"configured": True, "closing": True}

    FakeClient.__init__ = closing  # type: ignore[method-assign]
    try:
        async with s.lease(_event(), persistent=True):
            pass
    finally:
        FakeClient.__init__ = orig  # type: ignore[method-assign]
    assert _claims(s).created == [name]
    assert _claims(s).deleted == []  # closing => wait for its own deletion, never delete it


async def test_unconfigured_sandbox_is_deleted_and_reopened(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    name = claim_name("bot", digest("lane"))
    _claims(s).items[name] = {"metadata": {"name": name, "labels": {}}}
    async with s.lease(_event(), persistent=True):
        pass
    assert _claims(s).deleted == [name] and _claims(s).created == [name]


async def test_connect_409_closing_takes_the_closing_branch(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    name = claim_name("bot", digest("lane"))
    _claims(s).items[name] = {"metadata": {"name": name, "labels": {}}}
    orig = FakeClient.__init__

    def first_409(self: FakeClient, *a: Any, **kw: Any) -> None:
        orig(self, *a, **kw)
        self.health = {"configured": True, "closing": False}
        if len(FakeClient.instances) == 2:
            self.connect_error = ExecutionClientError("closing", status_code=409)

    FakeClient.__init__ = first_409  # type: ignore[method-assign]
    try:
        async with s.lease(_event(), persistent=True):
            pass
    finally:
        FakeClient.__init__ = orig  # type: ignore[method-assign]
    assert _claims(s).created == [name]


async def test_open_failure_deletes_the_claim(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    orig = FakeClient.connect

    async def boom(self: FakeClient, cfg: Any) -> None:
        raise ExecutionClientError("nope")

    FakeClient.connect = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(ExecutionClientError):
            async with s.lease(_event(), persistent=False):
                pass
    finally:
        FakeClient.connect = orig  # type: ignore[method-assign]
    assert len(_claims(s).deleted) == 1


async def test_ttl_touch_before_yield(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    async with s.lease(_event(), persistent=False) as box:
        assert _claims(s).touched == [box.claim]


def test_engine_config_rewrites_loopback_only_and_pins_home(tmp_path: Path) -> None:
    s, box = make_sessions(tmp_path)
    cfg = PublicEngineConfig(
        model_base_url="http://127.0.0.1:9000/v1",
        mcp_local_urls={"a": "http://127.0.0.1:9000/mcp"},
        mcp_servers={"remote": "https://mcp.example.com/x"},
    )
    out = s.engine_config(box, cfg)
    assert out.model_base_url == f"{PUBLIC}/s/{box.facade_token}/9000/v1"
    assert out.mcp_local_urls["a"] == f"{PUBLIC}/s/{box.facade_token}/9000/mcp"
    assert out.mcp_servers["remote"] == "https://mcp.example.com/x"  # non-loopback untouched
    assert out.home == "/home/agent" and out.work_dir == "/home/agent/workspace"
    assert out.idle_seconds == 900
    assert out.session_archive_url == f"{PUBLIC}/s/{box.facade_token}/session/archive"


def test_engine_config_swaps_egress_url_keeping_capability_and_ca(tmp_path: Path) -> None:
    s, box = make_sessions(tmp_path, egress_url="http://bot.ach.svc:8096")
    out = s.engine_config(
        box,
        PublicEngineConfig(
            egress_proxy_url="http://0.0.0.0:8096",
            egress_proxy_capability="cap",
            egress_ca_cert="-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
        ),
    )
    assert out.egress_proxy_url == "http://bot.ach.svc:8096"
    assert out.egress_proxy_capability == "cap" and "abc" in out.egress_ca_cert


async def _put(s: SandboxSessions, claim: str, body: bytes) -> web.StreamResponse:
    from unittest import mock

    from aiohttp import streams

    request = make_mocked_request("PUT", "/x")
    reader = streams.StreamReader(mock.Mock(_reading_paused=False), 2**16)
    reader.feed_data(body)
    reader.feed_eof()
    request._payload = reader  # type: ignore[attr-defined]
    return await s.on_archive(claim, request)


async def test_on_archive_commits_under_digest_and_deletes_claim(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    name = claim_name("bot", digest("lane"))
    _claims(s).items[name] = {
        "metadata": {
            "name": name,
            "labels": {SESSION_LABEL: digest("lane"), PERSISTENT_LABEL: "true", AGENT_LABEL: "bot"},
        }
    }
    resp = await _put(s, name, b"tarbytes")
    assert resp.status == 204
    assert _store(s).committed == {digest("lane"): b"tarbytes"}
    assert _claims(s).deleted == [name]


async def test_on_archive_oversize_413_keeps_claim(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)  # maxArchiveBytes=100
    name = claim_name("bot", digest("lane"))
    _claims(s).items[name] = {
        "metadata": {
            "name": name,
            "labels": {SESSION_LABEL: digest("lane"), PERSISTENT_LABEL: "true"},
        }
    }
    resp = await _put(s, name, b"x" * 500)
    assert resp.status == 413
    assert _claims(s).deleted == [] and _store(s).committed == {}


async def test_on_archive_for_sessionless_claim_is_409(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    name = "ach-bot-x"
    _claims(s).items[name] = {
        "metadata": {"name": name, "labels": {SESSION_LABEL: "d", PERSISTENT_LABEL: "false"}}
    }
    assert (await _put(s, name, b"x")).status == 409


async def test_boot_lists_claims_sweeps_and_close_leaves_sandboxes(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    _claims(s).items["ach-bot-live"] = {"metadata": {"name": "ach-bot-live"}}
    await s.boot()
    assert await s.is_live("ach-bot-live") and not await s.is_live("ach-bot-gone")
    assert _store(s).swept == 1
    await s.close()
    assert _claims(s).deleted == []


def test_engine_config_owns_sandbox_persistence_and_codemem_paths(tmp_path: Path) -> None:
    s, box = make_sessions(tmp_path)
    out = s.engine_config(box, PublicEngineConfig(codemem_db_path="/harness/codemem.db"))
    assert out.persistence_enabled is True
    assert out.codemem_db_path == "/home/agent/state/codemem.db"
    assert s.engine_config(box, PublicEngineConfig()).codemem_db_path == ""


async def test_closing_sandbox_stuck_terminating_is_deleted_and_recreated(tmp_path: Path) -> None:
    s, _ = make_sessions(tmp_path)
    name = claim_name("bot", digest("lane"))
    claims = _claims(s)
    claims.items[name] = {"metadata": {"name": name, "labels": {}}}
    orig_init, orig_wait = FakeClient.__init__, FakeClaims.wait_deleted

    def closing(self: FakeClient, *a: Any, **kw: Any) -> None:
        orig_init(self, *a, **kw)
        if len(FakeClient.instances) == 2:
            self.health = {"configured": True, "closing": True}

    async def stuck(self: FakeClaims, name: str, *, timeout: float, interval: float = 0.5) -> None:
        if name not in self.deleted:
            raise TimeoutError("still present")
        self.items.pop(name, None)

    FakeClient.__init__ = closing  # type: ignore[method-assign]
    FakeClaims.wait_deleted = stuck  # type: ignore[method-assign]
    try:
        async with s.lease(_event(), persistent=True):
            pass
    finally:
        FakeClient.__init__ = orig_init  # type: ignore[method-assign]
        FakeClaims.wait_deleted = orig_wait  # type: ignore[method-assign]
    assert claims.deleted[0] == name and claims.created == [name]

# SPDX-License-Identifier: Apache-2.0
"""Stateless sandbox lease: Kubernetes (SandboxClaims) is the registry, K derives every secret.

A restarted harness re-derives each sandbox's claim name and engine bearer from K, finds the
live claim by GET, and reconnects — nothing here survives a restart, nothing needs to.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import structlog
from aiohttp import web

from ach_agent.boot.execution_client import ExecutionClient, ExecutionClientError
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import SandboxBlock
from ach_agent.execution.wire import PublicEngineConfig
from ach_agent.sandbox.archive import ArchiveTooLarge, write_capped
from ach_agent.sandbox.claims import AGENT_LABEL, PERSISTENT_LABEL, SESSION_LABEL, ClaimClient
from ach_agent.sandbox.gateway import FacadeGateway
from ach_agent.sandbox.store import SessionStore
from ach_agent.sandbox.tokens import claim_name, digest, engine_bearer, facade_token

log = structlog.get_logger(__name__)

_LOOPBACK = "127.0.0.1"


@dataclass
class Sandbox:
    claim: str
    client: ExecutionClient
    facade_token: str
    persistent: bool = False


class SandboxSessions:
    def __init__(
        self,
        *,
        key: bytes,
        agent: str,
        claims: ClaimClient,
        store: SessionStore,
        gateway: FacadeGateway,
        sandbox: SandboxBlock,
        max_invocation_seconds: float,
        hydration_archive: Path | None,
        public_cfg: PublicEngineConfig,
        egress_url: str = "",
        client_factory: Callable[..., ExecutionClient] = ExecutionClient,
    ) -> None:
        self._key = key
        self._agent = agent
        self._claims = claims
        self._store = store
        self._gateway = gateway
        self._sb = sandbox
        self._max_invocation = max_invocation_seconds
        self._hydration = hydration_archive
        # Current harness-side config; connect() always sends the CURRENT one (new egress
        # capability after a harness restart, P3).
        self.public_cfg = public_cfg
        self._egress_url = egress_url
        self._factory = client_factory
        self._live: set[str] = set()
        self._clients: dict[str, ExecutionClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._public_base = f"http://{sandbox.gateway_host}:{sandbox.gateway_port}"

    # ── registry ──────────────────────────────────────────────────────────────────────────
    async def boot(self) -> None:
        for claim in await self._claims.list_agent(self._agent):
            self._live.add(claim["metadata"]["name"])
        await self._store.sweep()

    async def is_live(self, claim: str) -> bool:
        if claim in self._live:
            return True
        if await self._claims.get(claim) is None:
            return False
        self._live.add(claim)
        return True

    async def close(self) -> None:
        """Close clients only: sandboxes keep running and push to whichever harness is up."""
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            with contextlib.suppress(Exception):
                await client.close()

    # ── archive push from a sandbox ───────────────────────────────────────────────────────
    async def on_archive(self, claim: str, request: web.Request) -> web.StreamResponse:
        """Store the bytes as-is (never extracted here), then release the claim."""
        obj = await self._claims.get(claim)
        labels = (obj or {}).get("metadata", {}).get("labels", {})
        session_digest = labels.get(SESSION_LABEL)
        if labels.get(PERSISTENT_LABEL) != "true" or not session_digest:
            return web.json_response({"detail": "not a persistent session"}, status=409)
        tmp = self._store.cache_dir / f".in-{uuid.uuid4().hex}.tar.gz"
        try:
            await write_capped(
                request.content.iter_any(), tmp, max_bytes=self._sb.sessions.max_archive_bytes
            )
        except ArchiveTooLarge:
            return web.json_response({"detail": "archive too large"}, status=413)
        await self._store.commit(session_digest, tmp)
        await self._claims.delete(claim)
        self._drop(claim)
        return web.Response(status=204)

    def _drop(self, claim: str) -> None:
        self._live.discard(claim)
        client = self._clients.pop(claim, None)
        if client is not None:
            asyncio.get_running_loop().create_task(self._quiet_close(client))

    @staticmethod
    async def _quiet_close(client: ExecutionClient) -> None:
        with contextlib.suppress(Exception):
            await client.close()

    # ── config sent to a sandbox ──────────────────────────────────────────────────────────
    def engine_config(self, box: Sandbox, cfg: PublicEngineConfig) -> PublicEngineConfig:
        def rewrite(url: str) -> str:
            # Non-loopback URLs carry no credential (invariant) and pass through unchanged.
            if urlsplit(url).hostname != _LOOPBACK:
                return url
            return self._gateway.rewrite(url, box.facade_token, public_base=self._public_base)

        update: dict[str, object] = {
            "model_base_url": rewrite(cfg.model_base_url) if cfg.model_base_url else "",
            "mcp_local_urls": {k: rewrite(v) for k, v in cfg.mcp_local_urls.items()},
            "mcp_servers": {k: rewrite(v) for k, v in cfg.mcp_servers.items()},
            "home": self._sb.home,
            "work_dir": f"{self._sb.home}/workspace",
            "idle_seconds": self._sb.idle_seconds if box.persistent else 0,
            "session_archive_url": (
                f"{self._public_base}/s/{box.facade_token}/session/archive"
                if box.persistent
                else ""
            ),
        }
        if cfg.egress_proxy_url and self._egress_url:
            update["egress_proxy_url"] = self._egress_url
        return cfg.model_copy(update=update)

    # ── lease ─────────────────────────────────────────────────────────────────────────────
    def _box(self, name: str, ip: str, persistent: bool) -> Sandbox:
        client = self._factory(
            f"http://{ip}:{self._sb.engine_port}",
            controller_id=f"harness-{name}",
            auth_token=engine_bearer(self._key, name),
        )
        return Sandbox(name, client, facade_token(self._key, name), persistent)

    @contextlib.asynccontextmanager
    async def lease(self, event: MessageEvent, *, persistent: bool) -> AsyncIterator[Sandbox]:
        session_digest = digest(event.session_key if persistent else uuid.uuid4().hex)
        name = claim_name(self._agent, session_digest)
        async with self._locks.setdefault(name, asyncio.Lock()):
            box = await self._open(name, session_digest, persistent)
            try:
                await self._claims.touch(
                    name,
                    shutdown_at=dt.datetime.now(dt.UTC)
                    + dt.timedelta(seconds=self._sb.idle_seconds + self._max_invocation + 300),
                )
                yield box
            finally:
                if not persistent:
                    await self._end_sessionless(box)

    async def _open(self, name: str, session_digest: str, persistent: bool) -> Sandbox:
        if persistent and await self._claims.get(name) is not None:
            box = await self._reuse(name)
            if box is not None:
                return box
        return await self._create(name, session_digest, persistent)

    async def _reuse(self, name: str) -> Sandbox | None:
        """Reconnect to a live claim, or return None once it has been cleared for recreation."""
        cached = self._clients.get(name)
        if cached is not None and not cached.controller_lost:
            return Sandbox(name, cached, facade_token(self._key, name), True)
        ip = await self._claims.wait_ready(name, timeout=self._sb.ready_timeout_seconds)
        box = self._box(name, ip, True)
        try:
            health = await box.client.sandbox_health()
            if health.get("closing"):
                raise _Closing
            if not health.get("configured"):
                await box.client.close()
                await self._claims.delete(name)
                self._live.discard(name)
                await self._claims.wait_deleted(name, timeout=self._sb.ready_timeout_seconds)
                return None
            await box.client.connect(self.engine_config(box, self.public_cfg))
        except _Closing:
            pass
        except ExecutionClientError as exc:
            if exc.status_code != 409:
                await box.client.close()
                raise
        else:
            self._clients[name] = box.client
            self._live.add(name)
            return box
        await box.client.close()
        await self._claims.wait_deleted(name, timeout=self._sb.ready_timeout_seconds)
        self._live.discard(name)
        return None

    async def _create(self, name: str, session_digest: str, persistent: bool) -> Sandbox:
        labels = {
            AGENT_LABEL: self._agent,
            SESSION_LABEL: session_digest,
            PERSISTENT_LABEL: "true" if persistent else "false",
        }
        shutdown = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=self._sb.ready_timeout_seconds + self._max_invocation + 300
        )

        async def provision() -> str:
            await self._claims.create(name, labels, shutdown)
            return await self._claims.wait_ready(name, timeout=self._sb.ready_timeout_seconds)

        async def restore() -> Path | None:
            return await self._store.fetch(session_digest) if persistent else None

        try:
            ip, saved = await asyncio.gather(provision(), restore())
        except BaseException:
            await self._claims.delete(name)
            raise
        box = self._box(name, ip, persistent)
        try:
            if saved is not None:
                await box.client.import_archive(saved, "home")
            cfg = self.public_cfg
            if self._hydration is not None:
                path = await box.client.import_archive(self._hydration, "hydration")
                cfg = cfg.model_copy(update={"hydration_dir": path})
            await box.client.connect(self.engine_config(box, cfg))
        except BaseException:
            with contextlib.suppress(Exception):
                await box.client.close()
            await self._claims.delete(name)
            raise
        self._live.add(name)
        if persistent:
            self._clients[name] = box.client
        return box

    async def _end_sessionless(self, box: Sandbox) -> None:
        with contextlib.suppress(Exception):
            await box.client.close_session()
        with contextlib.suppress(Exception):
            await box.client.close()
        with contextlib.suppress(Exception):
            await self._claims.delete(box.claim)
        self._live.discard(box.claim)


class _Closing(Exception):
    pass

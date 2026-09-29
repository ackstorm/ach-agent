# SPDX-License-Identifier: Apache-2.0
"""Embedded mitmproxy lifecycle (design §6, §8).

Findings encoded here (re-verify against the pinned version when bumping it):
  1. DumpMaster's default ErrorCheck calls sys.exit(1) on a startup-time ERROR log.
     Inside a Task, SystemExit escapes the EVENT LOOP (not the awaiter) — verified against
     CPython 3.13: asyncio's Task.__step re-raises BaseExceptions straight out of the
     loop, killing asyncio.run(). The catch must be INSIDE the task's own coroutine
     (_run), wrapping master.run() — design §6 is wrong on this; this module corrects it.
  2. Master.run() swaps the loop's exception handler AND installs asyncio's eager task
     factory for its whole lifetime — i.e. for every other harness task too. Eager
     scheduling broke CompletionRegistry.submit (KeyError), so start() restores H's task
     factory once the listener is up. The exception handler swap stays: nothing in H may
     rely on its own loop exception handler while the proxy runs.
  3. proxyauth (built-in) gates the local capability; set via options.update() after
     DumpMaster construction — the key doesn't exist before addon registration.
  4. allow_hosts (anchored "^host:port$" regex per declared service) scopes TLS
     interception to declared origins only — every other host is passed through as an
     opaque tunnel, never decrypted, never touched by the addon (design §1 scope).
  5. confdir -> private 0700 tempdir: the CA private key must never sit in a HOME the
     engine can read (standalone mode shares filesystem with E).
  6. proxyserver.servers entries expose listen_addrs as a tuple property, not a method
     (verified against 12.2.3 — not what the design doc's spike assumed).
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

import structlog
from mitmproxy import options
from mitmproxy.tools import dump

from ach_agent.egress.addon import EgressAddon
from ach_agent.egress.policy import ResolvedService

log = structlog.get_logger(__name__)

_CAPABILITY_USER = "ach-egress"


class EgressStartupError(Exception):
    """design §8: 'Startup fails; no native turn starts.'"""


def _allow_hosts_patterns(services: Sequence[ResolvedService]) -> list[str]:
    return [f"^{re.escape(s.host)}:{s.port}$" for s in services]


class EgressProxy:
    def __init__(
        self,
        services: Sequence[ResolvedService],
        on_failure: Callable[[], None] | None = None,
    ) -> None:
        self._services = services
        self._on_failure = on_failure
        self._master: dump.DumpMaster | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.confdir: str | None = None

    async def _run(self) -> None:
        assert self._master is not None
        try:
            await self._master.run()
        except SystemExit as exc:  # finding 1 — must be caught HERE, not at the awaiter
            raise EgressStartupError(f"embedded proxy exited: {exc.code}") from exc
        finally:
            if not self._stopping and self._on_failure is not None:
                log.error("egress: proxy task ended unexpectedly")
                self._on_failure()

    async def start(self) -> tuple[str, str, str]:
        """Returns (loopback endpoint, proxy capability, public CA cert PEM)."""
        self.confdir = tempfile.mkdtemp(prefix="ach-egress-")  # mkdtemp is 0700
        opts = options.Options(
            listen_host="127.0.0.1",
            listen_port=0,
            confdir=self.confdir,
            allow_hosts=_allow_hosts_patterns(self._services),
        )
        self._master = dump.DumpMaster(opts, with_termlog=False, with_dumper=False)
        self._master.addons.add(EgressAddon(self._services))
        capability = secrets.token_urlsafe(32)
        self._master.options.update(proxyauth=f"{_CAPABILITY_USER}:{capability}")
        loop = asyncio.get_running_loop()
        harness_task_factory = loop.get_task_factory()
        self._task = asyncio.create_task(self._run())
        try:
            port = await self._await_listener()
        except EgressStartupError:
            await self.stop()
            raise
        # Finding 2: undo run()'s eager task factory for the rest of H (mitmproxy's own
        # finally restores this same value on shutdown). Its tasks run lazily too — the
        # gh smoke test and tests/egress cover that.
        loop.set_task_factory(harness_task_factory)
        ca_pem = (Path(self.confdir) / "mitmproxy-ca-cert.pem").read_text()
        log.info("egress: proxy started", port=port, service_count=len(self._services))
        return f"http://127.0.0.1:{port}", capability, ca_pem

    async def _await_listener(self) -> int:
        assert self._task is not None
        for _ in range(250):  # bounded: ~5s, then fail loud (CLAUDE.md, LocalMcpHost shape)
            if self._task.done():
                exc = self._task.exception()
                raise EgressStartupError(f"proxy exited before listening: {exc}") from exc
            port = self._listening_port()
            if port:
                return port
            await asyncio.sleep(0.02)
        raise EgressStartupError("proxy listener not ready within 5s")

    def _listening_port(self) -> int | None:
        assert self._master is not None
        proxyserver = self._master.addons.get("proxyserver")
        if proxyserver is None:
            return None
        servers = getattr(proxyserver, "servers", None)
        if not servers:
            return None
        for server in servers:
            addrs = getattr(server, "listen_addrs", None)
            if addrs:
                return addrs[0][1]
        return None

    async def stop(self) -> None:
        self._stopping = True
        if self._master is not None:
            self._master.shutdown()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, Exception):
                log.warning("egress: proxy did not shut down cleanly")
        if self.confdir is not None:
            shutil.rmtree(self.confdir, ignore_errors=True)  # removes CA private key
        self._master = self._task = self.confdir = None

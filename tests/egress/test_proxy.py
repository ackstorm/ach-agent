from __future__ import annotations

import asyncio
import os
import stat

import pytest

from ach_agent.egress.policy import ResolvedService
from ach_agent.egress.proxy import EgressProxy, EgressStartupError


def _service() -> ResolvedService:
    return ResolvedService(
        name="github",
        host="api.github.com",
        port=443,
        header="Authorization",
        prefix="Bearer ",
        secret="s3cr3t",
        placeholder_env="GH_TOKEN",
    )


async def test_start_returns_connectable_loopback_endpoint_capability_and_ca() -> None:
    proxy = EgressProxy([_service()])
    try:
        endpoint, capability, ca_pem = await proxy.start()
        assert endpoint.startswith("http://127.0.0.1:")
        port = int(endpoint.rsplit(":", 1)[1])
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        assert len(capability) >= 32
        assert ca_pem.startswith("-----BEGIN CERTIFICATE-----")
        assert "PRIVATE KEY" not in ca_pem
    finally:
        await proxy.stop()


async def test_confdir_is_private_and_removed_on_stop() -> None:
    proxy = EgressProxy([_service()])
    await proxy.start()
    confdir = proxy.confdir
    assert confdir is not None
    # mkdtemp() under tempfile.gettempdir() is 0700 by construction — the actual
    # security property (unreadable to another principal). A "not under $HOME" check
    # is environment-fragile (some sandboxes redirect TMPDIR under $HOME) and doesn't
    # test anything the 0700 bit doesn't already guarantee.
    assert stat.S_IMODE(os.stat(confdir).st_mode) == 0o700
    await proxy.stop()
    assert not os.path.exists(confdir)


async def test_host_loop_keeps_running_during_proxy_lifetime() -> None:
    proxy = EgressProxy([_service()])
    ticks = 0
    try:
        await proxy.start()
        for _ in range(5):
            ticks += 1
            await asyncio.sleep(0.02)
    finally:
        await proxy.stop()
    assert ticks == 5


async def test_stop_is_idempotent() -> None:
    proxy = EgressProxy([_service()])
    await proxy.start()
    await proxy.stop()
    await proxy.stop()


async def test_systemexit_from_master_run_becomes_startup_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ErrorCheck's sys.exit(1) inside run() must surface as EgressStartupError and must
    NOT escape the event loop (which would kill this test process — the regression the
    test exists to catch). Patches only run(); the real DumpMaster is constructed."""
    from mitmproxy.tools import dump

    async def _exit(self: object) -> None:
        raise SystemExit(1)

    monkeypatch.setattr(dump.DumpMaster, "run", _exit)
    proxy = EgressProxy([_service()])
    with pytest.raises(EgressStartupError):
        await proxy.start()
    await proxy.stop()


async def test_proxy_death_after_start_invokes_on_failure() -> None:
    """design §8: proxy task failure → agent unready. start() takes an on_failure
    callback; Task 8 wires it to the harness's fatal path."""
    failed = asyncio.Event()
    proxy = EgressProxy([_service()], on_failure=failed.set)
    await proxy.start()
    assert proxy._master is not None
    proxy._master.shutdown()  # simulate unexpected exit: run() returns without stop()
    await asyncio.wait_for(failed.wait(), timeout=5)
    await proxy.stop()


async def test_harness_task_factory_restored_while_proxy_runs() -> None:
    """Master.run() installs asyncio.eager_task_factory for its lifetime; the harness
    code isn't written for eager scheduling (CompletionRegistry.submit KeyError'd), so
    start() puts the previous factory back once the listener is up."""
    loop = asyncio.get_running_loop()
    before = loop.get_task_factory()
    proxy = EgressProxy([_service()])
    try:
        await proxy.start()
        assert loop.get_task_factory() is before
    finally:
        await proxy.stop()
    assert loop.get_task_factory() is before

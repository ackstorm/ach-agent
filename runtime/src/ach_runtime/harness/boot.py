# SPDX-License-Identifier: MIT
"""Production control-role entry point with two ordinary uvicorn listeners."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ach_runtime.config.schema import RuntimeConfig, parse_config
from ach_runtime.harness.app import create_app
from ach_runtime.harness.control import assemble, create_facade_app
from ach_runtime.harness.hosted import create_host_app
from ach_runtime.harness.tokens import host_token
from ach_runtime.observability.runs import configure_logging

log = logging.getLogger(__name__)


async def _stop_channel_sources(sources: list[Any]) -> None:
    for source in reversed(sources):
        try:
            await source.stop()
        except Exception:  # noqa: BLE001
            log.warning("control channel source stop failed", exc_info=True)


async def _start_channel_sources(config: RuntimeConfig, control: Any) -> list[Any]:
    """Start one cron scheduler and each configured Redis consumer after discovery."""
    sources: list[Any] = []
    cron_channels = [channel for channel in config.channels if channel.type == "cron"]
    queue_channels = [channel for channel in config.channels if channel.type == "queue"]
    try:
        if cron_channels:
            from ach_runtime.channels.cron import CronScheduler

            scheduler = CronScheduler(cron_channels, control)
            sources.append(scheduler)
            await scheduler.start()
        if queue_channels:
            from ach_runtime.channels.queue import QueueConsumer

            for channel in queue_channels:
                consumer = QueueConsumer(channel, control)
                sources.append(consumer)
                await consumer.start()
    except BaseException:
        await _stop_channel_sources(sources)
        raise
    return sources


async def run_control(
    config_path: Path,
    *,
    host: str = "0.0.0.0",
    ingress_port: int = 8080,
    facade_port: int = 8081,
    facade_host: str | None = None,
    host_api: bool = True,
    assembly: Any = assemble,
) -> None:
    configure_logging()
    try:
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("cannot read control config") from exc
        config: RuntimeConfig = parse_config(raw)
        control = await assembly(config)
    except Exception:
        from ach_runtime.observability import alarm

        alarm("configuration_invalid")
        log.error("control configuration or hydration failed")
        raise
    ingress = create_app(control)
    facades = create_facade_app(control)
    # The host reaches /host/v1 on the 8081 listener before readiness: adoption waits for its inventory.
    if host_api:
        facades.mount("/host/v1", create_host_app(control.source, host_token(control.key, config.agent.uid)))

    import uvicorn

    class ManagedServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            # Uvicorn's serve loop uses this context manager. Keep signal ownership in
            # the control runner so listener shutdown preserves the facade drain.
            yield

    shutdown_timeout = config.workspace.shutdown_timeout_seconds
    servers = [
        ManagedServer(uvicorn.Config(
            ingress, host=host, port=ingress_port, lifespan="on",
            timeout_graceful_shutdown=shutdown_timeout,
        )),
        ManagedServer(uvicorn.Config(
            facades, host=facade_host or host, port=facade_port, lifespan="on",
            timeout_graceful_shutdown=shutdown_timeout,
            # Facade URLs carry the Workspace HMAC; the access log would publish it.
            access_log=False,
        )),
    ]
    tasks: list[asyncio.Task[None]] = []
    discovery: asyncio.Task[list[Any]] | None = None
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    shutdown_requested = asyncio.Event()
    sources: list[Any] = list(getattr(ingress.state, "a2a_bridges", {}).values())

    def request_shutdown() -> None:
        if ingress.state.ready:
            shutdown_requested.set()
        else:
            control.source.close()  # pending long-polls answer 503
            for server in servers:
                server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        tasks = [asyncio.create_task(server.serve()) for server in servers]
        async with asyncio.timeout(config.engine.startup_timeout_seconds):
            while not all(server.started for server in servers):
                if any(task.done() for task in tasks):
                    for task in tasks:
                        if task.done() and not task.cancelled() and task.exception() is not None:
                            raise task.exception()
                    raise RuntimeError("a control listener stopped before startup completed")
                await asyncio.sleep(0.02)
        # Both HTTP applications, including the facade child lifespans and /host/v1, are
        # serving before adoption, which waits for the host's inventory with no deadline.
        discovery = asyncio.create_task(control.manager.adopt())
        while not discovery.done():
            done, _ = await asyncio.wait({discovery, *tasks}, return_when=asyncio.FIRST_COMPLETED)
            if discovery in done:
                break
            for task in done:
                if task.cancelled():
                    raise RuntimeError("a control listener stopped during Workspace discovery")
                error = task.exception()
                if error is not None:
                    raise error
                raise RuntimeError("a control listener stopped during Workspace discovery")
        await discovery
        await control.lifecycle.start()
        sources.extend(await _start_channel_sources(config, control))
        ingress.state.ready = True
        shutdown_task = asyncio.create_task(shutdown_requested.wait())
        done, _ = await asyncio.wait([*tasks, shutdown_task], return_when=asyncio.FIRST_COMPLETED)
        if shutdown_task in done:
            # Stop new ingress first. Keep the facade server alive while admitted runners
            # settle so running mini processes can finish model/MCP calls.
            ingress.state.ready = False
            control._stopping = True
            control.source.close()
            await _stop_channel_sources(sources)
            sources.clear()
            servers[0].should_exit = True
            await tasks[0]
            drain_deadline = loop.time() + shutdown_timeout
            while control.admission.queued_total and loop.time() < drain_deadline:
                await asyncio.sleep(0.05)
            if control.admission.queued_total:
                log.warning("control drain reached configured shutdown timeout")
            servers[1].should_exit = True
            await tasks[1]
            shutdown_task.cancel()
            await asyncio.gather(shutdown_task, return_exceptions=True)
        else:
            shutdown_task.cancel()
            await asyncio.gather(shutdown_task, return_exceptions=True)
            for task in done:
                if not task.cancelled() and task.exception() is not None:
                    raise task.exception()
            raise RuntimeError("a control listener stopped unexpectedly")
    finally:
        ingress.state.ready = False
        control._stopping = True
        await _stop_channel_sources(sources)
        sources.clear()
        if discovery is not None and not discovery.done():
            discovery.cancel()
            await asyncio.gather(discovery, return_exceptions=True)
        for server in servers:
            server.should_exit = True
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for sig in installed:
            loop.remove_signal_handler(sig)
        await control.close()

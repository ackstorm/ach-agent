# SPDX-License-Identifier: Apache-2.0
"""Split-role configuration and engine-role bootstrap.

The harness owns the full :class:`AgentConfig`.  This module is deliberately the
small boundary used to derive the channels projection and the credential-free
engine bootstrap.  It does not start inbound channels or the harness router;
those orchestration paths remain in the local/deployment launcher.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import re
import shutil
import signal
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

import uvicorn
from pydantic import JsonValue

from ach_agent.boot.bootstrap import (
    DEFAULT_BOOTSTRAP_WAIT_SECONDS,
    DEFAULT_CHANNELS_HOST,
    DEFAULT_CHANNELS_PORT,
    DEFAULT_ENGINE_HOST,
    DEFAULT_ENGINE_PORT,
    ChannelsBootstrap,
    role_bootstrap_path,
    wait_for_channels_bootstrap,
    wait_for_engine_bootstrap,
)
from ach_agent.boot.ipc import bind_listener, engine_socket_path
from ach_agent.boot.paths import harness_log_dir, resolve_role_paths
from ach_agent.boot.secrets import collect_secret_env_names, strip_forwarded_secrets
from ach_agent.config.schema import (
    AgentConfig,
    ChannelSourceConfig,
    CodememMemory,
    LocalMcpServer,
    RemoteMcpServer,
)
from ach_agent.engine.base.driver import EngineDriver
from ach_agent.engine.context import link_public_context
from ach_agent.engine.opencode.driver import OpencodeDriver
from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.state import NativeSessionStore
from ach_agent.execution.wire import PublicEngineConfig


class SplitRoleConfigError(ValueError):
    """A full harness config cannot be safely projected to a split role."""


_MCP_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_MANAGED_ENV_NAMES = frozenset(
    {
        "ACH_TOKEN",
        "ACH_API_KEY",
        "ACH_MODEL_TOKEN",
        "ACH_CHANNELS_HMAC_KEY",
        "ACH_HARNESS_URL",
        "ACH_ENGINE_URL",
        "ACH_MODEL_BASE_URL",
        "ACH_MODEL_HEADER",
    }
)


def _json_model(value: Any) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue], value.model_dump(mode="json", by_alias=True, exclude_none=True)
    )


def _source_projection(cfg: AgentConfig) -> list[dict[str, JsonValue]]:
    projected: list[dict[str, JsonValue]] = []
    for channel in cfg.channels:
        # Construct from an allowlist rather than dumping and excluding private fields;
        # a newly added harness field must never silently cross the role boundary.
        raw = {
            "name": channel.name,
            "type": channel.type,
            "concurrency": channel.concurrency,
            "source": channel.source,
            "webhook": channel.webhook,
            "cron": channel.cron,
            "queue": channel.queue,
            "a2a": channel.a2a,
        }
        source = ChannelSourceConfig.model_validate(raw)
        projected.append(_json_model(source))
    return projected


def _codemem_bootstrap(cfg: AgentConfig) -> tuple[str, str]:
    """Return codemem path/project without probing the E image from H."""
    memory = cfg.memory
    if not isinstance(memory, CodememMemory):
        return "", ""
    params = memory.codemem
    if params.db_path:
        db_path = params.db_path
    elif cfg.persistence.enabled:
        db_path = str(Path(cfg.persistence.mount_path) / "state" / "codemem.db")
    else:
        # Keep the pre-split volatile location stable even when an operator
        # chooses a custom native engine home.
        db_path = "/tmp/ach-home/state/codemem.db"
    return db_path, params.project


def _mcp_engine_env_names(cfg: AgentConfig) -> set[str]:
    names: set[str] = set()
    for spec in cfg.mcp_servers.values():
        if isinstance(spec, LocalMcpServer):
            names.update(spec.env)
        elif isinstance(spec, RemoteMcpServer):
            for value in spec.headers.values():
                names.update(_MCP_ENV_REF.findall(value))
    return names - _MANAGED_ENV_NAMES


def _engine_env_names(cfg: AgentConfig) -> list[str]:
    """Return the sanitized names allowed in the engine-role environment contract."""
    secret_names = set(collect_secret_env_names(cfg))
    names = [
        name
        for name in strip_forwarded_secrets(cfg)
        if name not in _MANAGED_ENV_NAMES and name not in secret_names
    ]
    names.extend(name for name in sorted(_mcp_engine_env_names(cfg)) if name not in secret_names)
    return list(dict.fromkeys(names))


def _open_session_store(public: PublicEngineConfig, home: Path) -> MutableMapping[str, str]:
    """Select the engine-owned persistent map or the volatile boot map."""
    if public.persistence_enabled:
        return NativeSessionStore(home)
    from ach_agent.engine.base.pool import _LRUSessionMap

    return _LRUSessionMap()


def build_role_configs(
    cfg: AgentConfig, *, split_mode: bool = True
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Build the channels projection and public engine bootstrap.

    ``engine.forwardEnv`` selects names for the public engine bootstrap in both local
    and split mode.  The engine role reads each selected value from its own process
    environment; no value is serialized into this projection.
    """
    engine_env_names = _engine_env_names(cfg)
    engine_env = {
        name: os.environ[name] for name in engine_env_names if name in os.environ
    }
    paths = resolve_role_paths(cfg)
    channels: dict[str, JsonValue] = {
        "schemaVersion": "1",
        "channels": cast(JsonValue, _source_projection(cfg)),
    }
    codemem_db_path, codemem_project = _codemem_bootstrap(cfg)
    templates = {
        name: spec
        for name, spec in cfg.mcp_servers.items()
        if isinstance(spec, (LocalMcpServer, RemoteMcpServer))
    }
    public = PublicEngineConfig(
        agent_name=cfg.agent.name,
        engine_type=cfg.engine.type,
        binary_path=(
            cfg.engine.pi.binary_path
            if cfg.engine.type == "pi" and cfg.engine.pi
            else ("pi" if cfg.engine.type == "pi" else "opencode")
        ),
        home=str(paths.engine_home),
        work_dir=str(paths.work_dir),
        persistence_enabled=cfg.persistence.enabled,
        persistence_mount_path=cfg.persistence.mount_path,
        public_context=str(paths.public_context),
        engine_env=engine_env,
        model=cfg.model.name,
        model_type=cfg.model.type,
        params=cfg.model.params,
        thinking_enabled=cfg.model.thinking.enabled,
        thinking_effort=cfg.model.thinking.effort,
        steps=cfg.limits.max_steps,
        startup_timeout_seconds=cfg.engine.startup_timeout_seconds,
        mcp_templates=templates,
        exclude_tools=cfg.capability.filter.exclude.tools,
        codemem_db_path=codemem_db_path,
        codemem_project=codemem_project,
        pi_mcp_adapter_path=cfg.engine.pi.mcp_adapter_path
        if cfg.engine.type == "pi" and cfg.engine.pi
        else "",
    )
    return channels, _json_model(public)


async def run_engine(
    public_config: JsonValue | None = None, *, terminal_mode: bool = False
) -> None:
    """Start the engine HTTP role with no native process at endpoint boot."""
    if public_config is None and not terminal_mode:
        service = ExecutionService(None, None)
        app = create_execution_app(service)
        listener = bind_listener(engine_socket_path())
        server = uvicorn.Server(
            uvicorn.Config(app=app, host=DEFAULT_ENGINE_HOST, port=DEFAULT_ENGINE_PORT, log_level="warning")
        )

        async def stop_on_shutdown() -> None:
            while not service.shutdown_requested and not server.should_exit:
                await asyncio.sleep(0.05)
            with contextlib.suppress(Exception):
                await service.release_controller(service.controller_id or "")
            server.should_exit = True

        watcher = asyncio.create_task(stop_on_shutdown())
        try:
            await server.serve(sockets=[listener])
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            with contextlib.suppress(Exception):
                await service.release_controller(service.controller_id or "")
            listener.close()
            engine_socket_path().unlink(missing_ok=True)
        return
    if public_config is None:
        public_config = await wait_for_engine_bootstrap(
            role_bootstrap_path("engine"),
            timeout=_bootstrap_wait_seconds(),
        )
    public = PublicEngineConfig.model_validate(public_config)
    home = Path(public.home or "/tmp/ach-home")
    work_dir = Path(public.work_dir or home / "workspace")
    public_context = Path(public.public_context or "/tmp/ach-public-context")
    from ach_agent import identity

    identity.configure(public.agent_name, os.environ.get("ACH_ENVIRONMENT", ""))
    # E creates only its private home/workspace.  Public context may be a late-mounted
    # read-only volume hydrated by H, so endpoint boot never creates H-owned paths.
    home.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    link_public_context(home, public_context, work_dir=work_dir, create_public=False)
    store = _open_session_store(public, home)
    if public.engine_type == "pi":
        from ach_agent.engine.pi.driver import PiDriver

        driver: EngineDriver = PiDriver()
    else:
        driver = OpencodeDriver()

    if terminal_mode:
        from ach_agent.engine import trace
        from ach_agent.execution.service import _engine_config

        try:
            native_cfg = _engine_config(public)
            from ach_agent.channels.tui import _CONSOLE_SESSION_KEY

            token = public.trace_token
            if not token:
                raise SplitRoleConfigError("traceToken is required for native terminal mode")
            if not public.trace_parent or not public.trace_session_id:
                raise SplitRoleConfigError(
                    "traceParent and traceSessionId are required for native terminal mode"
                )
            trace.adopt(token)
            trace.adopt_tui(
                token,
                traceparent=public.trace_parent,
                session_id=public.trace_session_id,
            )
            native_cfg.model_base_url = trace.tokenize_url(native_cfg.model_base_url, token)
            native_cfg.mcp_local_urls = {
                name: trace.tokenize_url(url, token)
                for name, url in native_cfg.mcp_local_urls.items()
            }
            if public.engine_type == "pi":
                from ach_agent.engine.pi.driver import PiDriver

                await PiDriver().run_tui(native_cfg, _CONSOLE_SESSION_KEY)
                return
            from ach_agent.engine.lifecycle import build_opencode_env

            server_native = await driver.launch(native_cfg, _CONSOLE_SESSION_KEY)
            try:
                binary = shutil.which(native_cfg.binary_path)
                if binary is None:
                    raise RuntimeError(f"engine binary not found: {native_cfg.binary_path}")
                config_path = server_native.config_path
                env = (
                    build_opencode_env(server_native.ephemeral_home, native_cfg, config_path)
                    if config_path
                    else {}
                )
                log_path = harness_log_dir() / "tui-attach.log"
                previous_sigint = signal.getsignal(signal.SIGINT)
                proc: asyncio.subprocess.Process | None = None
                with log_path.open("a", encoding="utf-8") as log_file:
                    try:
                        # Keep H/E Python logs off the alternate screen while the
                        # attach process inherits the real terminal descriptors.
                        real_stderr = sys.stderr
                        sys.stderr = log_file
                        try:
                            proc = await asyncio.create_subprocess_exec(
                                binary,
                                "attach",
                                f"http://127.0.0.1:{server_native.port}",
                                "--pure",
                                env=env,
                            )
                            signal.signal(signal.SIGINT, signal.SIG_IGN)
                            await proc.wait()
                        finally:
                            sys.stderr = real_stderr
                    except asyncio.CancelledError:
                        if proc is not None and proc.returncode is None:
                            proc.terminate()
                            with contextlib.suppress(asyncio.TimeoutError):
                                await asyncio.wait_for(proc.wait(), timeout=5.0)
                            if proc.returncode is None:
                                proc.kill()
                                await proc.wait()
                        raise
                    finally:
                        signal.signal(signal.SIGINT, previous_sigint)
            finally:
                await driver.stop(server_native)
        finally:
            close = getattr(store, "close", None)
            if close is not None:
                close()
        return
    service = ExecutionService(driver, store)
    app = create_execution_app(service)
    socket_name = os.environ.get("ACH_ENGINE_SOCKET", "").strip()
    host = os.environ.get("ACH_ENGINE_HOST", DEFAULT_ENGINE_HOST)
    try:
        port = int(os.environ.get("ACH_ENGINE_PORT", str(DEFAULT_ENGINE_PORT)))
    except ValueError as exc:
        close = getattr(store, "close", None)
        if close is not None:
            close()
        raise SplitRoleConfigError("ACH_ENGINE_PORT must be an integer") from exc
    listener = bind_listener(Path(socket_name)) if socket_name else None
    server = uvicorn.Server(
        uvicorn.Config(
            app=app,
            host=host,
            port=port,
            log_level="warning",
        )
    )

    async def stop_on_shutdown() -> None:
        # Uvicorn waits for an open streaming response during graceful shutdown.
        # Release the controller first for both an unhealthy service and an
        # ordinary SIGTERM/should_exit request, so the held NDJSON response can
        # finish and uvicorn can actually return from serve().
        while not service.shutdown_requested and not server.should_exit:
            await asyncio.sleep(0.05)
        try:
            await service.release_controller(service.controller_id or "")
        finally:
            server.should_exit = True

    watcher = asyncio.create_task(stop_on_shutdown())
    try:
        if listener is None:
            await server.serve()
        else:
            await server.serve(sockets=[listener])
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        with contextlib.suppress(Exception):
            await service.release_controller(service.controller_id or "")
        close = getattr(store, "close", None)
        if close is not None:
            close()
        if listener is not None:
            listener.close()
            Path(socket_name).unlink(missing_ok=True)
    if service.shutdown_requested or service.controller_cleanup_error:
        raise RuntimeError("engine role shutdown requested after unreliable cleanup")


async def run_harness(
    cfg: AgentConfig,
    *,
    tui_mode: bool = False,
    one_shot_prompt: str | None = None,
    debug_mode: bool = False,
) -> None:
    """Run the harness role while keeping the legacy local entrypoint stable."""
    # Import lazily: ``main`` imports this module while constructing the role
    # projections, and the role entrypoint must not create a second boot graph.
    from ach_agent.main import _run_harness

    await _run_harness(
        tui_mode=tui_mode,
        one_shot_prompt=one_shot_prompt,
        debug_mode=debug_mode,
        cfg=cfg,
        role_mode="harness",
    )


async def run_channels(channel_config: JsonValue | None = None) -> None:
    """Start the source role from its filtered configuration artifact."""
    bootstrap: ChannelsBootstrap | None = None
    if channel_config is None:
        channel_socket = os.environ.get("ACH_CHANNEL_SOCKET", "").strip()
        if channel_socket:
            from ach_agent.channels.client import ChannelsClient

            config_client = ChannelsClient(
                socket_path=channel_socket,
                base_url="http://ach-internal",
                key=b"",
                agent=os.environ.get("ACH_AGENT_NAME", "").strip(),
            )
            try:
                inputs = await config_client.fetch_config()
            finally:
                await config_client.close()
            channel_config = {
                "schemaVersion": "1",
                "channels": cast(
                    JsonValue,
                    [item.model_dump(mode="json", by_alias=True) for item in inputs.channels],
                ),
            }
        else:
            bootstrap = await wait_for_channels_bootstrap(
                role_bootstrap_path("channels"), timeout=_bootstrap_wait_seconds()
            )
            channel_config = {
                "schemaVersion": "1",
                "channels": cast(JsonValue, bootstrap.channels),
            }
    if not isinstance(channel_config, dict):
        raise SplitRoleConfigError("channels role requires an object configuration")
    if channel_config.get("schemaVersion") != "1":
        raise SplitRoleConfigError("invalid channels role artifact")
    sources = channel_config.get("channels")
    if not isinstance(sources, list):
        raise SplitRoleConfigError("channels role artifact must contain channels")
    raw_sources = sources
    channel_socket = os.environ.get("ACH_CHANNEL_SOCKET", "").strip()
    key_text = os.environ.get("ACH_CHANNELS_HMAC_KEY", "")
    if bootstrap is not None:
        key_text = bootstrap.hmac_key
    if not key_text and not channel_socket:
        raise SplitRoleConfigError(
            "ACH_CHANNELS_HMAC_KEY is required for a separated channels role"
        )
    harness_url = os.environ.get("ACH_HARNESS_URL", "").strip()
    if bootstrap is not None:
        harness_url = bootstrap.harness_url
    if not harness_url and not channel_socket:
        raise SplitRoleConfigError("channels bootstrap has no harness URL")
    from ach_agent import identity

    agent_name = os.environ.get("ACH_AGENT_NAME", "").strip()
    if bootstrap is not None:
        agent_name = bootstrap.agent_name
    identity.configure(agent_name, os.environ.get("ACH_ENVIRONMENT", ""))
    for source in raw_sources:
        ChannelSourceConfig.model_validate(source)
    from ach_agent.channels.a2a import A2AAgentExecutorBridge, build_a2a_app, make_a2a_agent_card
    from ach_agent.channels.client import ChannelsClient
    from ach_agent.channels.cron import CronScheduler
    from ach_agent.channels.queue import QueueConsumer
    from ach_agent.http.app import create_app

    source_configs: list[ChannelSourceConfig] = [
        ChannelSourceConfig.model_validate(source) for source in raw_sources
    ]
    if not agent_name:
        raise SplitRoleConfigError("ACH_AGENT_NAME is required for a separated channels role")
    client = ChannelsClient(
        harness_url or "http://ach-internal",
        key_text.encode(),
        agent=agent_name,
        poll_interval=2.0,
        socket_path=channel_socket or None,
    )
    probe_deadline = asyncio.get_running_loop().time() + (
        _bootstrap_wait_seconds() if bootstrap is not None else 30.0
    )
    probe_channel = source_configs[0].name if source_configs else ""
    while True:
        try:
            if await client.probe_harness(probe_channel):
                break
        except Exception:
            pass
        if asyncio.get_running_loop().time() >= probe_deadline:
            await client.close()
            raise SplitRoleConfigError("harness signed connectivity did not become ready")
        await asyncio.sleep(0.5)
    a2a_mounts: list[tuple[str, Any]] = []
    a2a_bridges: list[A2AAgentExecutorBridge] = []
    for source_cfg in source_configs:
        if source_cfg.type != "a2a":
            continue
        bridge = A2AAgentExecutorBridge(
            handler=client,
            channel_cfg=source_cfg,
            completion_port=client,
        )
        a2a_bridges.append(bridge)
        a2a_mounts.append(
            (
                f"/a2a/{source_cfg.name}",
                build_a2a_app(make_a2a_agent_card(source_cfg.name), bridge),
            )
        )
    http_sources = [
        source for source in source_configs if source.type in ("webhook", "webhook-script")
    ]
    app = create_app(http_sources, client, a2a_mounts=a2a_mounts)
    host = os.environ.get("ACH_CHANNELS_HOST", DEFAULT_CHANNELS_HOST)
    try:
        port = int(os.environ.get("ACH_CHANNELS_PORT", str(DEFAULT_CHANNELS_PORT)))
    except ValueError as exc:
        await client.close()
        raise SplitRoleConfigError("ACH_CHANNELS_PORT must be an integer") from exc
    server = uvicorn.Server(uvicorn.Config(app=app, host=host, port=port, log_level="warning"))
    cron = CronScheduler(
        [source for source in source_configs if source.type == "cron"], handler=client
    )
    queues: list[QueueConsumer] = []
    for source_cfg in source_configs:
        if source_cfg.type == "queue":
            queues.append(QueueConsumer(source_cfg, handler=client))
    try:
        await cron.start()
        for queue in queues:
            await queue.start()
        await server.serve()
    finally:
        for bridge in a2a_bridges:
            await bridge.shutdown()
        for queue in queues:
            await queue.stop()
        await cron.stop()
        await client.close()


def _bootstrap_wait_seconds() -> float:
    raw = os.environ.get("ACH_BOOTSTRAP_WAIT_SECONDS", str(DEFAULT_BOOTSTRAP_WAIT_SECONDS))
    try:
        value = float(raw)
    except ValueError as exc:
        raise SplitRoleConfigError("ACH_BOOTSTRAP_WAIT_SECONDS must be a number") from exc
    if value <= 0 or not math.isfinite(value):
        raise SplitRoleConfigError("ACH_BOOTSTRAP_WAIT_SECONDS must be positive")
    return value

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
import os
import re
import shutil
import signal
import sys
from pathlib import Path
from typing import Any, cast

import uvicorn
from pydantic import JsonValue

from ach_agent.boot.ipc import bind_listener, channel_socket_path, engine_socket_path
from ach_agent.boot.paths import harness_log_dir, resolve_role_paths
from ach_agent.boot.secrets import collect_secret_env_names, strip_forwarded_secrets
from ach_agent.config.schema import (
    AgentConfig,
    ChannelSourceConfig,
    CodememMemory,
    LocalMcpServer,
    RemoteMcpServer,
)
from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService
from ach_agent.execution.wire import PublicEngineConfig

DEFAULT_CHANNELS_HOST = "0.0.0.0"
DEFAULT_CHANNELS_PORT = 8080
DEFAULT_ENGINE_HOST = "127.0.0.1"
DEFAULT_ENGINE_PORT = 8081


class SplitRoleConfigError(ValueError):
    """A full harness config cannot be safely projected to a split role."""


async def _run_native_terminal(service: ExecutionService) -> None:
    """Run the inherited native terminal after controller-open configuration."""
    public = service.public_config
    if public is None or service.driver is None:
        raise SplitRoleConfigError("native terminal requires controller configuration")
    from ach_agent.channels.tui import _CONSOLE_SESSION_KEY
    from ach_agent.engine import trace
    from ach_agent.execution.service import _engine_config

    token = public.trace_token
    if not token or not public.trace_parent or not public.trace_session_id:
        raise SplitRoleConfigError("trace fields are required for native terminal mode")
    trace.adopt(token)
    trace.adopt_tui(token, traceparent=public.trace_parent, session_id=public.trace_session_id)
    native_cfg = _engine_config(public)
    native_cfg.model_base_url = trace.tokenize_url(native_cfg.model_base_url, token)
    native_cfg.mcp_local_urls = {
        name: trace.tokenize_url(url, token) for name, url in native_cfg.mcp_local_urls.items()
    }
    if public.engine_type == "pi":
        await service.driver.run_tui(native_cfg, _CONSOLE_SESSION_KEY)  # type: ignore[attr-defined]
        return
    from ach_agent.engine.lifecycle import build_opencode_env

    server_native = await service.driver.launch(native_cfg, _CONSOLE_SESSION_KEY)
    try:
        binary = shutil.which(native_cfg.binary_path)
        if binary is None:
            raise RuntimeError(f"engine binary not found: {native_cfg.binary_path}")
        env = (
            build_opencode_env(server_native.ephemeral_home, native_cfg, server_native.config_path)
            if server_native.config_path
            else {}
        )
        log_path = harness_log_dir() / "tui-attach.log"
        previous_sigint = signal.getsignal(signal.SIGINT)
        proc: asyncio.subprocess.Process | None = None
        with log_path.open("a", encoding="utf-8") as log_file:
            try:
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
        await service.driver.stop(server_native)


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


def build_role_configs(
    cfg: AgentConfig, *, split_mode: bool = True
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Build the channels projection and public engine bootstrap.

    ``engine.forwardEnv`` selects names for the public engine bootstrap in both local
    and split mode.  The engine role reads each selected value from its own process
    environment; no value is serialized into this projection.
    """
    engine_env_names = _engine_env_names(cfg)
    engine_env = {name: os.environ[name] for name in engine_env_names if name in os.environ}
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
    service = ExecutionService(None, None)
    if public_config is not None:
        await service.configure(PublicEngineConfig.model_validate(public_config))
    app = create_execution_app(service)
    listener = bind_listener(engine_socket_path())
    server = uvicorn.Server(
        uvicorn.Config(
            app=app,
            host=DEFAULT_ENGINE_HOST,
            port=DEFAULT_ENGINE_PORT,
            log_level="warning",
        )
    )

    async def stop_on_shutdown() -> None:
        while not service.shutdown_requested and not server.should_exit:
            await asyncio.sleep(0.05)
        with contextlib.suppress(Exception):
            await service.release_controller(service.controller_id or "")
        server.should_exit = True

    async def run_terminal() -> None:
        await service.configured_event.wait()
        native_task = asyncio.create_task(_run_native_terminal(service))
        try:
            while not native_task.done():
                if service.controller_id is None or server.should_exit:
                    native_task.cancel()
                    await asyncio.gather(native_task, return_exceptions=True)
                    return
                await asyncio.sleep(0.05)
            await native_task
        finally:
            if not native_task.done():
                native_task.cancel()
                await asyncio.gather(native_task, return_exceptions=True)
            with contextlib.suppress(Exception):
                await service.release_controller(service.controller_id or "")
            server.should_exit = True

    watcher = asyncio.create_task(run_terminal() if terminal_mode else stop_on_shutdown())
    watcher_error: BaseException | None = None
    try:
        await server.serve(sockets=[listener])
    finally:
        if not watcher.done():
            watcher.cancel()
        watcher_result = await asyncio.gather(watcher, return_exceptions=True)
        if watcher_result and isinstance(watcher_result[0], BaseException):
            result = watcher_result[0]
            if not isinstance(result, asyncio.CancelledError):
                watcher_error = result
        with contextlib.suppress(Exception):
            await service.release_controller(service.controller_id or "")
        await service.close()
        listener.close()
        engine_socket_path().unlink(missing_ok=True)
    if watcher_error is not None:
        raise watcher_error
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
    """Start source adapters from the harness-owned channel socket projection."""
    fetched_agent_name = ""
    if channel_config is None:
        from ach_agent.channels.client import ChannelsClient

        config_client = ChannelsClient(socket_path=str(channel_socket_path()))
        try:
            deadline = asyncio.get_running_loop().time() + 30.0
            while True:
                try:
                    inputs = await config_client.fetch_config()
                    break
                except Exception as exc:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise SplitRoleConfigError(
                            "channel configuration did not become available"
                        ) from exc
                    await asyncio.sleep(0.5)
        finally:
            await config_client.close()
        fetched_agent_name = inputs.agent_name
        channel_config = {
            "schemaVersion": "1",
            "channels": cast(
                JsonValue,
                [item.model_dump(mode="json", by_alias=True) for item in inputs.channels],
            ),
        }
    if not isinstance(channel_config, dict):
        raise SplitRoleConfigError("channels role requires an object configuration")
    if channel_config.get("schemaVersion") != "1":
        raise SplitRoleConfigError("invalid channels role artifact")
    sources = channel_config.get("channels")
    if not isinstance(sources, list):
        raise SplitRoleConfigError("channels role artifact must contain channels")
    raw_sources = sources
    channel_socket = str(channel_socket_path())
    from ach_agent import identity

    agent_name = os.environ.get("ACH_AGENT_NAME", "").strip()
    if fetched_agent_name:
        agent_name = fetched_agent_name
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
        agent=agent_name,
        poll_interval=2.0,
        socket_path=channel_socket or None,
    )
    probe_deadline = asyncio.get_running_loop().time() + 30.0
    probe_channel = source_configs[0].name if source_configs else ""
    while True:
        try:
            if await client.probe_harness(probe_channel):
                break
        except Exception:
            pass
        if asyncio.get_running_loop().time() >= probe_deadline:
            await client.close()
            raise SplitRoleConfigError("harness connectivity did not become ready")
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

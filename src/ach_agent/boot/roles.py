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
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any, cast

import uvicorn
from pydantic import JsonValue

from ach_agent.boot.paths import resolve_role_paths
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


def _codemem_bootstrap(cfg: AgentConfig, engine_home: Path) -> tuple[str, str]:
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
        db_path = str(engine_home / "state" / "codemem.db")
    return db_path, params.project


def build_role_configs(
    cfg: AgentConfig, *, split_mode: bool = True
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Build the channels projection and public engine bootstrap.

    ``engine.forwardEnv`` is a harness-era inheritance mechanism and is rejected in
    split mode.  Native subprocess forwarding, when intentionally configured by the
    engine role, uses ``PublicEngineConfig.engineEnvNames`` and reads values from E's
    own environment.
    """
    if split_mode and cfg.engine.forward_env:
        raise SplitRoleConfigError(
            "engine.forwardEnv is not supported in split mode; configure explicit engine env names"
        )
    engine_env_names: list[str] = []
    if not split_mode and cfg.engine.forward_env:
        # Native local mode retains the established clean-slate forwarding policy;
        # secret names are removed by the existing fail-safe helper before they
        # reach the child environment.
        from ach_agent.boot.secrets import strip_forwarded_secrets

        engine_env_names = strip_forwarded_secrets(cfg)
    paths = resolve_role_paths(cfg)
    channels: dict[str, JsonValue] = {
        "schemaVersion": "1",
        "channels": cast(JsonValue, _source_projection(cfg)),
    }
    codemem_db_path, codemem_project = _codemem_bootstrap(cfg, paths.engine_home)
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
        engine_env_names=engine_env_names,
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


async def run_engine(public_config: JsonValue) -> None:
    """Start the engine HTTP role with no native process at endpoint boot."""
    public = PublicEngineConfig.model_validate(public_config)
    home = Path(public.home or "/tmp/ach-home")
    work_dir = Path(public.work_dir or home / "workspace")
    public_context = Path(public.public_context or "/tmp/ach-public-context")
    # E creates only its private home/workspace.  Public context may be a late-mounted
    # read-only volume hydrated by H, so endpoint boot never creates H-owned paths.
    home.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    link_public_context(home, public_context, create_public=False)
    if public.persistence_enabled:
        store: MutableMapping[str, str] = NativeSessionStore(home)
    else:
        from ach_agent.engine.base.pool import _LRUSessionMap

        store = _LRUSessionMap()
    if public.engine_type == "pi":
        from ach_agent.engine.pi.driver import PiDriver

        driver: EngineDriver = PiDriver()
    else:
        driver = OpencodeDriver()
    service = ExecutionService(driver, store)
    app = create_execution_app(service)
    host = os.environ.get("ACH_ENGINE_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("ACH_ENGINE_PORT", "8081"))
    except ValueError as exc:
        close = getattr(store, "close", None)
        if close is not None:
            close()
        raise SplitRoleConfigError("ACH_ENGINE_PORT must be an integer") from exc
    server = uvicorn.Server(uvicorn.Config(app=app, host=host, port=port, log_level="warning"))

    async def stop_on_unhealthy() -> None:
        while not service.shutdown_requested:
            await asyncio.sleep(0.05)
        # Force the held controller stream to observe shutdown.  Merely setting
        # Server.should_exit leaves an active NDJSON response open indefinitely.
        await service.release_controller(service.controller_id or "")
        server.should_exit = True

    watcher = asyncio.create_task(stop_on_unhealthy())
    try:
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        with contextlib.suppress(Exception):
            await service.release_controller(service.controller_id or "")
        close = getattr(store, "close", None)
        if close is not None:
            close()
    if service.shutdown_requested or service.controller_cleanup_error:
        raise RuntimeError("engine role shutdown requested after unreliable cleanup")


async def run_harness(_cfg: AgentConfig) -> None:
    """Reserved role entrypoint; harness orchestration is owned by Task 8B."""
    raise NotImplementedError("harness role orchestration is implemented by the local launcher")


async def run_channels(_channel_config: JsonValue) -> None:
    """Reserved role entrypoint; channel startup is owned by Task 8B."""
    raise NotImplementedError("channels role orchestration is implemented by the local launcher")

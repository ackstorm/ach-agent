# SPDX-License-Identifier: MIT
"""Cold composition and ordinary uvicorn lifecycle for the mini execution role."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.engine.opencode import OpenCodeEngine
from ach_runtime.execution.auth import create_authenticated_app
from ach_runtime.execution.service import DEFAULT_WORKSPACE_DIR, ExecutionService
from ach_runtime.execution.sessions import DEFAULT_SESSIONS_ROOT, SessionRegistry
from ach_runtime.observability.runs import configure_logging

REQUIRED_ENV = (
    "ACH_SANDBOX_VERIFY_KEY",
    "ACH_WORKSPACE_NAME",
    "ACH_WORKSPACE_REF",
)


def load_bootstrap(config: Path | None, environ: Mapping[str, str]) -> ExecutionBootstrap:
    inline = environ.get("ACH_BOOTSTRAP_JSON")
    if inline and config is not None:
        raise ValueError("ACH_BOOTSTRAP_JSON and --config are mutually exclusive")
    if inline:
        try:
            value = json.loads(inline)
        except json.JSONDecodeError as exc:
            raise ValueError("ACH_BOOTSTRAP_JSON is not valid JSON") from exc
    elif config is not None:
        try:
            value = json.loads(config.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read execution bootstrap {config}: {exc}") from exc
    else:
        raise ValueError("no execution bootstrap: set ACH_BOOTSTRAP_JSON or pass --config")
    return ExecutionBootstrap.model_validate(value)


def _identity(environ: Mapping[str, str]) -> tuple[str, str, str]:
    missing = [name for name in REQUIRED_ENV if not environ.get(name)]
    if missing:
        raise ValueError(f"required environment is missing: {', '.join(missing)}")
    verify_key, workspace_name, workspace_ref = (environ[name] for name in REQUIRED_ENV)
    if not workspace_ref.strip() or not workspace_name.strip():
        raise ValueError("workspace identity environment values must not be blank")
    return verify_key, workspace_name, workspace_ref


def build_execution_app(
    bootstrap: ExecutionBootstrap,
    *,
    environ: Mapping[str, str],
    workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
) -> FastAPI:
    verify_key, workspace_name, workspace_ref = _identity(environ)
    instance_id = str(uuid.uuid4())
    engine = OpenCodeEngine()
    service = ExecutionService(
        engine=engine,
        sessions=SessionRegistry(sessions_root),
        workspace_dir=workspace_dir,
        workspace_ref=workspace_ref,
    )
    app = create_authenticated_app(
        service,
        verify_key=verify_key,
        workspace_name=workspace_name,
        instance_id=instance_id,
        bootstrap=bootstrap,
    )
    app.state.bootstrap = bootstrap
    app.state.instance_id = instance_id
    return app


async def run_execution(
    config: Path | None,
    *,
    host: str = "0.0.0.0",
    port: int = 8080,
    workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
    sessions_root: Path = DEFAULT_SESSIONS_ROOT,
) -> None:
    configure_logging()
    bootstrap = load_bootstrap(config, os.environ)
    # Fail identity and key validation before uvicorn opens a listening socket.
    app = build_execution_app(
        bootstrap, environ=os.environ, workspace_dir=workspace_dir, sessions_root=sessions_root
    )
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, lifespan="on", timeout_graceful_shutdown=25)
    )
    await server.serve()

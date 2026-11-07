# SPDX-License-Identifier: MIT
"""Cold composition and ordinary uvicorn lifecycle for the mini execution role."""

from __future__ import annotations

import json
import logging
import os
import socket
import ssl
import time
import uuid
from collections.abc import Mapping
from pathlib import Path

from fastapi import FastAPI

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.engine.opencode import OpenCodeEngine
from ach_runtime.engine.pi import PiEngine
from ach_runtime.execution.auth import create_authenticated_app
from ach_runtime.execution.service import DEFAULT_WORKSPACE_DIR, ExecutionService
from ach_runtime.execution.sessions import DEFAULT_SESSIONS_ROOT, SessionRegistry
from ach_runtime.observability.runs import configure_logging

log = logging.getLogger(__name__)

NETWORK_POLICY_OPT_OUT = "ACH_INSECURE_WITHOUT_NETWORK_POLICY"

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


def require_network_policy(
    environ: Mapping[str, str], *, attempts: int = 30, timeout: float = 2.0, pause: float = 1.0
) -> None:
    """Refuse to run where the cluster does not enforce NetworkPolicy: the sandbox's egress lockdown
    is the OpenShell driver's deny-all NetworkPolicy, so without enforcement it reaches any IP.

    The probe is the kube API Service, which every pod is told about and that policy blocks: a TCP
    connect plus a TLS handshake, never a request. A bare connect proves nothing under ambient,
    whose ztunnel accepts locally and only then dials out. A CNI may leave a new pod open until its
    policy lands, so one blocked attempt in the window is enough."""
    if environ.get(NETWORK_POLICY_OPT_OUT) == "true":
        log.warning("NetworkPolicy enforcement check skipped (openshell.insecureWithoutNetworkPolicy)")
        return
    host = environ.get("KUBERNETES_SERVICE_HOST")
    if not host:
        return  # not on Kubernetes
    port = int(environ.get("KUBERNETES_SERVICE_PORT") or 443)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    for attempt in range(attempts):
        if attempt:
            time.sleep(pause)
        try:
            with socket.create_connection((host, port), timeout=timeout) as raw, context.wrap_socket(raw):
                pass
        except OSError:
            return
    raise RuntimeError(
        f"NetworkPolicy is not enforced: the sandbox reached the Kubernetes API at {host}:{port}, which "
        "the OpenShell sandbox NetworkPolicy blocks. Enable NetworkPolicy enforcement in the cluster's "
        "CNI, or set the chart value openshell.insecureWithoutNetworkPolicy=true to run without it."
    )


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
    # A Workspace PVC mounted at /sandbox/ach (persistence type volume) starts empty.
    workspace_dir.mkdir(parents=True, exist_ok=True)
    instance_id = str(uuid.uuid4())
    engine = PiEngine() if bootstrap.engine == "pi" else OpenCodeEngine()
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
    app.state.engine = engine
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
    require_network_policy(os.environ)
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

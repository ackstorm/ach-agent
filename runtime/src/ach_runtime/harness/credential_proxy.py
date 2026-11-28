# SPDX-License-Identifier: Apache-2.0
"""ACH credential proxy for a HermesAgent.

Hermes runs in a sandbox that holds no ACH credential. It sends its model (`/v1/...`) and MCP
(`/mcp/...`) requests here with a per-HermesAgent bearer; the proxy checks it, replaces it with
the ACH credential (`KeySource`: static ek_ or exchanged JWT, rotated) and streams the request
to ACH unchanged otherwise.

At every start Hermes downloads `/hermes/bundle`: its config.yaml and skills, hydrated from the
ACH Environment as an Agent's are. The keys ACH writes are ACH's and are rewritten every time;
what spec.config adds beside them is kept.
"""

from __future__ import annotations

import copy
import hmac
import io
import json
import logging
import os
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ach_runtime.facades._proxy import ProcessIdentity, forward
from ach_runtime.harness.hydrate import HydratedCatalog, HydrationError, HydrationInput, Hydrator, stage_context
from ach_runtime.harness.keys import Exchange, KeySource

log = logging.getLogger(__name__)

PREFIXES = ("/v1/", "/mcp/")
# Hermes expands ${VAR} in config.yaml; the sandbox sets these.
PROXY_URL, PROXY_TOKEN = "${ACH_PROXY_URL}", "${ACH_PROXY_TOKEN}"
ACH_SKILLS = "ach-skills"


def render_hermes_config(user: dict[str, Any], catalog: HydratedCatalog, base_url: str, instructions: str) -> dict[str, Any]:
    """Hermes' config.yaml: spec.config with the ACH-owned keys (model, the Environment's MCP
    servers, the ACH skills directory, the Environment's instructions) set on top."""
    config = copy.deepcopy(user)
    model = {"provider": "custom", "base_url": f"{PROXY_URL}/v1", "api_key": PROXY_TOKEN, "default": catalog.model.id}
    metadata = catalog.model.metadata
    if metadata is not None and metadata.context_limit:
        model["context_length"] = metadata.context_limit
    config["model"] = {**(config.get("model") or {}), **model}

    servers = dict(config.get("mcp_servers") or {})
    ach_host = urlsplit(base_url).netloc
    for server in catalog.mcp_servers:
        endpoint = urlsplit(server.endpoint or f"/mcp/{server.id}")
        if endpoint.netloc not in ("", ach_host) or not endpoint.path.startswith("/mcp/"):
            log.warning("MCP server %s is not served by ACH at %s: not configured", server.id, ach_host)
            continue
        servers[server.id] = {"url": PROXY_URL + endpoint.path, "headers": {"Authorization": f"Bearer {PROXY_TOKEN}"}}
    config["mcp_servers"] = servers

    skills = dict(config.get("skills") or {})
    ach_dir = "${HERMES_HOME}/" + ACH_SKILLS
    skills["external_dirs"] = [d for d in skills.get("external_dirs") or [] if d != ach_dir] + [ach_dir]
    config["skills"] = skills

    if instructions:
        config["agent"] = {**(config.get("agent") or {}), "system_prompt": instructions}
    return config


async def build_bundle(catalog: HydratedCatalog, base_url: str, user: dict[str, Any], client: httpx.AsyncClient) -> bytes:
    """A tar.gz for HERMES_HOME: config.yaml (as JSON, which is YAML) and ach-skills/."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        await stage_context(catalog, root, excluded_skills=(), client=client)
        # An Environment's instructions, by name, as an Agent appends them.
        instructions = "\n\n".join(
            p.read_text(encoding="utf-8") for p in sorted((root / "instructions").glob("*/*")) if p.is_file()
        )
        config = json.dumps(render_hermes_config(user, catalog, base_url, instructions), indent=1).encode()
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            info = tarfile.TarInfo("config.yaml")
            info.size = len(config)
            tar.addfile(info, io.BytesIO(config))
            (root / "skills").mkdir(exist_ok=True)
            tar.add(root / "skills", arcname=ACH_SKILLS)
        return buffer.getvalue()


def create_proxy_app(base_url: str, keys: KeySource, bearer: str, identity: ProcessIdentity,
                     client: httpx.AsyncClient, *, model: str = "", user_config: dict[str, Any] | None = None,
                     ach_client: httpx.AsyncClient | None = None) -> Starlette:
    base_url = base_url.rstrip("/")
    expected = f"Bearer {bearer}".encode()
    # Hydration and context downloads go to ACH with the rotating credential.
    ach = ach_client or httpx.AsyncClient(auth=keys.auth, timeout=60)

    def authorized(request: Request) -> bool:
        return hmac.compare_digest(request.headers.get("authorization", "").encode(), expected)

    async def healthz(_: Request) -> Response:
        return JSONResponse({"ready": keys.ready()}, status_code=200 if keys.ready() else 503)

    async def bundle(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            catalog = await Hydrator(resolve_ek=keys.current, client=ach).load(HydrationInput(
                agent=identity.agent, base_url=base_url, environment=identity.environment, model=model))
            body = await build_bundle(catalog, base_url, user_config or {}, ach)
        except (HydrationError, httpx.HTTPError) as exc:
            log.warning("Hermes bundle failed: %s", exc)
            return JSONResponse({"error": f"hydration failed: {exc}"}, status_code=502)
        return Response(body, media_type="application/gzip")

    async def proxy(request: Request) -> Response:
        if not authorized(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        path = request.url.path
        if not path.startswith(PREFIXES):
            return JSONResponse({"error": "not found"}, status_code=404)
        return await forward(client, base_url + path, request, {}, identity=identity, auth=keys.auth)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        await keys.start()
        try:
            yield
        finally:
            await keys.close()
            await client.aclose()
            await ach.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route("/healthz", healthz), Route("/hermes/bundle", bundle),
                             Route("/{path:path}", proxy, methods=methods)], lifespan=lifespan)


def _from_env(env: os._Environ[str] | dict[str, str]) -> Starlette:
    base_url = env["ACH_BASE_URL"]
    if env.get("ACH_EXCHANGE_IDENTITY"):
        keys = KeySource(exchange=Exchange(base_url, env["ACH_EXCHANGE_IDENTITY"], env["ACH_ENVIRONMENT"],
                                           env["ACH_EXCHANGE_TOKEN_PATH"]),
                         client=httpx.AsyncClient(timeout=30))
    else:
        keys = KeySource(env["ACH_KEY"])
    identity = ProcessIdentity(agent=env["ACH_AGENT_NAME"], environment=env["ACH_ENVIRONMENT"])
    upstream = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
    return create_proxy_app(base_url, keys, env["ACH_PROXY_TOKEN"], identity, upstream,
                            model=env["HERMES_MODEL"], user_config=json.loads(env.get("HERMES_CONFIG") or "{}"))


def run_credential_proxy(host: str, port: int) -> None:
    import uvicorn

    from ach_runtime.harness.boot import FACADE_KEEP_ALIVE_SECONDS

    # Keep-alive above the OpenShell relay's idle cut, as for the facades.
    uvicorn.run(_from_env(os.environ), host=host, port=port, timeout_keep_alive=FACADE_KEEP_ALIVE_SECONDS,
                access_log=False)

# SPDX-License-Identifier: Apache-2.0
"""ACH credential proxy for a HermesAgent.

Hermes runs in a sandbox that holds no ACH credential. It sends its model (`/v1/...`) and MCP
(`/mcp/...`) requests here with a per-HermesAgent bearer; the proxy checks it, replaces it with
the ACH credential (`KeySource`: static ek_ or exchanged JWT, rotated) and streams the request
to ACH unchanged otherwise.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ach_runtime.facades._proxy import ProcessIdentity, forward
from ach_runtime.harness.keys import Exchange, KeySource

PREFIXES = ("/v1/", "/mcp/")


def create_proxy_app(base_url: str, keys: KeySource, bearer: str, identity: ProcessIdentity,
                     client: httpx.AsyncClient) -> Starlette:
    base_url = base_url.rstrip("/")
    expected = f"Bearer {bearer}".encode()

    async def healthz(_: Request) -> Response:
        return JSONResponse({"ready": keys.ready()}, status_code=200 if keys.ready() else 503)

    async def proxy(request: Request) -> Response:
        if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected):
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

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[Route("/healthz", healthz), Route("/{path:path}", proxy, methods=methods)],
                     lifespan=lifespan)


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
    return create_proxy_app(base_url, keys, env["ACH_PROXY_TOKEN"], identity, upstream)


def run_credential_proxy(host: str, port: int) -> None:
    import uvicorn

    from ach_runtime.harness.boot import FACADE_KEEP_ALIVE_SECONDS

    # Keep-alive above the OpenShell relay's idle cut, as for the facades.
    uvicorn.run(_from_env(os.environ), host=host, port=port, timeout_keep_alive=FACADE_KEEP_ALIVE_SECONDS,
                access_log=False)

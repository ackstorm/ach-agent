# SPDX-License-Identifier: MIT
"""MCP facade — fronts the granted (non-excluded) ACH MCP servers on a server-held route
(contract §5/§11). Adapted from old-ach-agent `engine/mcp_proxy.py::McpProxy`.

Two exclusion layers, both enforced before any upstream call (contract: "Server
exclusions alone do not implement tool exclusions or live revocation" — the reuse handoff
explicitly calls out that per-tool exclusion is new work, not already solved by the legacy
per-server exclude):

- `exclude` (contract `ach.capability.filter.exclude.mcpServers`) — a server id in it is
  never routed at all, applied at construction time (old-ach-agent semantic).
- `exclude_tools` (`...exclude.tools`) — a `tools/call` JSON-RPC request naming an excluded
  tool, alone or inside a batch, is denied per request, and the excluded tools are dropped from a `tools/list`
  reply (JSON or SSE) so the model never sees them; any other method/body passes through
  unexamined. Names are as the upstream lists them (`<server>.<tool>` through LiteLLM).

The engine prefixes every MCP tool with its server id itself, so a gateway prefix would
reach the model twice (`mcp-x_mcp-x.tool`) and weak models then call names that do not
exist. A `tools/list` reply therefore advertises `<server>.<tool>` as the bare `<tool>`, and
a `tools/call` naming a bare tool is mapped back to the upstream name before the exclusion
check and the upstream call.

Authentication is the accepted Workspace HMAC (`harness/tokens.py::authenticate_facade`);
see `facades/model.py`'s module docstring for the same removed capability machinery.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ach_runtime.facades._proxy import ProcessIdentity, forward, has_encoded_body, join_tail
from ach_runtime.harness.tokens import WorkspaceAuthenticationFailed, authenticate_facade

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
_INTERNAL_CLIENT_TIMEOUT = httpx.Timeout(None, connect=30.0)

OwnedWorkspace = Callable[[str], Awaitable[bool]]
CorrelationHeaders = Callable[[], Mapping[str, str]] | None


def _jsonrpc_method(body: bytes) -> tuple[str | None, Any]:
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    return parsed.get("method"), parsed.get("params")


def _tool_calls(parsed: Any) -> list[dict[str, Any]]:
    """The `params` of every `tools/call` message in a JSON-RPC body or batch."""
    return [
        message["params"]
        for message in (parsed if isinstance(parsed, list) else [parsed])
        if isinstance(message, dict)
        and message.get("method") == "tools/call"
        and isinstance(message.get("params"), dict)
    ]


def _upstream_tool_names(body: bytes, upstream: Mapping[str, str]) -> bytes:
    """Map the bare tool names a `tools/list` reply advertised back to the upstream's names.
    A body without such a call passes through byte for byte."""
    if not upstream or not body:
        return body
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return body
    calls = [p for p in _tool_calls(parsed) if isinstance(p.get("name"), str) and p["name"] in upstream]
    if not calls:
        return body
    for params in calls:
        params["name"] = upstream[params["name"]]
    return json.dumps(parsed, separators=(",", ":")).encode()


def _excluded_tool_call(body: bytes, exclude_tools: frozenset[str], prefix: str) -> bool:
    """True when the body, or any message of a JSON-RPC batch, calls an excluded tool, by its
    upstream name or by the bare name under this server's prefix."""
    if not exclude_tools or not body:
        return False
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return False
    return any(
        not isinstance(name := params.get("name"), str)
        or name in exclude_tools
        or prefix + name in exclude_tools
        for params in _tool_calls(parsed)
    )


def _listed_tools(
    exclude_tools: frozenset[str], prefix: str, upstream: dict[str, str]
) -> Callable[[bytes], bytes]:
    """Drop excluded tools from a `tools/list` reply, plain JSON or SSE `data:` lines, and
    advertise each `<prefix><tool>` as `<tool>`, recording the upstream name in `upstream`.
    Anything that does not parse as a JSON-RPC result with a `tools` list is left as is:
    the filter never fails the request."""

    def drop(message: bytes) -> bytes:
        try:
            parsed = json.loads(message)
            tools = parsed["result"]["tools"]
            if not isinstance(tools, list):
                return message
            kept = [t for t in tools if not (isinstance(t, dict) and t.get("name") in exclude_tools)]
            for tool in kept:
                name = tool.get("name") if isinstance(tool, dict) else None
                if isinstance(name, str) and name.startswith(prefix) and len(name) > len(prefix):
                    upstream[name[len(prefix):]] = name
                    tool["name"] = name[len(prefix):]
            parsed["result"]["tools"] = kept
        except Exception:  # noqa: BLE001 - an unfilterable reply passes through unchanged
            return message
        return json.dumps(parsed, separators=(",", ":")).encode()

    def sse_line(line: bytes) -> bytes:
        if not line.startswith(b"data:"):
            return line
        content = line.rstrip(b"\r\n")
        return b"data: " + drop(content[5:].strip()) + line[len(content) :]

    def transform(body: bytes) -> bytes:
        if not body.lstrip().startswith((b"event:", b"data:", b"id:", b":")):
            return drop(body)
        return b"".join(sse_line(line) for line in body.splitlines(keepends=True))

    return transform


class McpFacade:
    """`server_headers`/`key` live only in this instance's handler closure — never on an attribute
    reachable from outside this module (old-ach-agent invariant)."""

    def __init__(
        self,
        *,
        servers: dict[str, str],
        server_headers: Mapping[str, Mapping[str, str]],
        key: bytes,
        owned_workspace: OwnedWorkspace,
        exclude: frozenset[str],
        exclude_tools: frozenset[str],
        identity: ProcessIdentity,
        correlation_headers: CorrelationHeaders = None,
        client: httpx.AsyncClient | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=_INTERNAL_CLIENT_TIMEOUT)
        self._responses: set[httpx.Response] = set()
        endpoints = {sid: ep.rstrip("/") for sid, ep in servers.items() if sid not in exclude}
        headers_by_server = {sid: dict(h) for sid, h in server_headers.items()}
        # Per server: bare tool name -> upstream name, learnt from its `tools/list` replies.
        upstream_names: dict[str, dict[str, str]] = {sid: {} for sid in endpoints}

        async def handle(request: Request) -> Response:
            token = request.path_params["token"]
            try:
                await authenticate_facade(key, token, owned_workspace=owned_workspace)
            except WorkspaceAuthenticationFailed:
                return JSONResponse({"detail": "forbidden"}, status_code=403)

            server_id = request.path_params["server_id"]
            endpoint = endpoints.get(server_id)
            if endpoint is None:
                return JSONResponse({"detail": "unknown or excluded mcp server"}, status_code=404)

            body = await request.body()
            if exclude_tools and has_encoded_body(request.headers):
                # An encoded body cannot be inspected for an excluded tools/call.
                return JSONResponse({"detail": "encoded request body not accepted"}, status_code=415)
            prefix = f"{server_id}."
            body = _upstream_tool_names(body, upstream_names[server_id])
            if _excluded_tool_call(body, exclude_tools, prefix):
                return JSONResponse({"detail": "tool excluded by capability filter"}, status_code=403)

            target = join_tail(endpoint, request.path_params.get("tail", ""))
            if target is None:
                return JSONResponse({"detail": "path escapes the mcp server"}, status_code=400)
            correlation = correlation_headers() if correlation_headers is not None else None
            listing = _jsonrpc_method(body)[0] == "tools/list"
            return await forward(
                self._client,
                target,
                request,
                headers_by_server.get(server_id, {}),
                identity=identity,
                correlation=correlation,
                mcp=True,
                body=body,
                transform=_listed_tools(exclude_tools, prefix, upstream_names[server_id]) if listing else None,
                auth=auth,
                _responses=self._responses,
            )

        self._handle = handle

    def app(self) -> Starlette:
        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            try:
                yield
            finally:
                await self.aclose()

        return Starlette(
            routes=[
                Route("/t/{token}/mcp/{server_id}", self._handle, methods=_METHODS),
                Route("/t/{token}/mcp/{server_id}/{tail:path}", self._handle, methods=_METHODS),
            ],
            lifespan=lifespan,
        )

    async def aclose(self) -> None:
        """Close every response this facade still owns (even one whose streaming body was
        never iterated by the ASGI layer), then close only the client this facade created
        itself — an injected client outlives this facade and stays the caller's to close.
        Every close is attempted even if an earlier one raised; close errors are never
        silently swallowed as a clean shutdown."""
        responses = list(self._responses)
        self._responses.clear()
        results: list[Any] = list(
            await asyncio.gather(*(r.aclose() for r in responses), return_exceptions=True)
        )
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001 - reported below, not swallowed
                results.append(exc)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise errors[0]

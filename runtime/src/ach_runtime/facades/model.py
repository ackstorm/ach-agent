# SPDX-License-Identifier: MIT
"""Model facade — fronts the ONE granted model endpoint on a server-held route for the
execution pod (contract §5/§11). Adapted from old-ach-agent `engine/mcp_proxy.py::ModelProxy`;
not ported: `CostAccountant`/`CostObserver` (unused accounting, contract §9/Task 8) and the
module-global `_MODEL_PROXIES` startup registry (task-7-reuse-handoff.md).

The facade fronts the provider's API prefix (`/v1`, `/gemini/v1beta`, `/anthropic`) for the
ONE configured model: `check_model_request` allow-lists the method, path and query, requires
the body (or Gemini path) to name exactly that model, and rejects body fields an LLM gateway
routes on. Anything else is refused before any upstream call. Requests whose path would climb
out of that prefix are refused as well (`join_tail`).

Authentication is the accepted Workspace HMAC (`harness/tokens.py::authenticate_facade`):
the capability/resolver/clock machinery this module used to carry is gone (root-reviewed
correction) — unknown/wrong-key/non-owned tokens deny with zero upstream egress; an
untokenized request never matches a route at all (Starlette 404).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from ach_runtime.facades._proxy import ProcessIdentity, forward, has_encoded_body, join_tail
from ach_runtime.harness.hydrate import HydratedCatalog, ModelMetadata
from ach_runtime.harness.tokens import WorkspaceAuthenticationFailed, authenticate_facade

_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
_BODY_MODEL_PATHS = {
    "openai": frozenset({"chat/completions", "responses"}),
    "anthropic": frozenset({"messages", "messages/count_tokens"}),
}
# Top-level body fields an LLM gateway such as LiteLLM routes or authenticates on.
_ROUTING_FIELDS = frozenset({"fallbacks", "user_config", "api_base", "base_url", "api_key"})
_GEMINI_PATH = re.compile(r"models/([^:]+):(generateContent|streamGenerateContent|countTokens)")
_INTERNAL_CLIENT_TIMEOUT = httpx.Timeout(None, connect=30.0)

OwnedWorkspace = Callable[[str], Awaitable[bool]]
CorrelationHeaders = Callable[[], Mapping[str, str]] | None


class _DuplicateKey(ValueError):
    pass


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Parsers disagree on which duplicate wins, so a body with one is refused."""
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKey(keys)
    return dict(pairs)


def check_model_request(
    api: str,
    model: str,
    method: str,
    tail: str,
    raw_path: bytes,
    query: Mapping[str, str],
    encoded_body: bool,
    body: bytes,
) -> tuple[int, str] | None:
    """(status, reason) when the request must not leave the facade; None when it may."""
    if method != "POST":
        return 405, "method not allowed"
    if b"%" in raw_path:
        return 400, "encoded path not allowed"
    if encoded_body:
        return 400, "encoded body not allowed"
    if api == "gemini":
        match = _GEMINI_PATH.fullmatch(tail)
        if match is None:
            return 404, "path not allowed"
        if match.group(1) != model or {"", ".", ".."} & set(match.group(1).split("/")):
            return 400, "model not allowed"
        if set(query) - {"alt"} or query.get("alt", "sse") != "sse":
            return 400, "query not allowed"
    else:
        if tail not in _BODY_MODEL_PATHS[api]:
            return 404, "path not allowed"
        if query:
            return 400, "query not allowed"
    try:
        document = json.loads(body, object_pairs_hook=_no_duplicate_keys)
    except _DuplicateKey:
        return 400, "duplicate key"
    except ValueError:
        return 400, "body is not JSON"
    if not isinstance(document, dict):
        return 400, "body is not an object"
    if api != "gemini" and document.get("model") != model:
        return 400, "model not allowed"
    if _ROUTING_FIELDS & document.keys():
        return 400, "routing field not allowed"
    return None


class ModelFacade:
    """`headers`/`key` live only in this instance's handler closure — never on an attribute
    reachable from outside this module (old-ach-agent invariant)."""

    def __init__(
        self,
        *,
        endpoint: str,
        headers: Mapping[str, str],
        api: str,
        model: str,
        key: bytes,
        owned_workspace: OwnedWorkspace,
        identity: ProcessIdentity,
        correlation_headers: CorrelationHeaders = None,
        client: httpx.AsyncClient | None = None,
        auth: httpx.Auth | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=_INTERNAL_CLIENT_TIMEOUT)
        self._responses: set[httpx.Response] = set()
        target_base = endpoint.rstrip("/")
        upstream_headers = dict(headers)

        async def handle(request: Request) -> Response:
            token = request.path_params["token"]
            try:
                await authenticate_facade(key, token, owned_workspace=owned_workspace)
            except WorkspaceAuthenticationFailed:
                return JSONResponse({"detail": "forbidden"}, status_code=403)

            tail = request.path_params.get("tail", "")
            body = await request.body()
            rejected = check_model_request(
                api, model, request.method, tail, request.scope.get("raw_path", b""),
                request.query_params, has_encoded_body(request.headers), body,
            )
            if rejected is not None:
                status, reason = rejected
                return JSONResponse({"detail": reason}, status_code=status)

            target = join_tail(target_base, tail)
            if target is None:
                return JSONResponse({"detail": "path escapes the model endpoint"}, status_code=400)
            correlation = correlation_headers() if correlation_headers is not None else None
            return await forward(
                self._client,
                target,
                request,
                upstream_headers,
                identity=identity,
                correlation=correlation,
                body=body,
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
                Route("/t/{token}", self._handle, methods=_METHODS),
                Route("/t/{token}/{tail:path}", self._handle, methods=_METHODS),
            ],
            lifespan=lifespan,
        )

    async def aclose(self) -> None:
        """Close every response this facade still owns (even one whose streaming body was
        never iterated by the ASGI layer, so its own `finally` never ran), then close only
        the client this facade created itself — an injected client outlives this facade and
        stays the caller's to close. Every close is attempted even if an earlier one raised;
        close errors are never silently swallowed as a clean shutdown."""
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


# ---------------------------------------------------------------------------
# public_catalog — pure projection of the hydrated catalog for the engine owner.
# ---------------------------------------------------------------------------


def present_metadata(metadata: ModelMetadata | None) -> dict[str, Any]:
    """Project `ModelMetadata` into the plugin fixture's model-info shape
    (`plugins/managed-config/index.js::toModelInfo`). Every key is independently optional:
    absent upstream data is omitted, never fabricated as a zero/false default — an explicit
    `False`/`0` from upstream is kept. `metadata=None` (degraded hydration) yields `{}`.
    """
    if metadata is None:
        return {}

    out: dict[str, Any] = {}

    limit: dict[str, int] = {}
    if metadata.context_limit is not None:
        limit["context"] = metadata.context_limit
    if metadata.output_limit is not None:
        limit["output"] = metadata.output_limit
    if limit:
        out["limit"] = limit

    cost: dict[str, float] = {}
    if metadata.input_cost_per_token is not None:
        cost["input"] = round(metadata.input_cost_per_token * 1_000_000, 6)
    if metadata.output_cost_per_token is not None:
        cost["output"] = round(metadata.output_cost_per_token * 1_000_000, 6)
    if metadata.cache_read_input_token_cost is not None:
        cost["cache_read"] = round(metadata.cache_read_input_token_cost * 1_000_000, 6)
    if cost:
        out["cost"] = cost

    if metadata.supports_function_calling is not None:
        out["tool_call"] = metadata.supports_function_calling
    if metadata.supports_reasoning is not None:
        out["reasoning"] = metadata.supports_reasoning

    modality_flags = (
        metadata.supports_vision,
        metadata.supports_audio_input,
        metadata.supports_pdf_input,
        metadata.supports_video_input,
    )
    if any(flag is not None for flag in modality_flags):
        input_modalities = ["text"]
        if metadata.supports_vision:
            input_modalities.append("image")
        if metadata.supports_audio_input:
            input_modalities.append("audio")
        if metadata.supports_pdf_input:
            input_modalities.append("pdf")
        if metadata.supports_video_input:
            input_modalities.append("video")
        out["modalities"] = {"input": input_modalities, "output": ["text"]}
        out["attachment"] = any(flag for flag in modality_flags)

    return out


def public_catalog(
    catalog: HydratedCatalog,
    *,
    provider_id: str,
    model_url: str,
    mcp_urls: dict[str, str],
    memory_url: str | None = None,
    artifact_url: str | None = None,
) -> dict[str, Any]:
    """Pure projection of the already-hydrated catalog into the engine owner's fixture
    shape — not a new registry/type. `mcp_urls` must already exclude filtered/raw-memory
    server ids; this function performs no filtering of its own."""
    mcp: dict[str, Any] = {gid: {"url": url, "headers": {}} for gid, url in mcp_urls.items()}
    if memory_url:
        mcp["memory"] = {"url": memory_url, "headers": {}}
    if artifact_url:
        mcp["artifacts"] = {"url": artifact_url, "headers": {}}

    return {
        "providers": {
            provider_id: {
                "baseURL": model_url,
                "apiKey": "local-proxy",
                "models": {catalog.model.id: present_metadata(catalog.model.metadata)},
            }
        },
        "mcp": mcp,
    }

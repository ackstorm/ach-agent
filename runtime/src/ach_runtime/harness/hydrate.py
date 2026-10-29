# SPDX-License-Identifier: MIT
"""Hydration + model-metadata facade input (contract §5; Task 7).

`Hydrator.load(config) -> HydratedCatalog` combines the two authorized ACH surfaces:
POST `/platform/hydrate` (granted model/MCP/A2A ids+endpoints, hydrated context) and GET
`/v2/model/info?model=<granted id>` (context/output limits, modalities, cost; not returned
by hydrate itself — task-7-model-metadata-ruling-evidence.md).

Reused from `old-ach-agent/src/ach_agent/engine/{hydrate,cost}.py`
(`task-7-reuse-handoff.md`): the typed `{id,endpoint}` manifest objects, the POST/GET
request shape and the price-entry match (`model_name` then `litellm_params.model`).
Deliberately NOT reused: `hydrate()`'s unconditional process-identity commit (this runtime
has no such global — the caller already knows its own `ach.environment`) and
`resolve_model()`'s empty-catalog local-development fallback / `sys.exit` — both become an
explicit `HydrationError` here (fail closed, never pretend an empty-catalog dev mode).

Essential vs. degraded data (task-7-model-metadata-ruling-evidence.md): the hydrated
catalog (granted model id/endpoint) is essential — its absence or an explicit-environment
mismatch raises `HydrationError`. Model metadata (limits/modalities/cost) is a still-
unverified live integration proof, never essential: a missing, malformed or unreachable
`/v2/model/info` response degrades to `metadata=None`, never a fabricated zero-cost/no-limit
default (the OpenCode adapter's own defaults are not evidence upstream supplied those
values).
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError

from ach_runtime.facades._proxy import ProcessIdentity, with_identity_headers

if TYPE_CHECKING:
    from ach_runtime.config.schema import RuntimeConfig

_REQUEST_TIMEOUT_SECONDS = 30.0


class HydrationError(RuntimeError):
    """Essential hydration/catalog data missing or invalid — fail closed (contract §5)."""


# ---------------------------------------------------------------------------
# Hydration manifest — exact shape of POST /platform/hydrate (contract §5/§10)
# ---------------------------------------------------------------------------


class ModelEntry(BaseModel):
    id: str
    endpoint: str = ""


class McpServerEntry(BaseModel):
    id: str
    endpoint: str = ""


class A2AAgentEntry(BaseModel):
    id: str = ""
    endpoint: str = ""


class ContextItem(BaseModel):
    name: str = ""
    id: str = ""
    download_url: str = Field(default="", alias="downloadUrl")


class _RuntimeBlock(BaseModel):
    models: list[ModelEntry] = Field(default_factory=list)
    mcp_servers: list[McpServerEntry] = Field(default_factory=list, alias="mcpServers")
    a2a_agents: list[A2AAgentEntry] = Field(default_factory=list, alias="a2aAgents")


class _ContextBlock(BaseModel):
    skills: list[ContextItem] = Field(default_factory=list)
    prompts: list[ContextItem] = Field(default_factory=list)
    artifacts: list[ContextItem] = Field(default_factory=list)


class HydrationManifest(BaseModel):
    environment: str
    runtime: _RuntimeBlock = Field(default_factory=_RuntimeBlock)
    context: _ContextBlock = Field(default_factory=_ContextBlock)

    @property
    def models(self) -> list[str]:
        return [m.id for m in self.runtime.models]


async def _post_hydrate(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    r = await client.post(url, headers=headers, json=body, timeout=_REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    data: dict[str, Any] = r.json()
    return data


async def fetch_hydration_manifest(
    base_url: str,
    ek: str,
    environment: str,
    *,
    identity: ProcessIdentity,
    client: httpx.AsyncClient | None = None,
) -> HydrationManifest:
    """POST `/platform/hydrate` with the environment-bound ek_ plus explicit agent/
    environment identity (contract §5).

    `environment` is sent explicitly and rechecked against the response: "un `environment`
    explícito distinto del hidratado es error" (contract §5) — ACH binds environment to the
    credential server-side; a mismatch here means the caller's own config disagrees with
    what that credential is actually bound to, which must fail, not silently proceed under
    either value.
    """
    headers = with_identity_headers({"x-ach-key": ek}, identity)
    owns_client = client is None
    http_client = client or httpx.AsyncClient()
    try:
        try:
            data = await _post_hydrate(
                http_client,
                f"{base_url.rstrip('/')}/platform/hydrate",
                headers,
                {"environment": environment},
            )
        except httpx.HTTPError as exc:
            raise HydrationError(f"hydrate request failed: {exc}") from exc
        except ValueError as exc:
            raise HydrationError(f"hydrate response is not valid JSON: {exc}") from exc
    finally:
        if owns_client:
            await http_client.aclose()

    try:
        manifest = HydrationManifest.model_validate(data)
    except ValidationError as exc:
        raise HydrationError(f"hydrate response invalid: {exc}") from exc

    # An empty requested environment means "whatever this credential is bound to" (the operator
    # renders capability.environment only when authored); only an explicit request can mismatch.
    if environment and manifest.environment != environment:
        raise HydrationError(
            f"environment mismatch: requested {environment!r}, hydrated {manifest.environment!r}"
        )
    return manifest


def resolve_model(manifest: HydrationManifest, model_id: str) -> ModelEntry:
    """Fail closed on an empty catalog or a non-granted model — never a dev-mode fallback."""
    if not manifest.runtime.models:
        raise HydrationError("hydration returned an empty model catalog")
    for entry in manifest.runtime.models:
        if entry.id == model_id:
            if not entry.endpoint:
                raise HydrationError(f"model {model_id!r} has an empty endpoint")
            return entry
    raise HydrationError(f"model {model_id!r} is not granted by hydration")


# ---------------------------------------------------------------------------
# Model metadata — GET /v2/model/info?model=<granted id> (degraded, never essential)
# ---------------------------------------------------------------------------


class ModelMetadata(BaseModel):
    """Every field is optional and distinguishes "upstream never supplied this" (`None`)
    from an explicit value, including explicit `False`/`0` — Task 7's `bool = False`
    defaults silently turned an absent flag into a fabricated negative; contract §5's
    "never a fabricated zero-cost/no-limit default" applies to booleans too, not just the
    numeric fields (see `_optional_bool` below)."""

    context_limit: int | None = None
    output_limit: int | None = None
    supports_vision: bool | None = None
    supports_pdf_input: bool | None = None
    supports_audio_input: bool | None = None
    supports_video_input: bool | None = None
    mode: str | None = None
    supports_function_calling: bool | None = None
    supports_reasoning: bool | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    cache_read_input_token_cost: float | None = None


_NUMERIC_METADATA_FIELDS = (
    "max_input_tokens",
    "max_output_tokens",
    "input_cost_per_token",
    "output_cost_per_token",
    "cache_read_input_token_cost",
)


def _is_finite_number(raw: Any) -> bool:
    return isinstance(raw, int | float) and not isinstance(raw, bool) and math.isfinite(raw)


def _optional_bool(model_info: dict[str, Any], key: str) -> bool | None:
    """`None` when `key` is absent — distinct from an explicit `False` (contract §5)."""
    raw = model_info.get(key)
    return raw if isinstance(raw, bool) else None


def _match_entry(data: list[Any], model_name: str) -> dict[str, Any] | None:
    """Two-step match reused verbatim from legacy `cost.py::_match_entry`."""
    for entry in data:
        if isinstance(entry, dict) and entry.get("model_name") == model_name:
            return entry
    for entry in data:
        if isinstance(entry, dict):
            params = entry.get("litellm_params")
            if isinstance(params, dict) and params.get("model") == model_name:
                return entry
    return None


def _parse_model_info(model_info: dict[str, Any]) -> ModelMetadata | None:
    for key in _NUMERIC_METADATA_FIELDS:
        raw = model_info.get(key)
        if raw is not None and not _is_finite_number(raw):
            return None  # malformed — never fabricate a value from it
    try:
        return ModelMetadata(
            context_limit=model_info.get("max_input_tokens"),
            output_limit=model_info.get("max_output_tokens"),
            supports_vision=_optional_bool(model_info, "supports_vision"),
            supports_pdf_input=_optional_bool(model_info, "supports_pdf_input"),
            supports_audio_input=_optional_bool(model_info, "supports_audio_input"),
            supports_video_input=_optional_bool(model_info, "supports_video_input"),
            mode=model_info.get("mode"),
            supports_function_calling=_optional_bool(model_info, "supports_function_calling"),
            supports_reasoning=_optional_bool(model_info, "supports_reasoning"),
            input_cost_per_token=model_info.get("input_cost_per_token"),
            output_cost_per_token=model_info.get("output_cost_per_token"),
            cache_read_input_token_cost=model_info.get("cache_read_input_token_cost"),
        )
    except ValidationError:
        return None  # malformed typed value (e.g. a fractional token limit) — degrade


async def fetch_model_metadata(
    base_url: str,
    ek: str,
    model_id: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> ModelMetadata | None:
    """GET `/v2/model/info?model=<granted id>` with `x-ach-key` (contract §5).

    Degrades to `None` on every failure mode (unreachable, non-2xx, malformed JSON, no
    matching entry, non-dict/malformed `model_info`) — this endpoint's availability for a
    non-admin ek_ caller is an unverified live integration proof
    (task-7-model-metadata-ruling-evidence.md), not essential catalog data.
    """
    owns_client = client is None
    http_client = client or httpx.AsyncClient()
    try:
        try:
            r = await http_client.get(
                f"{base_url.rstrip('/')}/v2/model/info",
                params={"model": model_id},
                headers={"x-ach-key": ek},
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError:
            return None
        if r.status_code >= 400:
            return None
        try:
            body = r.json()
        except ValueError:
            return None
    finally:
        if owns_client:
            await http_client.aclose()

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return None
    entry = _match_entry(data, model_id)
    if entry is None:
        return None
    model_info = entry.get("model_info")
    if not isinstance(model_info, dict):
        return None
    return _parse_model_info(model_info)


# ---------------------------------------------------------------------------
# Hydrator — the plan's `Hydrator.load(config) -> HydratedCatalog`
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HydratedModel:
    id: str
    endpoint: str
    metadata: ModelMetadata | None


@dataclass(frozen=True, slots=True)
class HydratedCatalog:
    environment: str
    model: HydratedModel
    mcp_servers: tuple[McpServerEntry, ...]
    skills: tuple[ContextItem, ...]
    prompts: tuple[ContextItem, ...]
    artifacts: tuple[ContextItem, ...]
    a2a_agents: tuple[A2AAgentEntry, ...] = ()


EkResolver = Callable[[str], str]


def _default_ek_resolver(env_name: str) -> str:
    """Reads the Harness control pod's own process env (contract §5: SecretRefs become
    private control-pod env vars) — the real ek_ never leaves this process."""
    value = os.environ.get(env_name)
    if not value:
        raise HydrationError(f"ach.identity env {env_name!r} is unset")
    return value


class Hydrator:
    """`resolve_ek` is injected (tests supply a fixture; production reads the real env) —
    same DI convention as `Admission`'s injected `clock`/`runner` (harness/admission.py)."""

    def __init__(
        self,
        *,
        resolve_ek: EkResolver = _default_ek_resolver,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._resolve_ek = resolve_ek
        self._client = client

    async def load(self, config: RuntimeConfig) -> HydratedCatalog:
        identity = ProcessIdentity(agent=config.agent.name, environment=config.ach.environment)
        ek = self._resolve_ek(config.ach.identity.env)
        manifest = await fetch_hydration_manifest(
            config.ach.base_url, ek, config.ach.environment, identity=identity, client=self._client
        )
        entry = resolve_model(manifest, config.model.name)
        metadata = await fetch_model_metadata(
            config.ach.base_url, ek, config.model.name, client=self._client
        )
        return HydratedCatalog(
            environment=manifest.environment,
            model=HydratedModel(id=entry.id, endpoint=entry.endpoint, metadata=metadata),
            mcp_servers=tuple(manifest.runtime.mcp_servers),
            skills=tuple(manifest.context.skills),
            prompts=tuple(manifest.context.prompts),
            artifacts=tuple(manifest.context.artifacts),
            a2a_agents=tuple(manifest.runtime.a2a_agents),
        )


async def stage_context(
    catalog: HydratedCatalog,
    config: RuntimeConfig,
    root: "Path",
    *,
    client: httpx.AsyncClient,
    ek: str,
) -> None:
    """Download authorized context into the legacy on-disk skills/prompts layout."""
    import shutil
    import tempfile
    from pathlib import PurePosixPath
    from ach_runtime.storage.archive import extract

    root.mkdir(parents=True, exist_ok=True)
    excluded = set(config.ach.capability.filter.exclude.skills)
    headers = {"x-ach-key": ek}
    for kind in ("skills", "prompts", "artifacts"):
        items = getattr(catalog, kind)
        for item in items:
            if kind == "skills" and item.name in excluded:
                continue
            if not item.download_url:
                continue
            if not ek:
                raise HydrationError("ACH identity credential is not configured")
            path = PurePosixPath(item.name)
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise HydrationError("hydrated context item has an unsafe name")
            response = await client.get(item.download_url, headers=headers, timeout=_REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            if len(response.content) > 512 * 1024**2:
                raise HydrationError("hydrated context archive exceeds transfer byte limit")
            archive = root / f".ach-context-{kind}-{len(list(root.glob('.ach-context-*')))}.tar.gz"
            archive.write_bytes(response.content)
            scratch = Path(tempfile.mkdtemp(prefix=".ach-context-stage-", dir=root))
            try:
                staged = scratch / "payload"
                extract(archive, staged, max_expanded_bytes=4 * 1024**3, max_members=10_000)
                target = root / kind / path
                target.parent.mkdir(parents=True, exist_ok=True)
                if kind == "skills":
                    # ACH serves a skill's files at the archive root: nest them under
                    # skills/<name>/, stripping at most one wrapper directory, like ach-cli's
                    # skillstage.ContentDir.
                    content = staged
                    if not (staged / "SKILL.md").is_file():
                        content = next(
                            (d for d in sorted(staged.iterdir()) if (d / "SKILL.md").is_file()),
                            staged,
                        )
                    if target.exists():
                        raise HydrationError("hydrated skills contain a duplicate path")
                    shutil.copytree(content, target)
                else:
                    target.mkdir(parents=True, exist_ok=True)
                    for child in staged.iterdir():
                        destination = target / child.name
                        if destination.exists():
                            raise HydrationError("hydrated context contains a duplicate path")
                        if child.is_dir():
                            shutil.copytree(child, destination)
                        else:
                            shutil.copy2(child, destination)
            finally:
                shutil.rmtree(scratch, ignore_errors=True)
                archive.unlink(missing_ok=True)

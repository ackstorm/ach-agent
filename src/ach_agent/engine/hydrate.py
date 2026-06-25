# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)


class ModelEntry(BaseModel):
    """A hydrated model: ACH returns objects ``{id, endpoint}``, not bare strings.

    ``endpoint`` is the ACH compat URL serving this model (e.g. ``…/v1``); the
    localhost model proxy fronts it.
    """

    id: str
    endpoint: str = ""


class McpServer(BaseModel):
    id: str
    endpoint: str


class A2AAgent(BaseModel):
    id: str = ""
    endpoint: str = ""


class ContextItem(BaseModel):
    name: str = ""
    id: str = ""
    download_url: str = Field(default="", alias="downloadUrl")


class Context(BaseModel):
    skills: list[ContextItem] = Field(default_factory=list)
    prompts: list[ContextItem] = Field(default_factory=list)
    artifacts: list[ContextItem] = Field(default_factory=list)


class _Runtime(BaseModel):
    models: list[ModelEntry] = Field(default_factory=list)
    mcpServers: list[McpServer] = Field(default_factory=list)
    a2aAgents: list[A2AAgent] = Field(default_factory=list)


class HydrationManifest(BaseModel):
    environment: str = ""
    runtime: _Runtime = Field(default_factory=_Runtime)
    context: Context = Field(default_factory=Context)

    @property
    def models(self) -> list[str]:
        """The hydrated model ids (for membership checks / logging)."""
        return [m.id for m in self.runtime.models]

    @property
    def model_entries(self) -> list[ModelEntry]:
        """The full hydrated model objects (id + endpoint)."""
        return list(self.runtime.models)

    @property
    def mcp_servers(self) -> list[McpServer]:
        return self.runtime.mcpServers

    @property
    def a2a_agents(self) -> list[A2AAgent]:
        return self.runtime.a2aAgents


async def _post_hydrate(url: str, headers: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers=headers)
        r.raise_for_status()
        data: dict[str, Any] = r.json()
        return data


async def hydrate(base_url: str, ek: str) -> HydrationManifest:
    data = await _post_hydrate(f"{base_url.rstrip('/')}/platform/hydrate", {"x-ach-key": ek})
    return HydrationManifest.model_validate(data)


def resolve_model(manifest: HydrationManifest, name: str) -> ModelEntry | None:
    """Resolve a configured model name against the hydrated models.

    Returns the matching ``ModelEntry`` (so the caller can use its real endpoint).
    Hard-fail ``sys.exit(1)`` if the hydrated set is non-empty but ``name`` is absent.
    Returns ``None`` when the hydrated set is empty (local dev without ACH).
    """
    for entry in manifest.runtime.models:
        if entry.id == name:
            return entry
    if manifest.runtime.models:
        log.error("model not in hydrated models — exiting", name=name, available=manifest.models)
        sys.exit(1)
    return None

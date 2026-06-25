from __future__ import annotations

import sys
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)


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
    models: list[str] = Field(default_factory=list)
    mcpServers: list[McpServer] = Field(default_factory=list)
    a2aAgents: list[A2AAgent] = Field(default_factory=list)


class HydrationManifest(BaseModel):
    environment: str = ""
    runtime: _Runtime = Field(default_factory=_Runtime)
    context: Context = Field(default_factory=Context)

    @property
    def models(self) -> list[str]:
        return self.runtime.models

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


def resolve_model(manifest: HydrationManifest, name: str) -> None:
    if manifest.models and name not in manifest.models:
        log.error("model not in hydrated models — exiting", name=name, available=manifest.models)
        sys.exit(1)

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


def _sse_json(text: str) -> dict[str, Any]:
    """Parse the first JSON-RPC payload out of an MCP ``text/event-stream`` body.

    ACH MCP responses come back as ``event: message\\ndata: {json}``. Returns the
    decoded ``data`` dict (or ``{}`` if none / unparseable).
    """
    import json

    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                obj: dict[str, Any] = json.loads(line[5:].strip())
                return obj
            except (ValueError, TypeError):
                return {}
    return {}


async def list_mcp_tools(endpoint: str, ek: str) -> list[str]:
    """Best-effort: return the tool names an MCP server exposes for this ek.

    Runs the StreamableHTTP handshake (initialize → initialized → tools/list) and
    extracts ``result.tools[].name``. Never raises — returns ``[]`` on any error so a
    boot-time probe can warn without breaking startup. The ek is sent as ``x-ach-key``
    and never logged.
    """
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ach-agent", "version": "0"},
        },
    }
    headers = {
        "x-ach-key": ek,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(endpoint, headers=headers, json=init)
            r.raise_for_status()
            sid = r.headers.get("mcp-session-id", "")
            sess = {**headers, "mcp-session-id": sid} if sid else headers
            await c.post(
                endpoint,
                headers=sess,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            tr = await c.post(
                endpoint,
                headers=sess,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            tr.raise_for_status()
            tools = _sse_json(tr.text).get("result", {}).get("tools", [])
            return [str(t.get("name", "")) for t in tools if isinstance(t, dict)]
    except Exception:  # noqa: BLE001 — best-effort probe, never breaks boot
        return []


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

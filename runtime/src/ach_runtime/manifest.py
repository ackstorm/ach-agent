# SPDX-License-Identifier: MIT
"""The `run` manifest: the few fields an author writes, expanded into a workspace-v3 wire config.

With `ach:` the agent's capability is an ACH environment, plus any `mcpServers`; without it,
the capability is standalone: `model` names its own endpoint and `mcpServers` lists the tools.

Every local default lives in `_DEFAULTS`. Persistence and artifacts stay off, so the single-unit
agent needs no S3 store (`storage.config.storage_from_env` returns None).
"""

from __future__ import annotations

import copy
import re
import uuid
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config

KEY_ENV = "ACH_KEY"
URL_ENV = "ACH_URL"
LOCAL_NAMESPACE = "local"
TUI_CHANNEL = "tui"
TUI_WORKSPACE = "tui"
TUI_SESSION = "tui"
TUI_TOKEN_ENV = "ACH_SECRET_TUI"

# ponytail: idle timeouts are a day so the workspace the TUI is attached to is never retired
# under it; a dev session longer than that restarts `run`.
_DEFAULTS: dict[str, Any] = {
    "engine": {
        "env": {},
        "startupTimeoutSeconds": 60,
        "compaction": {"auto": True, "keep": {"tokens": 8000}, "buffer": 20000},
    },
    "limits": {
        "maxActiveWorkspaces": 1,
        "maxConcurrentInvocations": 1,
        "maxConcurrentScripts": 1,
        "maxInvocationSeconds": 600,
        "maxQueuedTotal": 5,
        "idempotencyWindowSeconds": 3600,
        "maxSteps": 50,
    },
    "workspace": {
        "idleTimeoutSeconds": 86400,
        "shutdownTimeoutSeconds": 30,
        "maxConcurrentSessions": 1,
        "persistence": {"type": "none"},
        "session": {"idleTimeoutSeconds": 86400},
    },
    "artifacts": {"enabled": False, "maxArtifactBytes": 104857600, "retentionDays": 0},
}

# The control looks a channel up when it runs an invocation, so `run`'s in-process warm-up
# needs this entry. Its webhook token is random per run and never leaves the process.
_TUI_CHANNEL: dict[str, Any] = {
    "name": TUI_CHANNEL,
    "type": "webhook",
    "source": "generic",
    "routing": {"workspaceKey": TUI_WORKSPACE, "sessionKey": TUI_SESSION},
    "webhook": {"auth": {"type": "header_token", "env": TUI_TOKEN_ENV, "header": "x-tui-token"}},
}


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class ManifestError(ValueError):
    """The manifest cannot be read or does not describe a valid agent."""


class AchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str | None = None  # read on the host by the `ach-agent` script, unused in the config
    environment: str = Field(min_length=1)


class PromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> PromptSpec:
        if (self.file is None) == (self.text is None):
            raise ValueError("prompt: give exactly one of 'file' or 'text'")
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ach: AchSpec | None = None
    # Passed through to capability.ach.model or capability.standalone.model; the RuntimeConfig
    # schema validates it.
    model: dict[str, Any]
    # Passed through to capability.<server>.mcpServers; with `ach:`, added to the environment's.
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list, alias="mcpServers")
    prompt: PromptSpec | None = None
    # Passed through: the RuntimeConfig schema validates the names.
    exclude: dict[str, list[str]] = Field(default_factory=dict)
    # Merged over the `limits` defaults; the RuntimeConfig schema validates the values.
    limits: dict[str, Any] = Field(default_factory=dict)
    # The RuntimeConfig schema validates the names.
    deny_tools: list[str] = Field(default_factory=list, alias="denyTools")

    @model_validator(mode="after")
    def _checks(self) -> Manifest:
        if self.ach is None and "baseUrl" not in self.model:
            raise ValueError("model.baseUrl is required without 'ach'")
        # The ach-agent script forwards each name as `docker run -e NAME`.
        if bad := [name for name in secret_env_names(self) if not _ENV_NAME.fullmatch(name)]:
            raise ValueError(f"headers: invalid env variable name {', '.join(map(repr, bad))}")
        return self


def secret_env_names(manifest: Manifest) -> list[str]:
    """The environment variables the manifest's `{env: NAME}` headers read."""
    groups = [manifest.model.get("headers") or {}, *(server.get("headers") or {} for server in manifest.mcp_servers)]
    return sorted({value["env"] for group in groups for value in group.values()
                   if isinstance(value, dict) and isinstance(value.get("env"), str)})


def load_manifest(path: Path) -> Manifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        return Manifest.model_validate(raw or {})
    except ValidationError as exc:
        raise ManifestError(f"{path.name}: {exc}") from exc


def _system_prompt(prompt: PromptSpec, base_dir: Path) -> dict[str, Any]:
    if prompt.text is not None:
        return {"type": "text", "text": prompt.text}
    path = base_dir / prompt.file
    try:
        return {"type": "text", "text": path.read_text(encoding="utf-8")}
    except OSError as exc:
        raise ManifestError(f"prompt file not found: {path}") from exc


def _capability(manifest: Manifest, base_url: str | None) -> dict[str, Any]:
    if manifest.ach is None:
        return {"server": "standalone", "standalone": {"model": manifest.model, "mcpServers": manifest.mcp_servers}}
    return {
        "server": "ach",
        "ach": {
            "baseUrl": base_url,
            "environment": manifest.ach.environment,
            "key": {"env": KEY_ENV},
            "model": manifest.model,
            "mcpServers": manifest.mcp_servers,
        },
    }


def manifest_to_wire(
    manifest: Manifest, *, base_dir: Path, name: str, base_url: str | None = None,
) -> dict[str, Any]:
    """The validated wire config for `manifest`; `base_url` is the Hub URL in `ach` mode.
    Raises ManifestError or ConfigError."""
    wire: dict[str, Any] = {
        "schemaVersion": "workspace-v3",
        "agent": {
            "name": name,
            "namespace": LOCAL_NAMESPACE,
            "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"ach-agent-run:{name}")),
        },
        "capability": {
            **_capability(manifest, base_url),
            "filter": {"exclude": {"tools": [], "mcpServers": [], "skills": [], **manifest.exclude}},
        },
        **copy.deepcopy(_DEFAULTS),
        "channels": [copy.deepcopy(_TUI_CHANNEL)],
    }
    wire["limits"].update(manifest.limits)
    wire["engine"]["denyTools"] = manifest.deny_tools
    if manifest.prompt is not None:
        wire["prompt"] = {"system": _system_prompt(manifest.prompt, base_dir)}
    wire["configVersion"] = compute_config_version(wire)
    parse_config(wire)
    return wire

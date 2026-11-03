# SPDX-License-Identifier: MIT
"""The `run` manifest: the few fields an author writes, expanded into a workspace-v3 wire config.

Every local default lives in `_DEFAULTS`. Persistence and artifacts stay off, so the single-unit
agent needs no S3 store (`storage.config.storage_from_env` returns None).
"""

from __future__ import annotations

import copy
import uuid
from pathlib import Path
from typing import Any, Literal

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
        "persistence": {"enabled": False, "retentionDays": 0},
        "session": {"idleTimeoutSeconds": 86400, "persistence": {"enabled": False, "retentionDays": 0}},
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


class ManifestError(ValueError):
    """The manifest cannot be read or does not describe a valid agent."""


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    type: Literal["openai", "gemini", "anthropic"]


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

    ach: AchSpec
    model: ModelSpec
    prompt: PromptSpec | None = None
    # Passed through: the RuntimeConfig schema validates the names.
    exclude: dict[str, list[str]] = Field(default_factory=dict)


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


def manifest_to_wire(manifest: Manifest, *, base_dir: Path, name: str, base_url: str) -> dict[str, Any]:
    """The validated wire config for `manifest`; raises ManifestError or ConfigError."""
    wire: dict[str, Any] = {
        "schemaVersion": "workspace-v3",
        "agent": {
            "name": name,
            "namespace": LOCAL_NAMESPACE,
            "uid": str(uuid.uuid5(uuid.NAMESPACE_URL, f"ach-agent-run:{name}")),
        },
        "capability": {
            "server": "ach",
            "ach": {
                "baseUrl": base_url,
                "environment": manifest.ach.environment,
                "key": {"env": KEY_ENV},
                "model": {
                    "name": manifest.model.name,
                    "type": manifest.model.type,
                    "params": {},
                    "thinking": {"enabled": False, "effort": None},
                },
            },
            "filter": {"exclude": {"tools": [], "mcpServers": [], "skills": [], **manifest.exclude}},
        },
        **copy.deepcopy(_DEFAULTS),
        "channels": [copy.deepcopy(_TUI_CHANNEL)],
    }
    if manifest.prompt is not None:
        wire["prompt"] = {"system": _system_prompt(manifest.prompt, base_dir)}
    wire["configVersion"] = compute_config_version(wire)
    parse_config(wire)
    return wire

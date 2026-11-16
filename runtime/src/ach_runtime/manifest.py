# SPDX-License-Identifier: Apache-2.0
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
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config

KEY_ENV = "ACH_KEY"
URL_ENV = "ACH_URL"
DEFAULT_MODEL_API = "openai"  # the Agent CR's default (operator DefaultModelType)
# Top-level keys whose Agent CR field sits elsewhere, named in the error for a manifest that uses them.
_CR_NAMES = {"exclude": "filter.exclude", "denyTools": "engine.denyTools"}
LOCAL_NAMESPACE = "local"
TUI_CHANNEL = "tui"
TUI_WORKSPACE = "tui"
TUI_SESSION = "tui"
TUI_TOKEN_ENV = "ACH_SECRET_TUI"
LOCAL_SKILL_TYPE = "dir"
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")

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


class SystemPromptSpec(BaseModel):
    """The Agent CR's `prompt.system`. A `file` is relative to agent.yaml and sent as text."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text", "file", "ach"]
    text: str | None = None
    file: str | None = None
    ach: str | None = None
    ach_file: str | None = Field(default=None, alias="achFile")

    @model_validator(mode="after")
    def _block_matches_type(self) -> SystemPromptSpec:
        if getattr(self, self.type) is None:
            raise ValueError(f"prompt.system: type {self.type} needs '{self.type}'")
        return self


class PromptSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: SystemPromptSpec
    compose: Literal["append", "replace"] | None = None


class EngineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["opencode", "pi"] = "opencode"
    # The RuntimeConfig schema validates the names.
    deny_tools: list[str] = Field(default_factory=list, alias="denyTools")


class FilterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Passed through: the RuntimeConfig schema validates the names.
    include: dict[str, list[str]] = Field(default_factory=dict)
    exclude: dict[str, list[str]] = Field(default_factory=dict)


class Manifest(BaseModel):
    """agent.yaml: every field has the Agent CR's name and shape, so `promote` copies it."""

    model_config = ConfigDict(extra="forbid")

    ach: AchSpec | None = None
    engine: EngineSpec = Field(default_factory=EngineSpec)
    # Passed through to capability.ach.model or capability.standalone.model, with `api` renamed
    # `type`; the RuntimeConfig schema validates it.
    model: dict[str, Any]
    # Passed through to capability.<server>.mcpServers; with `ach:`, added to the environment's.
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list, alias="mcpServers")
    # Archives of skills, passed through to capability.standalone.skills; with `ach:` they come from the
    # environment. `type: dir` entries are local folders with either: see `local_skill_dirs`.
    skills: list[dict[str, Any]] = Field(default_factory=list)
    prompt: PromptSpec | None = None
    filter: FilterSpec = Field(default_factory=FilterSpec)
    # Merged over the `limits` defaults; the RuntimeConfig schema validates the values.
    limits: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _cr_names(cls, data: Any) -> Any:
        """Name the Agent CR field for a key that has another name or place there."""
        if not isinstance(data, dict):
            return data
        hints = [f"'{old}' is '{new}', as in the Agent CR" for old, new in _CR_NAMES.items() if old in data]
        if isinstance(data.get("model"), dict) and "type" in data["model"]:
            hints.append("'model.type' is 'model.api' (openai, gemini, anthropic), as in the Agent CR")
        if isinstance(data.get("engine"), str):
            hints.append("'engine: <name>' is 'engine: {type: <name>}', as in the Agent CR")
        prompt = data.get("prompt")
        if isinstance(prompt, dict) and {"file", "text"} & set(prompt):
            hints.append("'prompt.file' / 'prompt.text' are 'prompt.system: {type: file|text, ...}', as in the Agent CR")
        if hints:
            raise ValueError("; ".join(hints))
        return data

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
    groups = [manifest.model.get("headers") or {}, *(server.get("headers") or {} for server in (*manifest.mcp_servers, *manifest.skills))]
    return sorted({value["env"] for group in groups for value in group.values()
                   if isinstance(value, dict) and isinstance(value.get("env"), str)})


def _archive_skills(manifest: Manifest) -> list[dict[str, Any]]:
    return [entry for entry in manifest.skills if entry.get("type") != LOCAL_SKILL_TYPE]


def local_skill_dirs(manifest: Manifest, base_dir: Path) -> dict[str, Path]:
    """The `type: dir` skills: folders beside the manifest, copied into the context on every run.
    They never reach the wire config or the Agent CR; production gets its skills elsewhere."""
    found: dict[str, Path] = {}
    for entry in manifest.skills:
        if entry.get("type") != LOCAL_SKILL_TYPE:
            continue
        name = str(entry.get("name", ""))
        if set(entry) != {"name", "type", "path"} or not _SKILL_NAME.fullmatch(name):
            raise ManifestError(f"skills {name!r}: type dir takes a lowercase name and a path, nothing else")
        if name in found:
            raise ManifestError(f"skills {name!r}: listed twice")
        path = (base_dir / str(entry["path"])).resolve()
        if not (path / "SKILL.md").is_file():
            raise ManifestError(f"skills {name!r}: no SKILL.md in {path}")
        found[name] = path
    return found


def load_manifest(path: Path) -> Manifest:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    try:
        return Manifest.model_validate(raw or {})
    except ValidationError as exc:
        raise ManifestError(f"{path.name}: {exc}") from exc


def _system_prompt(system: SystemPromptSpec, base_dir: Path) -> dict[str, Any]:
    """The runtime's `prompt.system`: a file is read and sent as text, `achFile` becomes `file`."""
    if system.type == "text":
        return {"type": "text", "text": system.text}
    if system.type == "ach":
        return {"type": "ach", "ach": system.ach, **({"file": system.ach_file} if system.ach_file else {})}
    path = base_dir / str(system.file)  # set: the validator requires the block its type names
    try:
        return {"type": "text", "text": path.read_text(encoding="utf-8")}
    except OSError as exc:
        raise ManifestError(f"prompt file not found: {path}") from exc


def _wire_model(model: dict[str, Any]) -> dict[str, Any]:
    """agent.yaml and the Agent CR name the model protocol `api`; the wire config names it `type`."""
    wire = copy.deepcopy(model)
    wire["type"] = wire.pop("api", DEFAULT_MODEL_API)
    return wire


def _capability(manifest: Manifest, base_url: str | None) -> dict[str, Any]:
    model = _wire_model(manifest.model)
    if manifest.ach is None:
        return {"server": "standalone", "standalone": {
            "model": model, "mcpServers": manifest.mcp_servers, "skills": _archive_skills(manifest)}}
    if _archive_skills(manifest):
        raise ManifestError("skill archives need a standalone manifest; with ach: they come from the environment "
                            "(type dir works with both)")
    return {
        "server": "ach",
        "ach": {
            "baseUrl": base_url,
            "environment": manifest.ach.environment,
            "key": {"env": KEY_ENV},
            "model": model,
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
            "filter": {
                "include": {"tools": [], **manifest.filter.include},
                "exclude": {"tools": [], "mcpServers": [], "skills": [], **manifest.filter.exclude},
            },
        },
        **copy.deepcopy(_DEFAULTS),
        "channels": [copy.deepcopy(_TUI_CHANNEL)],
    }
    wire["limits"].update(manifest.limits)
    wire["engine"]["denyTools"] = manifest.engine.deny_tools
    if manifest.engine.type != "opencode":
        wire["engine"]["type"] = manifest.engine.type
    if manifest.prompt is not None:
        prompt: dict[str, Any] = {"system": _system_prompt(manifest.prompt.system, base_dir)}
        if manifest.prompt.compose is not None:
            prompt["compose"] = manifest.prompt.compose
        wire["prompt"] = prompt
    wire["configVersion"] = compute_config_version(wire)
    parse_config(wire)
    return wire

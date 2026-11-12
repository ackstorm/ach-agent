# SPDX-License-Identifier: Apache-2.0
"""`promote`: the Agent CR for a manifest, from `agent.yaml` plus `deploy.yaml`.

agent.yaml uses the CR's field names, so its fields are copied as written, with three changes: `ach.profile`
is dropped, each `{env: NAME}` header becomes a `secretKeyRef` into `deploy.yaml`'s `secretName`, and a
`prompt.system` file is inlined as text. `model` and `mcpServers` go under `spec.ach`, or under
`spec.standalone` for a manifest with no `ach:`. `deploy.yaml` is a partial Agent CR (`metadata`, `spec`) plus
`secretName`; a field set in both files is an error. No environment variable is read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ach_runtime.config.schema import ConfigError
from ach_runtime.manifest import ManifestError, _system_prompt, load_manifest, manifest_to_wire

API_VERSION = "ach-agent.ackstorm.ai/v1alpha1"
_DEPLOY_KEYS = {"metadata", "secretName", "spec"}
_COPIED = ("engine", "filter", "prompt", "limits")
# Validation needs a Hub URL; deploy.yaml usually has the real one.
_PLACEHOLDER_BASE_URL = "https://ach.invalid"


class PromoteError(ValueError):
    """The manifest and deploy file do not make an Agent CR."""


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    # A prompt reads as a block in the GitOps diff, not as one escaped line.
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


_Dumper.add_representer(str, _str)


def _headers(headers: dict[str, Any], secret_name: str | None, where: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for header, value in headers.items():
        if isinstance(value, dict) and "env" in value:
            if not secret_name:
                raise PromoteError(f"{where}: header {header} reads {value['env']}; set secretName in deploy.yaml")
            out[header] = {"secretKeyRef": {"name": secret_name, "key": value["env"]}}
        else:
            out[header] = value
    return out


def _merge(base: dict[str, Any], extra: dict[str, Any], path: str) -> dict[str, Any]:
    out = dict(base)
    for key, value in extra.items():
        if key not in out:
            out[key] = value
        elif isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value, f"{path}.{key}")
        else:
            raise PromoteError(f"{path}.{key} is set in both agent.yaml and deploy.yaml")
    return out


def _read_deploy(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PromoteError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise PromoteError(f"{path.name}: expected a mapping")
    if unknown := sorted(set(raw) - _DEPLOY_KEYS):
        raise PromoteError(f"{path.name}: unknown keys {', '.join(unknown)} (expected metadata, secretName, spec)")
    return raw


def promote(config: Path, deploy: Path, *, name: str) -> str:
    """The Agent CR YAML for `config` (agent.yaml) and `deploy` (deploy.yaml)."""
    manifest = load_manifest(config)
    raw = _read_deploy(deploy)
    deploy_ach = (raw.get("spec") or {}).get("ach") or {}
    try:  # what `run` accepts: the runtime schema, as the agent's config at boot
        manifest_to_wire(manifest, base_dir=config.parent, name=name,
                         base_url=deploy_ach.get("baseUrl") or _PLACEHOLDER_BASE_URL)
    except ConfigError as exc:
        raise PromoteError(f"{config.name}: {exc}") from exc
    secret_name = raw.get("secretName")
    written = manifest.model_dump(by_alias=True, exclude_unset=True)

    server = "standalone" if manifest.ach is None else "ach"
    capability: dict[str, Any] = {} if manifest.ach is None else {"environment": manifest.ach.environment}
    capability["model"] = model = written["model"]
    if model.get("headers"):
        model["headers"] = _headers(model["headers"], secret_name, "model")
    if servers := written.get("mcpServers"):
        for entry in servers:
            if entry.get("headers"):
                entry["headers"] = _headers(entry["headers"], secret_name, f"mcpServers.{entry.get('name')}")
        capability["mcpServers"] = servers
    if server == "standalone" and (skills := written.get("skills")):
        for entry in skills:
            if entry.get("headers"):
                entry["headers"] = _headers(entry["headers"], secret_name, f"skills.{entry.get('name')}")
        capability["skills"] = skills
    spec: dict[str, Any] = {"capabilityServer": server, server: capability}
    spec.update({key: written[key] for key in _COPIED if key in written})
    if manifest.prompt is not None and manifest.prompt.system.type == "file":
        try:
            spec["prompt"]["system"] = _system_prompt(manifest.prompt.system, config.parent)
        except ManifestError as exc:
            raise PromoteError(str(exc)) from exc

    cr = {
        "apiVersion": API_VERSION,
        "kind": "Agent",
        "metadata": {"name": name, **(raw.get("metadata") or {})},
        "spec": _merge(spec, raw.get("spec") or {}, "spec"),
    }
    return yaml.dump(cr, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=1000)

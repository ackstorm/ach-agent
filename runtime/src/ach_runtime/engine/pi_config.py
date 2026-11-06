# SPDX-License-Identifier: MIT
"""Translates `PublicConfig` into pi's agent-directory files, flags and env."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ach_runtime.config.public import PublicConfig
from ach_runtime.engine.config import GRANT_KEYS, base_env, to_fixture

PROVIDER_ID = "ach"
FIXED_ARGS = ["--no-approve", "-ne", "-e", "builtin:mcp", "--offline", "--no-skills", "--no-context-files"]


def agent_dir(home: Path) -> Path:
    return home / ".pi" / "agent"


@dataclass(frozen=True)
class PiConfig:
    models: dict[str, Any]
    mcp: dict[str, Any]
    settings: dict[str, Any]
    args: list[str]
    system_prompt: str | None


def _model_entry(public: PublicConfig, metadata: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": public.model.name}
    if limit := metadata.get("limit"):
        entry.update(contextWindow=limit["context"], maxTokens=limit["output"])
    if cost := metadata.get("cost"):
        entry["cost"] = {
            "input": cost.get("input", 0), "output": cost.get("output", 0),
            "cacheRead": cost.get("cache_read", 0), "cacheWrite": 0,
        }
    if modalities := (metadata.get("modalities") or {}).get("input"):
        entry["input"] = [kind for kind in modalities if kind in ("text", "image")]
    if public.model.thinking.enabled:
        entry["reasoning"] = True
    if params:
        entry["samplingParams"] = params
    return entry


def to_pi_config(
    public: PublicConfig, *, home: Path, catalog: dict[str, Any] | None, mcp: dict[str, Any] | None,
    resolved_system_prompt: str | None, provider_base_url_override: str | None = None,
) -> PiConfig:
    """Raises ValueError for a config pi cannot honour."""
    if public.model.type != "openai":
        # pi's SDKs add /v1beta and /v1 to baseUrl, and check_model_request rejects those paths.
        raise ValueError(f"model.type {public.model.type!r} is not supported by the pi engine: use openai")
    params = {key: value for key, value in public.model.params.items() if key not in GRANT_KEYS}
    fixture = to_fixture(public, catalog=catalog, resolved_system_prompt=resolved_system_prompt)
    provider = fixture["providers"][public.model.type]
    entry = _model_entry(public, provider["models"][public.model.name], params)
    models = {"providers": {PROVIDER_ID: {
        "baseUrl": provider_base_url_override or provider["baseURL"],
        "api": "openai-completions",
        "apiKey": provider["apiKey"],
        "authHeader": True,
        "headers": provider.get("headers", {}),
        "models": [entry],
    }}}
    servers = mcp or fixture["mcp"]
    compaction = public.engine.compaction
    thinking = public.model.thinking
    args = [
        "--provider", PROVIDER_ID, "--model", public.model.name, *FIXED_ARGS,
        "--thinking", thinking.effort if thinking.enabled and thinking.effort else "off",
    ]
    if public.engine.deny_tools:
        args += ["--exclude-tools", ",".join(public.engine.deny_tools)]
    if (skills := home / ".ach-state" / "skills").is_dir():
        args += ["--skill", str(skills)]
    prompt = fixture.get("systemPrompt")
    if prompt is not None:
        flag = "--system-prompt" if fixture.get("promptCompose") == "replace" else "--append-system-prompt"
        args += [flag, str(agent_dir(home) / "system-prompt.md")]
    return PiConfig(
        models=models,
        mcp={"mcpServers": {
            name: {"url": server["url"], "headers": server.get("headers", {}), "exposure": "direct"}
            for name, server in servers.items()
        }},
        settings={"compaction": {
            "enabled": compaction.auto, "keepRecentTokens": compaction.keep.tokens, "reserveTokens": compaction.buffer,
        }},
        args=args,
        system_prompt=prompt,
    )


def write_pi_config(config: PiConfig, home: Path) -> None:
    """Rewrites every file on each start: agent-modified copies never survive. `settings.json`
    must exist before an interactive start, or PiG blocks on its first-run dialog."""
    directory = agent_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in (("models.json", config.models), ("mcp.json", config.mcp), ("settings.json", config.settings)):
        (directory / name).write_text(json.dumps(content), encoding="utf-8")
    if config.system_prompt is not None:
        (directory / "system-prompt.md").write_text(config.system_prompt, encoding="utf-8")


def build_pi_env(*, home: Path, scratch: Path, approved_env: dict[str, str]) -> dict[str, str]:
    env = base_env(home=home, scratch=scratch, approved_env=approved_env)
    directory = str(agent_dir(home))
    env.update({
        "PI_CODING_AGENT_DIR": directory, "PIG_CODING_AGENT_DIR": directory,
        "PI_OFFLINE": "1", "PIG_OFFLINE": "1", "PI_TELEMETRY": "0",
    })
    return env

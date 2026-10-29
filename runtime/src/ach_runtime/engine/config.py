# SPDX-License-Identifier: MIT
"""OpenCode v2 subprocess env + managed-config translation (contract §§4-5).

Reuses the clean-slate-env discipline from old-ach-agent/src/ach_agent/engine/lifecycle.py
`_OPENCODE_ENV_ALLOWLIST`/`build_opencode_env`: the engine does NOT inherit the mini-harness
environment. It receives only a benign CLI-basics allowlist plus the producer-approved
literal names/values already validated by `ach_runtime.config.public.public_config`
(contract §5: "en v1 solo valores literales pueden llegar por engine.forwardEnv") — the real
ek_ never reaches this map because the model/MCP endpoints here are harness-hosted facade
URLs, not provider URLs needing a credential.
"""

from __future__ import annotations

import os
import copy
from pathlib import Path
from typing import Any

from ach_runtime.config.public import PublicConfig
from ach_runtime.config.schema import PromptBlock

_ENGINE_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "SHELL", "USER", "LOGNAME", "HOSTNAME", "LANG", "LANGUAGE", "TERM", "TZ"}
)

# The plugin directory baked into the devtools/runtime image (Dockerfile.devtools
# `python-execution` stage: `COPY plugins/ ./plugins/` under WORKDIR /work).
MANAGED_CONFIG_PLUGIN_DIR = Path(
    os.environ.get("ACH_MANAGED_CONFIG_PLUGIN_DIR", "/work/plugins/managed-config")
)


def build_env(*, home: Path, scratch: Path, approved_env: dict[str, str], password: str) -> dict[str, str]:
    """Clean-slate env for one `opencode2 serve --stdio` subprocess.

    Pins HOME and all four XDG paths under the session's private directory (never the
    shared Workspace), scratch/TMPDIR off the persistent volume, the private server
    password, and the two project-config-discovery kill switches — `wid` (the shared
    Workspace directory) is untrusted content, and OPENCODE_DISABLE_PROJECT_CONFIG=1 stops
    opencode from reading a config file out of it.
    """
    env = {name: os.environ[name] for name in _ENGINE_ENV_ALLOWLIST if name in os.environ}
    env.update(approved_env)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["TMPDIR"] = str(scratch)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["OPENCODE_SERVER_PASSWORD"] = password
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    env["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    return env


def to_fixture(
    public: PublicConfig,
    *,
    managed_agent: str = "build",
    catalog: dict[str, Any] | None = None,
    resolved_system_prompt: str | None = None,
    lookup_only_prompt: bool = False,
) -> dict[str, Any]:
    """Translate the accepted `PublicConfig` into the managed-config plugin's fixture shape.

    MCP entries are left empty here: the per-session facade capability (contract §5, "la
    fachada per-session se emite en Task 7") is not yet fixtured — an engine adapter caller
    that has one injects it via `fixture["mcp"]` before launch.

    Thinking and explicit model parameters become per-request options for the managed agent.
    A file or ACH prompt is resolved by its owning caller and passed through the private
    `resolved_system_prompt` seam; this adapter never reads prompt files or fetches prompts.
    """
    model_params = dict(public.model.params)
    if catalog is None:
        model_params.setdefault("tool_call", True)
        providers = {
            public.model.type: {
                "baseURL": public.infrastructure.facade_endpoint,
                "apiKey": "local-proxy",
                "models": {public.model.name: model_params},
            }
        }
        mcp: dict[str, Any] = {}
    else:
        providers = copy.deepcopy(catalog["providers"])
        provider = providers[public.model.type]
        models = provider["models"]
        model_metadata = dict(models[public.model.name])
        grant_keys = {"tool_call", "cost", "limit", "modalities", "attachment", "reasoning"}
        model_metadata.update({key: value for key, value in model_params.items() if key not in grant_keys})
        if "tool_call" not in model_metadata:
            model_metadata["tool_call"] = True
        models[public.model.name] = model_metadata
        mcp = copy.deepcopy(catalog["mcp"])
    fixture: dict[str, Any] = {
        "managedAgent": managed_agent,
        "maxSteps": public.limits.max_steps,
        "denyTools": list(public.engine.deny_tools),
        "providers": providers,
        "mcp": mcp,
        "thinking": {
            "enabled": public.model.thinking.enabled,
            "effort": public.model.thinking.effort,
        },
    }
    request_options = _thinking_options(
        public.model.type, public.model.thinking.enabled, public.model.thinking.effort
    )
    # Catalog metadata fields are grants and are deliberately not request parameters.
    grant_keys = {"tool_call", "cost", "limit", "modalities", "attachment", "reasoning"}
    request_options.update({key: value for key, value in model_params.items() if key not in grant_keys})
    fixture["requestOptions"] = request_options
    persona: str | None = None
    if public.prompt is not None and public.prompt.system is not None:
        # SystemPrompt is a discriminated union (text/file/ach); only the inline-text case
        # is resolvable here without a file-read or ACH-hosted-prompt fetch this module has
        # no authority to perform — file/ach carry through as an explicit marker instead of
        # a silently-dropped value. See the Task 4 report's "Accepted limitations".
        system = public.prompt.system
        if system.type == "text":
            persona = system.text
        elif resolved_system_prompt is not None:
            persona = resolved_system_prompt
        elif not lookup_only_prompt:
            raise ValueError(f"required {system.type} system prompt must be resolved before engine start")
        fixture["promptCompose"] = public.prompt.compose
    # I7: the active memory tools instructions, composed once with the resolved persona
    # (never erasing a required-file failure above, never mutating the base PublicConfig)
    # — present for the text/file/ACH persona forms above AND the no-persona form.
    memory_tools_spec = catalog.get("memoryToolsSpec") if catalog is not None else None
    if memory_tools_spec:
        memory_section = f"## Memory Tools\n{memory_tools_spec}"
        persona = f"{persona}\n\n{memory_section}" if persona else memory_section
    if persona is not None:
        fixture["systemPrompt"] = persona
        # No `prompt` block (memory-tools-only persona): use the schema's `compose` default,
        # else the plugin applies neither append nor replace and the instructions are lost.
        fixture.setdefault("promptCompose", PromptBlock.model_fields["compose"].default)
    return fixture


def _thinking_options(model_type: str, enabled: bool, effort: str | None) -> dict[str, Any]:
    """Translate accepted thinking settings to released provider request options."""
    if not enabled or effort is None:
        return {}
    if model_type == "openai":
        return {"reasoningEffort": effort}
    if model_type == "gemini":
        return {"thinkingConfig": {"thinkingLevel": "high" if effort == "xhigh" else effort}}
    if model_type == "anthropic":
        budgets = {
            "minimal": 1024,
            "low": 4096,
            "medium": 10000,
            "high": 24576,
            "xhigh": 32000,
        }
        return {"thinking": {"type": "enabled", "budgetTokens": budgets[effort]}}
    return {}




def managed_content(public: PublicConfig, fixture: dict[str, Any]) -> dict[str, Any]:
    """The `OPENCODE_CONFIG` JSON body: managed plugin + the proven isolation flags.

    `snapshots: false` and `lsp: false` are the Task 1 spike's proven conservative defaults
    (contract §4) — v0.1.0 does not announce LSP support or a behaviourally-verified LSP
    disable, so this is retained as an internal setting, not a feature claim.
    """
    return {
        "snapshots": False,
        "lsp": False,
        "compaction": {
            "auto": public.engine.compaction.auto,
            "keep": {"tokens": public.engine.compaction.keep.tokens},
            "buffer": public.engine.compaction.buffer,
        },
        "plugins": [{"package": str(MANAGED_CONFIG_PLUGIN_DIR), "options": {"fixture": fixture}}],
    }

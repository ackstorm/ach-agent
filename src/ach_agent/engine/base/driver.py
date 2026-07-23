# SPDX-License-Identifier: Apache-2.0
"""EngineDriver seam — the symmetric abstraction over opencode and Pi (SP1 §4.2).

`router/lane.py` calls the engine only as the opaque injected `engine_runner`; it never
imports anything here (D-08 / RTR-06). All engine specifics live behind `EngineDriver`.
"""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ach_agent.engine.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer


@dataclass
class EngineConfig:
    """Rendered runtime config — engine section (CONTRACT.md §2).

    Fields extended by later plans as needed.
    """

    binary_path: str = "opencode"
    # Stable opencode HOME: holds .config/opencode (opencode.json + hydrated skills),
    # .local/share/opencode (sessions — persist because HOME is stable), and node_modules.
    home: str = ""
    work_dir: str = "/workspace"
    model: str = "gpt-4o-mini"  # opencode validates model names; must be a known OpenAI model ID
    # model.type — selects the opencode provider + wire (openai→ach/openai-compatible,
    # gemini→built-in google, anthropic→built-in anthropic). See _PROVIDER_BY_TYPE.
    model_type: str = "openai"
    params: dict[str, object] = field(default_factory=dict)  # model params (temperature, …)
    system_prompt: str = ""
    # prompt.compose (CONTRACT §2): "append" → persona via top-level `instructions` (after
    # opencode's model-default base prompt); "replace" → persona via `agent.build.prompt`
    # (instead of the default). Empty persona + replace falls back to append (never blank the base).
    compose: str = "append"
    steps: int = 50
    startup_timeout_seconds: int = 30
    max_invocation_seconds: int = 1800
    # MEM-01/D-02: optional MCP server URL for memory tools (present iff backend reachable).
    # Written to opencode.json before subprocess launch so the model either has memory tools
    # or does not — no runtime tool-registration API exists.
    mcp_servers: list[str] = field(default_factory=list)
    # Plan 2 (localhost-proxy / ek-hygiene): opencode.json points the model at this localhost
    # model-proxy baseURL (no ek_; the proxy injects it). Always set in a real boot — the
    # harness hard-fails without it (no direct-gateway fallback). Empty only in unit tests
    # that never invoke the model.
    model_base_url: str = ""
    # {server_id: "http://127.0.0.1:<port>/mcp/<id>"} from McpProxy — proxied external MCP
    # servers written into opencode.json's mcp block alongside any memory server.
    mcp_local_urls: dict[str, str] = field(default_factory=dict)
    # codemem (MEM/D-02): when set, opencode.json registers a LOCAL stdio MCP server that
    # opencode spawns as its own child: `codemem mcp --db-path <db>`. Empty → no codemem.
    # Static per-agent db path (operator config). Viewer is disabled via env (headless).
    codemem_db_path: str = ""
    # Stable codemem project namespace (config memory.codemem.project → CODEMEM_PROJECT env).
    # Required in config; carried here so the codemem MCP entry pins a consistent project.
    codemem_project: str = ""
    # Passthrough MCP servers (mcpServers type local|remote), pre-normalized to opencode.json
    # mcp.<name> entries by engine.mcp_passthrough.to_opencode_entry. opencode connects to these
    # DIRECTLY (not via the localhost proxy). Static per-agent (boot-computed from cfg.mcp_servers).
    extra_mcp_servers: dict[str, dict[str, object]] = field(default_factory=dict)
    # SEC-01 / ek-hygiene: extra env var NAMES the operator wants forwarded from the harness
    # env into the opencode subprocess (engine.forwardEnv). The opencode env is built
    # clean-slate from a small base allowlist (see build_opencode_env) — nothing else is
    # inherited — so the ek_ (ACH_TOKEN/ACH_API_KEY) never reaches opencode unless explicitly
    # named here. Use sparingly (e.g. a custom CA bundle path); never list the ek_.
    forward_env: list[str] = field(default_factory=list)
    # capability.filter.exclude.tools — opencode tool ids to disable in opencode.json
    # (agent.build.tools[<id>]=False), withholding them from the model.
    exclude_tools: list[str] = field(default_factory=list)

    # SP1: which driver runs this config. "opencode" | "pi". Selects the EngineDriver in
    # _make_engine_runner (main.py) and namespaces the pool sessions map (base/pool.py) so an
    # opencode ses_ id and a Pi session-file path never collide on a persisted home.
    engine_type: str = "opencode"


@dataclass
class TurnResult:
    """Result of ONE prompt turn (SP1 §4.3, the Fine boundary).

    `text` is the raw final assistant text — NOT validated here. `session_ref` is the
    engine-native handle the turn ran in (opencode: ``ses_…`` id; Pi: session-file path);
    ``base/terminal.py`` targets repair/wrap-up turns at it and post-turn hygiene keys on it.
    `aborted` is set when the step budget (``max_tool_calls``) cut the turn — such a turn
    usually lacks a terminal object, so ``base/terminal.py`` runs one wrap-up turn.
    """

    text: str
    session_ref: str
    aborted: bool = False


@runtime_checkable
class EngineDriver(Protocol):
    """Everything the harness needs from an engine, symmetric across opencode and Pi."""

    engine_type: str

    def skills_dir(self, home: Path) -> Path:
        """The SHARED skills extract dir under ``home``. No ``session_key``: hydration runs
        ONCE at boot (main.py:1240) before any key exists; every per-key config points here."""
        ...

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer: ...

    async def health(self, server: ManagedServer) -> bool: ...

    async def run_turn(
        self,
        server: ManagedServer,
        *,
        conv_key: str,
        prompt: str,
        reuse: bool,
        sessions: MutableMapping[str, str],
        session_ref: str | None = None,
        on_text: Callable[[str], None] | None,
        on_tool: Callable[[OpenCodeToolUpdate], None] | None,
        max_tool_calls: int,
        stats: dict[str, Any],
    ) -> TurnResult:
        """Run ONE prompt. If ``session_ref`` is given, continue exactly that engine session
        (repair/wrap-up) and bypass ``conv_key``/``reuse``/the map. Writes the final ref into
        ``stats['session_ref']`` (opencode also writes ``stats['oc_session_id']``)."""
        ...

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None: ...
    async def compact_session(self, server: ManagedServer, session_ref: str) -> None: ...
    async def stop(self, server: ManagedServer) -> None: ...

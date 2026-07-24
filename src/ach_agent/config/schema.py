# SPDX-License-Identifier: Apache-2.0
"""Full CONTRACT_v3 §2 Pydantic v2 config schema + hard-fail loader (CFG-01/02/03, D-01).

Models every block from the rendered runtime config. All blocks carry
ConfigDict(extra='forbid') so unknown keys cause a hard-fail at load time.

Constraint: NEVER import the router or any Hermes type here (RTR-06).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

log = structlog.get_logger(__name__)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ---------------------------------------------------------------------------
# Leaf / utility blocks
# ---------------------------------------------------------------------------


class AgentBlock(BaseModel):
    """CONTRACT §2 agent block."""

    model_config = ConfigDict(extra="forbid")

    name: str


class ThinkingBlock(BaseModel):
    """CONTRACT §2 model.thinking — normalized, engine-neutral reasoning intent.

    The canonical surface for "should this model think, and how hard". Each engine
    translates it (pi: models.json `reasoning` + `--thinking <effort>`; opencode:
    per-call providerOptions merged into the generated model options). Deliberately NOT
    model.params — params stays provider-specific per-call passthrough and wins on key
    collision with the generated translation.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = False
    effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None

    @model_validator(mode="after")
    def _effort_requires_enabled(self) -> ThinkingBlock:
        if self.effort is not None and not self.enabled:
            raise ValueError("model.thinking.effort requires model.thinking.enabled=true")
        return self


class ModelBlock(BaseModel):
    """CONTRACT_v3 §2 model block: provider-selecting name + open params."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str  # e.g. "openai.gpt-5"; passed verbatim
    type: Literal["openai", "gemini", "anthropic"]  # selects the ACH compat endpoint
    params: dict[str, Any] = Field(default_factory=dict)  # open, unvalidated, splatted to client
    thinking: ThinkingBlock = Field(default_factory=ThinkingBlock)


class LimitsBlock(BaseModel):
    """CONTRACT §2 limits block (§18.6 — all finite, always enforced)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_concurrent_invocations: int = Field(default=1, alias="maxConcurrentInvocations")
    max_invocation_seconds: int = Field(default=600, alias="maxInvocationSeconds")
    max_queued_total: int = Field(default=100, alias="maxQueuedTotal")
    idempotency_window_seconds: int = Field(default=3600, alias="idempotencyWindowSeconds")
    max_steps: int = Field(default=50, alias="maxSteps")
    terminal_output_retries: int = Field(default=1, alias="terminalOutputRetries")


class PiEngineBlock(BaseModel):
    """Pi-engine sub-block (consulted only when engine.type == 'pi').

    ONLY executable knobs live here. `binaryPath` pins the `pi` executable;
    `mcpAdapterPath` is the vendored pi-mcp-adapter package path referenced from Pi's
    settings.json `packages` (never a runtime `pi install`). Empty `mcpAdapterPath` → the
    driver falls back to the image's vendored default (SP2 pins it). Model identity and
    thinking/reasoning intent live in the model block (ModelBlock.thinking) — the
    v0.8.1-only `model`/`thinkingLevel` fields here were removed in v0.9.0.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    binary_path: str = Field(default="pi", alias="binaryPath")
    mcp_adapter_path: str = Field(default="", alias="mcpAdapterPath")


class EngineBlock(BaseModel):
    """Engine runtime knobs (harness-local; operator-optional).

    Carries "how we run opencode": its working directory, startup deadline, and the
    env-forwarding allowlist. opencode's subprocess env is built clean-slate from a small
    base allowlist (engine.lifecycle.build_opencode_env); ``forward_env`` lists extra var
    NAMES to forward — never the ek_ (ACH_TOKEN/ACH_API_KEY).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Empty by default — the harness derives the concrete paths at boot from
    # persistence (see ach_agent.main.resolve_engine_paths): persistence.enabled →
    # home=<mountPath>/home (persistent), else /tmp/ach-home (volatile); work_dir
    # defaults to <home>/workspace. Set either here to pin an explicit path.
    home: str = Field(default="", alias="home")
    work_dir: str = Field(default="", alias="workDir")
    startup_timeout_seconds: int = Field(default=30, alias="startupTimeoutSeconds")
    forward_env: list[str] = Field(default_factory=list, alias="forwardEnv")
    # Seconds an idle keyed opencode server lingers after its last release before being
    # stopped. Non-zero keeps the server warm so channel.session=auto persists the opencode
    # session (_sessions) across events for the same session_key instead of respawning. 0
    # restores spawn-per-invocation (stop as soon as the conversation ends).
    idle_ttl_seconds: float = Field(default=30.0, ge=0, alias="idleTtlSeconds")
    # Runaway control (Plan 4): abort a turn after this many DISTINCT tool calls, then run one
    # wrap-up turn so the model still returns a valid terminal object. 0 disables counting/abort;
    # maxInvocationSeconds remains the always-on time backstop. Recommend ~80 when opting in.
    max_tool_calls: int = Field(default=0, ge=0, alias="maxToolCalls")
    # SP1: which engine runs this agent. Canonical wire name is "pi" (runtime spec §7.4 amended
    # from the reserved "pymono"). Selects the EngineDriver in main._make_engine_runner.
    type: Literal["opencode", "pi"] = Field(default="opencode", alias="type")
    # Pi sub-block — only consulted when type == "pi"; optional so opencode configs never carry it.
    pi: PiEngineBlock | None = Field(default=None, alias="pi")


class PersistenceBlock(BaseModel):
    """CONTRACT §2 persistence block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    mount_path: str = Field(default="/var/lib/ach-agent", alias="mountPath")


class HealthBlock(BaseModel):
    """CONTRACT §2 health block."""

    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8080


class SystemText(BaseModel):
    """Inline persona text."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class SystemFile(BaseModel):
    """Persona sourced from a hydrated prompt file under <home>/.ach-state."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    file: str  # relative to <home>/.ach-state; absolute or ".." is rejected

    @field_validator("file")
    @classmethod
    def _no_escape(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("prompt.system.file must be a relative path under .ach-state, no '..'")
        return v


class SystemAch(BaseModel):
    """Persona sourced from a hydrated prompt by ACH name (harness resolves the file).

    ``ach`` is the hydrated prompt name → ``<home>/.ach-state/prompts/<ach>/``. ``file`` is
    an optional subpath within it; when empty the harness uses the prompt dir's sole file
    (and errors if the dir has 0 or >1 files). Convenience over ``file`` — the operator
    names the prompt, not its on-disk path.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["ach"]
    ach: str  # hydrated prompt name; maps to the prompts/<ach> dir. Absolute or ".." rejected;
    # "/" is allowed (a registry-qualified name is a nested dir), it just cannot escape upward.
    file: str = ""  # optional subpath under the prompt dir; empty → the sole file

    @field_validator("ach", "file")
    @classmethod
    def _no_escape(cls, v: str) -> str:
        if not v:
            return v
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("prompt.system ach/file must be a relative path, no '..'")
        return v

    @field_validator("ach")
    @classmethod
    def _ach_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt.system.ach must be a non-empty prompt name")
        return v


# Discriminated on `type`: the string shorthand is intentionally NOT accepted (CONTRACT_v3
# ADDENDUM-prompt-source §1 — the operator renders the object form).
SystemPrompt = Annotated[SystemText | SystemFile | SystemAch, Field(discriminator="type")]


class PromptBlock(BaseModel):
    """CONTRACT §2 prompt block."""

    model_config = ConfigDict(extra="forbid")

    # text | file | ach source; omitted → no persona (""). The plain-string form is rejected.
    system: SystemPrompt | None = None
    # CONTRACT §2 layering mode, rendered by the operator:
    #   append  → persona is appended AFTER opencode's model-default base prompt (top-level
    #             `instructions`); default.
    #   replace → persona REPLACES opencode's model-default base prompt (`agent.build.prompt`);
    #             env/mcp/skills/tools are unaffected (opencode `session/llm/request.ts`).
    # "replace" extends the accepted enum → must land lockstep with ach-runtime (the operator
    # renders `compose`). Was contract-reserved (append-only, accepted-not-executed).
    compose: Literal["append", "replace"] = "append"


class MentalModelSpec(BaseModel):
    """A pinned reflection the harness provisions into Hindsight at boot (CONTRACT §2)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    source_query: str = Field(alias="sourceQuery")
    auto_refresh: bool = Field(default=False, alias="autoRefresh")
    max_tokens: int = Field(default=2048, alias="maxTokens")


class HindsightParams(BaseModel):
    """Hindsight backend params — the ``memory.hindsight`` sub-block (CONTRACT §2)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint: str
    # Static memory bank_id (harness-owned; the agent never sees or sets it). Per-repo
    # partitioning is via tags, NEVER by templating bank from inbound payload (T-04-03).
    bank: str = ""
    # Admin secret for the harness→Hindsight path (Bearer). NOT the ek_. env-only; resolved
    # at use time; never logged / forwarded to opencode. OPTIONAL — omit when Hindsight is on
    # an internal/no-auth URL. If set but the env var is unset at runtime → fail-open degrade.
    auth: SecretSource | None = None
    # Optional mission string passed to create_bank at provisioning.
    mission: str = ""
    # Rich specs the harness provisions (create_mental_model) + reads (get_mental_model).
    mental_models: list[MentalModelSpec] = Field(default_factory=list, alias="mentalModels")

    @model_validator(mode="after")
    def _bank_static(self) -> HindsightParams:
        # T-04-03: bank is harness-owned + static — NEVER templated. Payload is untrusted (a
        # templated bank could select another tenant's memory), and the boot-started facade uses
        # the static bank, so a {{ }} bank would silently diverge from the mental-model fetch.
        if "{{" in self.bank:
            raise ValueError(
                "memory.hindsight.bank must be static — templating ({{ }}) is not allowed"
            )
        return self


class HindsightMemory(BaseModel):
    """CONTRACT §2 memory block — Hindsight backend (fail-open §31).

    Strict nested form: ``{type: hindsight, hindsight: {...}}``. There is NO backward-compat
    for a flat block or a missing ``type`` — the schema hard-fails (extra='forbid').
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["hindsight"]
    hindsight: HindsightParams


class CodememParams(BaseModel):
    """codemem backend params — the ``memory.codemem`` sub-block (CONTRACT §2)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Absolute path to the codemem SQLite DB. OMITTED → the harness derives it at boot from
    # persistence (<mountPath>/state/codemem.db when persistence.enabled, else
    # /tmp/ach-home/state/codemem.db). Set it to override. Operator config (trusted); never
    # templated per-repo, never from inbound payload.
    db_path: str | None = Field(default=None, alias="dbPath")
    # Stable project namespace (passed to codemem as CODEMEM_PROJECT). Fixed default so
    # remember + search always agree across sessions — codemem otherwise derives the project
    # from cwd (git repo root), and its fallbacks disagree on a non-git work_dir, silently
    # breaking cross-session recall. Override only if you know why.
    project: str = "ach-agent"

    @field_validator("db_path")
    @classmethod
    def _abs_no_escape(cls, v: str | None) -> str | None:
        if v is None:
            return v
        p = PurePosixPath(v)
        if not p.is_absolute() or ".." in p.parts:
            raise ValueError("memory.codemem.dbPath must be an absolute path with no '..'")
        return v


class CodememMemory(BaseModel):
    """CONTRACT §2 memory block — codemem backend (local stdio MCP, model-managed).

    Minimal form: ``{type: codemem}`` — the ``codemem`` sub-block and both its fields
    (``dbPath``, ``project``) are optional and derived/defaulted. Override via
    ``{type: codemem, codemem: {dbPath: ..., project: ...}}``. A flat ``dbPath`` at the
    ``memory`` level is rejected (extra='forbid').
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["codemem"]
    codemem: CodememParams = Field(default_factory=CodememParams)


# Strict discriminated union on `type` — `type` is REQUIRED (no default, no backward-compat
# coercion). An unknown/missing `type`, a flat block, or a mismatched sub-block hard-fails.
Memory = Annotated[HindsightMemory | CodememMemory, Field(discriminator="type")]


# ---------------------------------------------------------------------------
# mcpServers — harness-managed MCP servers (CONTRACT_v3 §2a, ADDENDUM-mcpservers)
# ---------------------------------------------------------------------------


class RepoCheckoutParams(BaseModel):
    """Params for the harness-hosted repoCheckout facade (the `checkout_repo` tool)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # The hydrated runtime.mcpServers[].id whose endpoint serves the
    # gitlab://{project}/archive/{ref} resource the harness reads (harness-side, with the ek_).
    source_mcp_server_id: str = Field(alias="sourceMcpServerId")
    tmp_base: str = Field(default="/tmp/gitlab", alias="tmpBase")
    ttl_seconds: float = Field(default=3600.0, ge=0, alias="ttlSeconds")


class RepoCheckoutServer(BaseModel):
    """INTERNAL: the harness HOSTS this MCP (FastMCP facade), injecting the ek_."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["repoCheckout"]
    repo_checkout: RepoCheckoutParams = Field(alias="repoCheckout")


class LocalMcpServer(BaseModel):
    """PASSTHROUGH: opencode LAUNCHES this as a stdio subprocess."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["local"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)  # env NAMES only; never the ek_


class RemoteMcpServer(BaseModel):
    """PASSTHROUGH: opencode CONNECTS directly to a remote MCP endpoint."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["remote"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)  # values are ${env:NAME} refs


# Strict discriminated union on `type` (mirror of the Memory union). Named *Config to avoid
# clashing with engine.hydrate.McpServer (the hydrated {id,endpoint} external server).
McpServerConfig = Annotated[
    RepoCheckoutServer | LocalMcpServer | RemoteMcpServer, Field(discriminator="type")
]


# ---------------------------------------------------------------------------
# Capability blocks (CONTRACT_v3 §2, D-05: ach-only)
# ---------------------------------------------------------------------------


class CapabilityAchBlock(BaseModel):
    """CONTRACT_v3 §2 capability.ach sub-block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Optional in the schema so a hand-authored / baked sample config can ship WITHOUT a
    # hardcoded ACH endpoint and have it supplied at runtime via the ACH_BASE_URL env var
    # (see load_config). Production always renders a concrete baseUrl from the CRD, and
    # load_config still hard-fails if neither the contract nor the env provides one.
    base_url: str = Field(default="", alias="baseUrl")
    # Optional: the EK already scopes the ACH environment server-side, so this is implicit
    # for hand-authored configs. The harness never reads it (it logs the environment from the
    # hydrate response, manifest.environment); the operator still renders it in production.
    environment: str = "platform"


class CapabilityFilterExcludeBlock(BaseModel):
    """CONTRACT_v3 §2 capability.filter.exclude sub-block — gate ABOVE the model."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list, alias="mcpServers")
    skills: list[str] = Field(default_factory=list)


class CapabilityFilterBlock(BaseModel):
    """CONTRACT_v3 §2 capability.filter sub-block."""

    model_config = ConfigDict(extra="forbid")

    exclude: CapabilityFilterExcludeBlock = Field(default_factory=CapabilityFilterExcludeBlock)


class CapabilityBlock(BaseModel):
    """CONTRACT_v3 §2 capability block (D-05: ach-only; direct → hard-fail)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["ach"] = "ach"
    ach: CapabilityAchBlock
    filter: CapabilityFilterBlock = Field(default_factory=CapabilityFilterBlock)


# ---------------------------------------------------------------------------
# Channel sub-blocks
# ---------------------------------------------------------------------------


class SecretSource(BaseModel):
    """CONTRACT_v3 §2 secret source — env-only (no disk secrets).

    env → the harness reads the value from os.environ[NAME] at use time (hardened default:
          dumpable=0 hides it from the co-resident agent; the NAME must NOT be in
          engine.forwardEnv — the harness strips it from the forwarded set + WARNs).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    env: str = Field(default="")

    @model_validator(mode="after")
    def _check(self) -> SecretSource:
        if not self.env:
            raise ValueError("secret.env is required")
        if not _ENV_NAME_RE.match(self.env):
            raise ValueError(f"secret.env is not a valid environment variable name: {self.env!r}")
        return self


def resolve_secret(src: SecretSource) -> str | None:
    """Resolve a SecretSource to its value at use time (never cached — rotation).

    Returns the stripped env value, or None if the env var is unset (fail closed).
    """
    val = os.environ.get(src.env)
    return val.strip() if val is not None else None


class WebhookAuthBlock(BaseModel):
    """CONTRACT_v3 §2 webhook.auth sub-block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["gitlab_token", "hmac", "header_token", "none"] = "hmac"
    # Exactly one of {env, file}; required unless type == "none".
    secret: SecretSource | None = None
    # For type=="header_token": the request header carrying the static shared secret
    # (constant-time compared against the resolved secret). Ignored for other types.
    header: str = Field(default="")

    @model_validator(mode="after")
    def _secret_required_unless_none(self) -> WebhookAuthBlock:
        if self.type != "none" and self.secret is None:
            raise ValueError(f"webhook.auth.secret is required for type={self.type!r}")
        return self


class WebhookBlock(BaseModel):
    """CONTRACT_v3 §2 webhook channel sub-block (deliver/deliverOnly removed)."""

    model_config = ConfigDict(extra="forbid")

    auth: WebhookAuthBlock = Field(default_factory=WebhookAuthBlock)

    # Which GitLab event kinds this channel ROUTES to the agent. None → all routable kinds
    # (merge_request, issue, note). Kinds not listed are accepted-and-ignored (HTTP 200), never
    # 422 — so GitLab does not auto-disable the hook. A note (comment) routes only when "note"
    # AND its noteable base kind (merge_request/issue) are both allowed.
    gitlab_events: list[Literal["merge_request", "issue", "note"]] | None = Field(
        default=None, alias="gitlabEvents"
    )

    # GitLab loop-guard: the GitLab username the agent posts AS (the egress PAT's user, NOT
    # agent.name — a distinct fact the operator must supply). When set, inbound gitlab events
    # authored by this user, plus gitlab-generated system notes, are dropped pre-enqueue (HTTP
    # 200 ignored) so the agent never re-triggers on its own comments/MRs. None → guard off
    # (the agent must then self-guard via prompt). gitlab source only.
    bot_username: str | None = Field(default=None, alias="botUsername")

    # GitLab actor allowlist: only these GitLab usernames may trigger the agent. None → no
    # filter (any author triggers). Non-empty → events authored by anyone NOT listed are
    # dropped pre-enqueue (HTTP 200 ignored). Applies to every routed kind (mr/issue/note).
    # gitlab source only.
    trigger_users: list[str] | None = Field(default=None, alias="triggerUsers")


class A2AAuthBlock(BaseModel):
    """CONTRACT §2 a2a.auth sub-block (§14.6 / §3 bearer discipline)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    header: str = Field(default="x-a2a-custom-api-key")
    secret: SecretSource | None = None

    @model_validator(mode="after")
    def _secret_required(self) -> A2AAuthBlock:
        if self.secret is None:
            raise ValueError("a2a.auth.secret is required")
        return self


class A2ABlock(BaseModel):
    """CONTRACT_v3 §2 a2a channel sub-block (CHN-05; async-only in v1)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["async"] = "async"
    auth: A2AAuthBlock = Field(default_factory=A2AAuthBlock)


class CronBlock(BaseModel):
    """CONTRACT_v3 §2 cron channel sub-block (CHN-02)."""

    model_config = ConfigDict(extra="forbid")

    schedule: str  # cron expression, e.g. "* * * * *"
    timezone: str = "UTC"  # IANA tz, e.g. "Europe/Madrid"

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        """Reject unknown IANA zones at parse time (trust boundary).

        Without this, an invalid tz surfaces only when CronScheduler builds
        ZoneInfo(v) at boot — an obscure ZoneInfoNotFoundError mid-startup.
        """
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v


class QueueBlock(BaseModel):
    """CONTRACT_v3 §2 queue channel sub-block (redis-only in v1, §7)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["redis"] = "redis"
    key: str
    ack_mode: Literal["onComplete"] = Field(default="onComplete", alias="ackMode")


# ---------------------------------------------------------------------------
# ChannelType + ChannelConfig
# ---------------------------------------------------------------------------

# D-02/D-06: unrecognized channel type → ValidationError → hard-fail
# slack/telegram removed; queue added per CONTRACT_v3 §2.
# NOTE: `tui` is NOT a channel — it is the `--tui` launch modifier (console mode that
# ignores configured channels). See main.py. So it is intentionally absent here.
ChannelType = Literal["webhook", "cron", "queue", "a2a"]


class SessionBlock(BaseModel):
    """channel.session — conversation identity + growth bounds.

    type: 'none'   → fresh opencode session per event, DELETEd post-turn (no residue);
          'auto'   → reuse the channel-derived session_key (per-MR for gitlab, name for cron…);
          'custom' → reuse the session under `key` ({{ }} template rendered per event).
    key: the {{ }} template (payload.* / internal.*). REQUIRED iff type=='custom',
         FORBIDDEN otherwise. An empty render falls back to 'none' behavior + WARN.
    max_tokens: when the previous turn's input_tokens exceed this, apply `overflow`
                (applies to 'auto'/'custom'; ignored for 'none').
    overflow: 'compact' → POST /session/{id}/compact in place (keeps memory);
              'rotate' → drop the LRU entry + DELETE the old session (fresh start).
    The router lane key (event.session_key) is NOT affected by any of this.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["auto", "none", "custom"] = "none"
    key: str | None = None
    max_tokens: int | None = Field(default=None, alias="maxTokens", gt=0)
    overflow: Literal["compact", "rotate"] = "compact"

    @model_validator(mode="after")
    def _check_key(self) -> SessionBlock:
        """`key` is the discriminated payload of type='custom': required there, banned elsewhere."""
        if self.type == "custom":
            if not (self.key and self.key.strip()):
                raise ValueError("session: type='custom' requires a non-empty 'key' template")
        elif self.key is not None:
            raise ValueError(
                f"session: 'key' is only valid with type='custom' (got type='{self.type}')"
            )
        return self


class ChannelConfig(BaseModel):
    """CONTRACT_v3 §2 channel entry. extra=forbid catches unknown channel-level keys."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    type: ChannelType  # Literal union rejects unknown types (CFG-03)
    concurrency: int = 1
    prompt: str | None = None
    session: SessionBlock = Field(default_factory=SessionBlock)
    source: Literal["gitlab", "github", "generic"] | None = None
    webhook: WebhookBlock | None = None
    cron: CronBlock | None = None
    queue: QueueBlock | None = None
    a2a: A2ABlock | None = None

    @field_validator("session", mode="before")
    @classmethod
    def _session_shorthand(cls, v: Any) -> Any:
        """YAML shorthand: `session: auto|none` ≡ `{type: <str>}`; any other string
        (a {{ }} template) ≡ `{type: custom, key: <str>}`."""
        if isinstance(v, str):
            if v in ("auto", "none"):
                return {"type": v}
            return {"type": "custom", "key": v}
        return v

    @model_validator(mode="after")
    def check_type_block_coherence(self) -> ChannelConfig:
        """D-04: enforce channel type↔sub-block coherence at config load time.

        The channel type names its required sub-block (type Literal == field name,
        1:1); every other type's block is forbidden. webhook additionally requires
        'source'. Raises ValueError (wrapped by Pydantic into ValidationError →
        sys.exit(1)).
        """
        t = self.type
        if getattr(self, t) is None:
            article = "an" if t == "a2a" else "a"
            raise ValueError(f"channel '{self.name}': type='{t}' requires {article} '{t}' block")
        if t == "webhook" and self.source is None:
            raise ValueError(f"channel '{self.name}': type='webhook' requires 'source' field")
        for foreign in ("webhook", "cron", "queue", "a2a"):
            if foreign != t and getattr(self, foreign) is not None:
                raise ValueError(f"channel '{self.name}': type='{t}' forbids '{foreign}' block")
        return self


# ---------------------------------------------------------------------------
# Root AgentConfig
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Full CONTRACT_v3 §2 rendered runtime config (D-01: modeled in one pass).

    ConfigDict(extra='forbid', strict=True) ensures unknown top-level keys
    cause a ValidationError that hard-fails the process (CFG-02).
    """

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    schema_version: Literal["1"] = Field(alias="schemaVersion")
    agent: AgentBlock
    model: ModelBlock
    capability: CapabilityBlock
    prompt: PromptBlock | None = None
    memory: Memory | None = None
    limits: LimitsBlock = Field(default_factory=LimitsBlock)
    engine: EngineBlock = Field(default_factory=EngineBlock)
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")
    persistence: PersistenceBlock = Field(default_factory=PersistenceBlock)
    health: HealthBlock = Field(default_factory=HealthBlock)
    channels: list[ChannelConfig] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _load_yaml(raw: str) -> Any:
    """Parse a YAML-authored contract into a plain dict (local dev convenience).

    Production always renders the contract to JSON via the operator; YAML is only a
    hand-authoring affordance for local dry-runs. Hard-fails (sys.exit 1) on malformed
    YAML, mirroring the JSON path's schema-mismatch behavior.
    """
    import yaml  # lazy: only needed when a .yaml/.yml contract is loaded

    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        log.error("config YAML parse error — exiting", error=str(exc))
        sys.exit(1)


def load_config(path: str) -> AgentConfig:
    """Load and validate the rendered runtime config (CFG-01/02/03).

    Accepts JSON (the rendered contract the operator emits) or, for local hand-authored
    dry-runs, YAML (`.yaml`/`.yml`) — both validate against the SAME schema, so a YAML
    file that loads will render to an equivalent JSON contract.

    Hard-fails with sys.exit(1) on schema mismatch or file-not-found.
    Never raises to the caller.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        log.error("config file not found — exiting", path=path)
        sys.exit(1)

    try:
        if path.endswith((".yaml", ".yml")):
            cfg = AgentConfig.model_validate(_load_yaml(raw))
        else:
            cfg = AgentConfig.model_validate_json(raw)
    except ValidationError as exc:
        log.error("config schema mismatch — exiting", errors=exc.errors())
        sys.exit(1)

    # ACH_BASE_URL env override (local-dev convenience). Lets the shipped sample/baked
    # configs omit a concrete ACH endpoint: the env var supplies it at runtime, and wins
    # if set so the same contract can be pointed at staging vs prod. In production the
    # operator renders baseUrl into the JSON contract and this env is simply absent.
    env_base = os.environ.get("ACH_BASE_URL", "").strip()
    if env_base:
        cfg.capability.ach.base_url = env_base
    if not cfg.capability.ach.base_url:
        log.error(
            "capability.ach.baseUrl is unset and ACH_BASE_URL is not in the environment — exiting"
        )
        sys.exit(1)
    return cfg

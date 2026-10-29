# SPDX-License-Identifier: MIT
"""workspace-v2 RuntimeConfig — the resolved wire contract (docs/schemas/ach-runtime-contract-v2.md).

Models the complete resolved camelCase wire object the operator emits at
`/etc/ach-runtime/config.json` (contract §5/§11): `agent`, `configVersion`, `ach`, `model`,
`engine`, `limits`, `workspace`, `artifacts`, `channels`, `prompt`, `hooks`, `memory`,
`infrastructure`. Every block carries `ConfigDict(extra="forbid")` — unknown keys hard-fail.

This is workspace-v2, independent of the old runtime's `schemaVersion: "1"` contract
(old-ach-agent/src/ach_agent/config/schema.py) — reused here only where the contract marks a
surface compatible (model/thinking/prompt/ach-memory/cron/queue/a2a shapes). Routing, Pi,
placement, session none/auto/custom and the old infra knobs are NOT ported: see contract §10.
"""

from __future__ import annotations

import re
import uuid as _uuid
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ach_runtime.config.canonical import (
    CanonicalizationError,
    compute_config_version,
    validate_json_value,
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ConfigError(ValueError):
    """Raised by parse_config on a wire/schema/configVersion mismatch."""


# ---------------------------------------------------------------------------
# Shared leaf blocks
# ---------------------------------------------------------------------------


class SecretAlias(BaseModel):
    """A private environment-variable NAME the Harness/control pod resolves — never a value.

    Wire shape for `ach.identity`, `handoff.secretEnv.<name>`: `{"env": "ACH_SECRET_..."}`.
    """

    model_config = ConfigDict(extra="forbid")

    env: str

    @field_validator("env")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _ENV_NAME_RE.match(v):
            raise ValueError(f"not a valid environment variable name alias: {v!r}")
        return v


class AgentIdentity(BaseModel):
    """`agent.{name,namespace,uid}` — namespace is an opaque, stable, host-assigned scope;
    uid is the canonical lowercase UUID."""

    model_config = ConfigDict(extra="forbid")

    name: str
    namespace: str
    # The pattern is schema-representable (review I2) and catches most malformed input (wrong
    # case, braces, missing hyphens); the field_validator below additionally parses it as a
    # real UUID, which the regex alone cannot: it also rejects an otherwise-shaped-but-invalid
    # 32-hex-digit value and is the actual source of truth.
    uid: str = Field(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )

    @field_validator("uid")
    @classmethod
    def _canonical_uuid(cls, v: str) -> str:
        try:
            parsed = _uuid.UUID(v)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError(f"agent.uid must be a canonical UUID: {v!r}") from exc
        if str(parsed) != v:
            raise ValueError(f"agent.uid must be the lowercase canonical UUID form: {v!r}")
        return v


# ---------------------------------------------------------------------------
# ach block (contract §5: baseUrl, environment, identity, capability.filter.exclude)
# ---------------------------------------------------------------------------


class AchCapabilityFilterExclude(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list, alias="mcpServers")
    skills: list[str] = Field(default_factory=list)


class AchCapabilityFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    exclude: AchCapabilityFilterExclude = Field(default_factory=AchCapabilityFilterExclude)


class AchCapabilityBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filter: AchCapabilityFilter = Field(default_factory=AchCapabilityFilter)


class AchBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    base_url: str = Field(alias="baseUrl")
    environment: str
    identity: SecretAlias
    capability: AchCapabilityBlock = Field(default_factory=AchCapabilityBlock)


# ---------------------------------------------------------------------------
# model block (contract §10: conservar; reused verbatim from old-ach-agent)
# ---------------------------------------------------------------------------


class ThinkingBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None

    @model_validator(mode="after")
    def _effort_requires_enabled(self) -> ThinkingBlock:
        if self.effort is not None and not self.enabled:
            raise ValueError("model.thinking.effort requires model.thinking.enabled=true")
        return self


class ModelBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    name: str
    type: Literal["openai", "gemini", "anthropic"]
    params: dict[str, Any] = Field(default_factory=dict)
    thinking: ThinkingBlock = Field(default_factory=ThinkingBlock)


# ---------------------------------------------------------------------------
# engine block — reserved forwardEnv names (contract §5/§11, task-2 dispatch)
# ---------------------------------------------------------------------------

# OPENCODE_* process-loader / catalog / project-discovery names observed in the Task 1
# spike (spikes/opencode-v2/probe.mjs, serve-fixture.mjs) plus the server password var.
# OPENCODE_ENABLE_EXA is NOT reserved — it is the one selected literal the PR example forwards.
_RESERVED_ENGINE_ENV_EXACT = frozenset(
    {
        "HOME",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_MODELS_URL",
        "OPENCODE_BIN",
    }
)
_RESERVED_ENGINE_ENV_PREFIXES = ("ACH_", "XDG_", "OPENCODE_CONFIG")


def _check_engine_env_name(name: str) -> None:
    if name in _RESERVED_ENGINE_ENV_EXACT or any(
        name.startswith(p) for p in _RESERVED_ENGINE_ENV_PREFIXES
    ):
        raise ValueError(f"engine.env name {name!r} is reserved")
    if not _ENV_NAME_RE.match(name):
        raise ValueError(f"engine.env name {name!r} is not a valid environment variable name")


class CompactionKeepBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tokens: int = Field(gt=0)


class CompactionBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auto: bool
    keep: CompactionKeepBlock
    buffer: int = Field(ge=0)


class EngineBlock(BaseModel):
    """Resolved `engine` — CR `forwardEnv` names have already become literal values.

    `env` carries only approved literal (non-secret) values selected by the operator from
    `spec.env`; SecretRefs never reach this map (contract §5: "en v1 solo valores literales
    pueden llegar por engine.forwardEnv").
    """

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    env: dict[str, str] = Field(default_factory=dict)
    startup_timeout_seconds: int = Field(default=30, gt=0, alias="startupTimeoutSeconds")
    compaction: CompactionBlock
    # Native OpenCode tools hidden from the model: each name becomes a permission deny rule
    # on the managed agent. MCP tools are governed by `ach.capability.filter.exclude.tools`.
    deny_tools: list[str] = Field(default_factory=list, alias="denyTools")

    @field_validator("deny_tools")
    @classmethod
    def _check_deny_tools(cls, v: list[str]) -> list[str]:
        for name in v:
            if not name or "." in name:
                raise ValueError(
                    f"engine.denyTools entry {name!r} must be a native OpenCode tool name; "
                    "MCP tools go in ach.capability.filter.exclude.tools"
                )
        if len(set(v)) != len(v):
            raise ValueError("engine.denyTools has duplicate names")
        return v

    @field_validator("env")
    @classmethod
    def _check_env(cls, v: dict[str, str]) -> dict[str, str]:
        for name in v:
            _check_engine_env_name(name)
        return v


# ---------------------------------------------------------------------------
# limits block (contract §3 — all finite, maxSteps/maxInvocationSeconds mandatory positive)
# ---------------------------------------------------------------------------


class LimitsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    max_active_workspaces: int = Field(gt=0, alias="maxActiveWorkspaces")
    max_concurrent_invocations: int = Field(gt=0, alias="maxConcurrentInvocations")
    max_concurrent_scripts: int | None = Field(default=None, gt=0, alias="maxConcurrentScripts")
    max_invocation_seconds: int = Field(gt=0, alias="maxInvocationSeconds")
    max_queued_total: int = Field(gt=0, alias="maxQueuedTotal")
    idempotency_window_seconds: int = Field(ge=0, alias="idempotencyWindowSeconds")
    max_steps: int = Field(gt=0, alias="maxSteps")
    # A running invocation with no engine progress (no new message part or tool-state
    # change) for this long fails with reason `stalled`, freeing its invocation slot.
    max_stall_seconds: int = Field(default=300, gt=0, alias="maxStallSeconds")


# ---------------------------------------------------------------------------
# workspace / artifacts blocks (contract §7/§8)
# ---------------------------------------------------------------------------


class PersistenceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    enabled: bool
    retention_days: int = Field(ge=0, alias="retentionDays")


class SessionPolicyBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    idle_timeout_seconds: int = Field(gt=0, alias="idleTimeoutSeconds")
    persistence: PersistenceBlock


class WorkspaceBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    idle_timeout_seconds: int = Field(gt=0, alias="idleTimeoutSeconds")
    shutdown_timeout_seconds: int = Field(gt=0, alias="shutdownTimeoutSeconds")
    ready_timeout_seconds: int = Field(default=30, gt=0, alias="readyTimeoutSeconds")
    max_concurrent_sessions: int = Field(gt=0, alias="maxConcurrentSessions")
    persistence: PersistenceBlock
    session: SessionPolicyBlock


class ArtifactsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    enabled: bool
    max_artifact_bytes: int = Field(gt=0, alias="maxArtifactBytes")
    retention_days: int = Field(ge=0, alias="retentionDays")


# ---------------------------------------------------------------------------
# prompt block (contract §10: conservar; reused verbatim from old-ach-agent)
# ---------------------------------------------------------------------------


class SystemText(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class SystemFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    file: str

    @field_validator("file")
    @classmethod
    def _no_escape(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError("prompt.system.file must be a relative path, no '..'")
        return v


class SystemAch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ach"]
    ach: str
    file: str = ""

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


SystemPrompt = Annotated[SystemText | SystemFile | SystemAch, Field(discriminator="type")]


class PromptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system: SystemPrompt | None = None
    compose: Literal["append", "replace"] = "append"


# ---------------------------------------------------------------------------
# hooks block (contract §6: sessionStart/sessionRestore/sessionSuspend)
# ---------------------------------------------------------------------------


class HookBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    script: str
    timeout_seconds: int = Field(default=120, gt=0, le=3600, alias="timeoutSeconds")

    @model_validator(mode="after")
    def _check(self) -> HookBlock:
        if not self.script.strip():
            raise ValueError("hook: 'script' must not be empty")
        return self


class HooksBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    # Workspace hooks run in /workspace: start after the workspace is restored on a fresh or
    # reactivated pod, stop on a clean idle close before the workspace snapshot.
    workspace_start: HookBlock | None = Field(default=None, alias="workspaceStart")
    # sessionStart is also the run gate: exit 78 skips the engine (outcome `skipped`) and its
    # stdout is appended to the prompt.
    session_start: HookBlock | None = Field(default=None, alias="sessionStart")
    session_restore: HookBlock | None = Field(default=None, alias="sessionRestore")
    session_suspend: HookBlock | None = Field(default=None, alias="sessionSuspend")
    workspace_stop: HookBlock | None = Field(default=None, alias="workspaceStop")


# ---------------------------------------------------------------------------
# memory block — ach-memory only (contract §10: codemem is out of the first pass)
# ---------------------------------------------------------------------------


class AchMemoryAuthAch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ach"]


class AchMemoryAuthBearer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["bearer"]
    env: str = Field(default="")
    header: str = Field(default="Authorization", pattern=r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")


AchMemoryAuth = Annotated[AchMemoryAuthAch | AchMemoryAuthBearer, Field(discriminator="type")]


class AchMemoryParams(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    endpoint: str = ""
    mcp_server_id: str = Field(default="", alias="mcpServerId")
    auth: AchMemoryAuth | None = None
    project: str = ""

    @model_validator(mode="after")
    def _one_location(self) -> AchMemoryParams:
        if bool(self.endpoint) == bool(self.mcp_server_id):
            raise ValueError("memory.achMemory needs exactly one of `endpoint` or `mcpServerId`")
        return self


class AchMemoryMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["ach-memory"]
    ach_memory: AchMemoryParams = Field(alias="achMemory")


# ---------------------------------------------------------------------------
# channels block — routing optional (contract §2), handoff/hooks (contract §6)
# ---------------------------------------------------------------------------

CONVERSATIONAL_GITLAB_EVENTS = frozenset({"merge_request", "issue", "note"})
ChannelType = Literal["webhook", "webhook-script", "cron", "queue", "a2a"]

# Env names the Harness pins for every handoff/script invocation (contract §6). A config entry
# with one of these names would be silently discarded — reject it instead.
_RESERVED_HANDOFF_ENV = frozenset(
    {"ACH_WORKSPACE_KEY", "ACH_SESSION_KEY", "ACH_EVENT_ID", "ACH_CHANNEL", "ACH_HANDOFF_DIR", "HOME"}
)
_RESERVED_HANDOFF_ENV_PREFIX = "ACH_EVENT_"


class RoutingBlock(BaseModel):
    """`channels[].routing` (contract §2) — both overrides optional and independent.

    An omitted override keeps the adapter default (generic webhook and queue have no default
    session and require `sessionKey`); a present one must be a non-empty template string.
    `workspaceKey: session` is a reserved keyword: one workspace per session.
    Template syntax/rendering is an adapter concern (Task 3), not validated here.
    """

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    workspace_key: str | None = Field(default=None, alias="workspaceKey")
    session_key: str | None = Field(default=None, alias="sessionKey")

    @model_validator(mode="before")
    @classmethod
    def _reject_explicit_null(cls, data: Any) -> Any:
        """A KEY PRESENT with value null is not the same as an OMITTED key (contract §2) —
        by the time a field validator runs, both already look like Python `None`, so this
        must inspect the raw wire dict before Pydantic merges in the field default (review I7).
        """
        if isinstance(data, dict):
            for key in ("workspaceKey", "sessionKey"):
                if key in data and data[key] is None:
                    raise ValueError(
                        f"routing.{key} is present but null — omit it to keep the adapter "
                        "default, or supply a non-empty template string"
                    )
        return data

    @field_validator("workspace_key", "session_key")
    @classmethod
    def _nonempty_if_present(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("routing override must be a non-empty template string")
        return v


class WebhookAuthBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    type: Literal["gitlab_token", "hmac", "header_token", "none"] = "hmac"
    env: str | None = None
    header: str = ""

    @model_validator(mode="after")
    def _check(self) -> WebhookAuthBlock:
        if self.type != "none":
            if not self.env:
                raise ValueError(f"webhook.auth.env is required for type={self.type!r}")
            if not _ENV_NAME_RE.match(self.env):
                raise ValueError(f"webhook.auth.env is not a valid env alias: {self.env!r}")
        elif self.env is not None:
            raise ValueError("webhook.auth.env must be omitted when type='none'")
        return self


class WebhookBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    auth: WebhookAuthBlock = Field(default_factory=WebhookAuthBlock)
    gitlab_events: (
        list[
            Literal[
                "merge_request",
                "issue",
                "note",
                "push",
                "project_create",
                "project_rename",
                "project_transfer",
                "project_update",
                "repository_update",
            ]
        ]
        | None
    ) = Field(default=None, alias="gitlabEvents")
    bot_username: str | None = Field(default=None, alias="botUsername")
    trigger_users: list[str] | None = Field(default=None, alias="triggerUsers")
    merge_requests_only: bool = Field(default=False, alias="mergeRequestsOnly")


class A2AAuthBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    header: str = Field(default="x-a2a-custom-api-key")
    env: str

    @field_validator("env")
    @classmethod
    def _valid_name(cls, v: str) -> str:
        if not _ENV_NAME_RE.match(v):
            raise ValueError(f"a2a.auth.env is not a valid env alias: {v!r}")
        return v


class A2ABlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["async"] = "async"
    auth: A2AAuthBlock


class CronBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schedule: str
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone: {v!r}") from exc
        return v


class QueueBlock(BaseModel):
    """Contract §3: the adapter confirms delivery on `Started`, never on completion.

    `ackMode` was a legacy `onComplete` selector (old-ach-agent) with no equivalent here —
    there is exactly one ACK semantic contract-wide, so it is not a configurable field.
    """

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    type: Literal["redis"] = "redis"
    key: str


class ScriptBlock(BaseModel):
    """Base shape for `channels[].script` (webhook-script handler) and `handoff` (below).

    `env` carries literal selected values; `secretEnv` carries private alias NAMES only
    (contract §11: "Handoff/script env contiene literales seleccionados" /
    "secretEnv.<nombre>: {env: ...} contienen solo aliases privados").
    """

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    script: str
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: dict[str, SecretAlias] = Field(default_factory=dict, alias="secretEnv")
    timeout_seconds: int = Field(default=120, gt=0, le=3600, alias="timeoutSeconds")

    @model_validator(mode="after")
    def _check(self) -> ScriptBlock:
        if not self.script.strip():
            raise ValueError("script: 'script' must not be empty")
        for name in (*self.env, *self.secret_env):
            if not _ENV_NAME_RE.match(name):
                raise ValueError(f"script: not a valid environment variable name: {name!r}")
            if name in _RESERVED_HANDOFF_ENV or name.startswith(_RESERVED_HANDOFF_ENV_PREFIX):
                raise ValueError(f"script: env name '{name}' is reserved by the harness")
        clash = set(self.env) & set(self.secret_env)
        if clash:
            raise ValueError(f"script: env name(s) in both env and secretEnv: {sorted(clash)}")
        return self


def _valid_relative_destination(v: str) -> str:
    """Reject anything that is not a proper descendant of the Workspace (review I4).

    `PurePosixPath` collapses spurious slashes and single-dot components (but NOT `..`,
    which it deliberately leaves unresolved) — so `.`, `./`, `././` and `./.` all normalize
    to the same empty-`.parts` root. Checking `.parts` rather than the raw string catches
    every one of those spellings, not just the literal `"."`.
    """
    if not v or not v.strip():
        raise ValueError("handoff.destination must be a non-empty relative path")
    if "\x00" in v:
        raise ValueError("handoff.destination must not contain a NUL byte")
    p = PurePosixPath(v)
    if p.is_absolute():
        raise ValueError(f"handoff.destination must be relative, not absolute: {v!r}")
    if not p.parts or ".." in p.parts:
        raise ValueError(
            f"handoff.destination must be a proper descendant of the Workspace, "
            f"not the Workspace root itself or a path that traverses out of it: {v!r}"
        )
    return v


class HandoffBlock(ScriptBlock):
    """`channels[].handoff` — replaces the shared Workspace directory at `destination`.

    V1 requires `workspace.maxConcurrentSessions == 1` whenever any channel carries a handoff
    (contract §6) — the Workspace, not a per-session HOME, is always the destination. It runs
    for every event; the directory is shared by every session in the workspace, so per-session
    files need `routing.workspaceKey: session`. `scope` stays only because ACH renders it.
    """

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    scope: Literal["event"] = "event"
    destination: str

    @field_validator("destination")
    @classmethod
    def _check_destination(cls, v: str) -> str:
        return _valid_relative_destination(v)


class ChannelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    name: str
    type: ChannelType
    concurrency: int = Field(default=1, gt=0)
    source: Literal["gitlab", "github", "generic"] | None = None
    routing: RoutingBlock | None = None
    prompt: str | None = None
    webhook: WebhookBlock | None = None
    handoff: HandoffBlock | None = None
    script: ScriptBlock | None = None
    cron: CronBlock | None = None
    queue: QueueBlock | None = None
    a2a: A2ABlock | None = None

    @model_validator(mode="after")
    def _check_type_block_coherence(self) -> ChannelConfig:
        t = self.type
        if t == "webhook-script":
            if self.webhook is None:
                raise ValueError(f"channel '{self.name}': webhook-script requires a webhook block")
            if self.script is None:
                raise ValueError(f"channel '{self.name}': webhook-script requires a script block")
            for field in ("prompt", "handoff"):
                if getattr(self, field) is not None:
                    raise ValueError(f"channel '{self.name}': webhook-script forbids '{field}'")
            if self.source != "gitlab":
                raise ValueError(f"channel '{self.name}': webhook-script requires source=gitlab")
            if self.webhook.gitlab_events is None:
                raise ValueError(
                    f"channel '{self.name}': webhook-script requires webhook.gitlabEvents"
                )
        elif getattr(self, t) is None:
            article = "an" if t == "a2a" else "a"
            raise ValueError(f"channel '{self.name}': type='{t}' requires {article} '{t}' block")

        if t in ("webhook", "webhook-script") and self.source is None:
            raise ValueError(f"channel '{self.name}': type='{t}' requires 'source'")
        if t == "webhook" and self.webhook is not None and self.webhook.gitlab_events:
            extra = sorted(set(map(str, self.webhook.gitlab_events)) - CONVERSATIONAL_GITLAB_EVENTS)
            if extra:
                raise ValueError(
                    f"channel '{self.name}': type='webhook' cannot route {extra} — "
                    "project/system events require type='webhook-script'"
                )
        needs_session = t == "queue" or (t == "webhook" and self.source == "generic")
        if needs_session and (self.routing is None or self.routing.session_key is None):
            raise ValueError(
                f"channel '{self.name}': routing.sessionKey is required "
                "(this source has no standard conversation identity)"
            )
        for foreign in ("webhook", "cron", "queue", "a2a"):
            allowed = foreign == t or (t == "webhook-script" and foreign == "webhook")
            if not allowed and getattr(self, foreign) is not None:
                raise ValueError(f"channel '{self.name}': type='{t}' forbids '{foreign}' block")
        if t != "webhook-script" and self.script is not None:
            raise ValueError(f"channel '{self.name}': type='{t}' forbids 'script' block")
        return self


# ---------------------------------------------------------------------------
# infrastructure block (contract v2 §3.1 — facadeEndpoint only; the host owns the rest)
# ---------------------------------------------------------------------------

# Review I5: the dispatch requires "TLS endpoints" and "bounded emptyDir" — these are typed
# strings today with no invariant enforced. Shared leaf checks, reused by the
# endpoint field below AND by PublicConfig/ExecutionBootstrap's own independent
# leaf models in public.py (imported from here, not redefined — those two are validated as
# standalone documents in their own right, not merely copied from an already-checked
# RuntimeConfig, so they need the same checks applied to their own fields).

# Single source for BOTH the Python check and the RAW Pydantic JSON Schema export (via
# `Field(json_schema_extra={"pattern": ENDPOINT_PATTERN})` below) — fix-2's gap C re-check:
# patching a schema dict in a test fixture after the fact is not the same as the actual
# `Model.model_json_schema()` export already carrying the constraint, which is what an
# external consumer (ACH, or anyone else calling it directly) actually gets. No
# `(?!...)`/`(?=...)` — plain anchored-at-start pattern, RE2-safe.
#
# Task-1 scope reset: the control/execution endpoints are plain in-cluster Service DNS
# (`http://agent-<agent name>.<namespace>.svc:8081`, `/facades`) — compulsory HTTPS implied an
# issuer-dependent transport this boundary no longer owns (D2 covers authenticated
# application calls separately). Validation now only requires an ordinary http(s) endpoint
# with a host, not a specific scheme.
ENDPOINT_PATTERN = r"^https?://[^/\s]+"
_ENDPOINT_RE = re.compile(ENDPOINT_PATTERN)


def _require_endpoint(v: str, field: str) -> str:
    if not _ENDPOINT_RE.match(v):
        raise ValueError(f"{field} must be an http(s):// endpoint with a host, not {v!r}")
    return v


class InfrastructureBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    facade_endpoint: str | None = Field(
        default=None, alias="facadeEndpoint", json_schema_extra={"pattern": ENDPOINT_PATTERN}
    )

    @field_validator("facade_endpoint")
    @classmethod
    def _check_endpoint(cls, v: str | None) -> str | None:
        return None if v is None else _require_endpoint(v, "infrastructure.facadeEndpoint")


# ---------------------------------------------------------------------------
# Root RuntimeConfig
# ---------------------------------------------------------------------------


class RuntimeConfig(BaseModel):
    """The complete resolved wire object `W` (contract §5/§11) minus no field at all.

    `configVersion` IS a member of this model (and of W) — compute_config_version() only
    excludes it from the bytes being hashed, it is not absent from the schema.
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, validate_by_alias=True, validate_by_name=False
    )

    schema_version: Literal["workspace-v2"] = Field(alias="schemaVersion")
    agent: AgentIdentity
    config_version: str = Field(alias="configVersion", pattern=r"^[0-9a-f]{64}$")
    ach: AchBlock
    model: ModelBlock
    engine: EngineBlock
    limits: LimitsBlock
    workspace: WorkspaceBlock
    artifacts: ArtifactsBlock
    channels: list[ChannelConfig] = Field(default_factory=list)
    prompt: PromptBlock | None = None
    hooks: HooksBlock = Field(default_factory=HooksBlock)
    memory: AchMemoryMemory | None = None
    infrastructure: InfrastructureBlock = Field(default_factory=InfrastructureBlock)

    @model_validator(mode="after")
    def _shared_handoff_requires_single_session(self) -> RuntimeConfig:
        if any(c.handoff is not None for c in self.channels) and self.workspace.max_concurrent_sessions != 1:
            raise ValueError(
                "a channel handoff replaces the shared Workspace directory: "
                "workspace.maxConcurrentSessions must be 1"
            )
        return self

    @model_validator(mode="after")
    def _hook_and_handoff_timeouts_fit_the_lane(self) -> RuntimeConfig:
        limit = self.limits.max_invocation_seconds
        for channel in self.channels:
            for field in ("handoff", "script"):
                block = getattr(channel, field)
                if block is not None and block.timeout_seconds > limit:
                    raise ValueError(
                        f"channel '{channel.name}': {field}.timeoutSeconds "
                        f"({block.timeout_seconds}) exceeds limits.maxInvocationSeconds ({limit})"
                    )
        for hook_name in ("session_start", "session_restore"):
            hook = getattr(self.hooks, hook_name)
            if hook is not None and hook.timeout_seconds > limit:
                raise ValueError(
                    f"hooks.{hook_name}.timeoutSeconds ({hook.timeout_seconds}) exceeds "
                    f"limits.maxInvocationSeconds ({limit})"
                )
        return self


def parse_config(raw: dict[str, Any]) -> RuntimeConfig:
    """Validate an already-decoded resolved wire dict and verify its configVersion.

    Hashes/validates the ORIGINAL wire `raw` before Pydantic fills any default or normalizes
    presence (contract §5: "el consumidor verifica ese mismo objeto antes de aplicar defaults
    internos"). Does not detect duplicate keys — ordinary JSON decoding never does (a later
    key wins), whether `raw` arrived as bytes via load_config_json or already as a dict.

    Raises `ConfigError` for every rejection on this boundary — ingress (Unicode/numeric),
    hash mismatch and schema validation alike (review M1: these used to leak
    `CanonicalizationError` for the first kind and `ConfigError` for the rest, so a caller
    catching only `ConfigError` missed ingress failures).
    """
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")
    if raw.get("schemaVersion") == "workspace-v1":
        raise ConfigError(
            "workspace-v1 is no longer supported; see docs/schemas/ach-runtime-contract-v2.md"
        )
    try:
        validate_json_value(raw)
    except CanonicalizationError as exc:
        raise ConfigError(str(exc)) from exc

    supplied = raw.get("configVersion")
    if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        raise ConfigError(f"configVersion must be 64 lowercase hex characters: {supplied!r}")
    try:
        expected = compute_config_version(raw)
    except CanonicalizationError as exc:
        raise ConfigError(str(exc)) from exc
    if supplied != expected:
        raise ConfigError(f"configVersion mismatch: supplied {supplied!r}, computed {expected!r}")

    try:
        # strict=True at call time, not just in RuntimeConfig.model_config: Pydantic v2 does
        # NOT propagate a model's own `strict` config into independently-configured nested
        # models (LimitsBlock, WorkspaceBlock, ...) — only the call-time override does, and
        # without it `maxSteps: true` / `idleTimeoutSeconds: "300"` would silently coerce.
        return RuntimeConfig.model_validate(raw, strict=True)
    except ValidationError as exc:
        raise ConfigError(str(exc)) from exc

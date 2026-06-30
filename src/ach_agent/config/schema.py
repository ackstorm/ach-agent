# SPDX-License-Identifier: Apache-2.0
"""Full CONTRACT_v3 §2 Pydantic v2 config schema + hard-fail loader (CFG-01/02/03, D-01).

Models every block from the rendered runtime config. All blocks carry
ConfigDict(extra='forbid') so unknown keys cause a hard-fail at load time.

Constraint: NEVER import the router or any Hermes type here (RTR-06).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Leaf / utility blocks
# ---------------------------------------------------------------------------


class AgentBlock(BaseModel):
    """CONTRACT §2 agent block."""

    model_config = ConfigDict(extra="forbid")

    name: str


class ModelBlock(BaseModel):
    """CONTRACT_v3 §2 model block: provider-selecting name + open params."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str  # e.g. "openai.gpt-5"; passed verbatim
    type: Literal["openai", "gemini", "anthropic"]  # selects the ACH compat endpoint
    params: dict[str, Any] = Field(default_factory=dict)  # open, unvalidated, splatted to client


class LimitsBlock(BaseModel):
    """CONTRACT §2 limits block (§18.6 — all finite, always enforced)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_concurrent_invocations: int = Field(default=1, alias="maxConcurrentInvocations")
    max_invocation_seconds: int = Field(default=1800, alias="maxInvocationSeconds")
    max_queued_total: int = Field(default=100, alias="maxQueuedTotal")
    idempotency_window_seconds: int = Field(default=3600, alias="idempotencyWindowSeconds")
    max_steps: int = Field(default=50, alias="maxSteps")
    terminal_output_retries: int = Field(default=1, alias="terminalOutputRetries")


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


class PersistenceBlock(BaseModel):
    """CONTRACT §2 persistence block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    mount_path: str = Field(default="/var/lib/ach-agent", alias="mountPath")


class HealthBlock(BaseModel):
    """CONTRACT §2 health block."""

    model_config = ConfigDict(extra="forbid")

    host: str = "0.0.0.0"
    port: int = 8000


class PromptBlock(BaseModel):
    """CONTRACT §2 prompt block."""

    model_config = ConfigDict(extra="forbid")

    system: str = ""
    compose: str = "append"


class MemoryBlock(BaseModel):
    """CONTRACT §2 memory block (fail-open — §31)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint: str
    mission: str = ""
    # Static memory bank_id (the memory namespace for this agent's mission, e.g.
    # "gitlab-pr-review"). Per-event tag-based partitioning is a separate future layer
    # (see the memory bank+tags design note) and does NOT change this static field.
    bank: str = ""
    mental_models: list[str] = Field(default_factory=list, alias="mentalModels")


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


class WebhookAuthBlock(BaseModel):
    """CONTRACT_v3 §2 webhook.auth sub-block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["gitlab_token", "hmac", "header_token", "none"] = "hmac"
    secret_path: str = Field(default="", alias="secretPath")
    # For type=="header_token": the request header carrying the static shared secret
    # (constant-time compared against the file at secret_path). Ignored for other types.
    header: str = Field(default="")


class WebhookBlock(BaseModel):
    """CONTRACT_v3 §2 webhook channel sub-block (deliver/deliverOnly removed)."""

    model_config = ConfigDict(extra="forbid")

    auth: WebhookAuthBlock = Field(default_factory=WebhookAuthBlock)


class A2AAuthBlock(BaseModel):
    """CONTRACT §2 a2a.auth sub-block (§14.6 / §3 bearer discipline)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    header: str = Field(default="x-a2a-custom-api-key")
    secret_path: str = Field(default="", alias="secretPath")


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


class ChannelConfig(BaseModel):
    """CONTRACT_v3 §2 channel entry. extra=forbid catches unknown channel-level keys."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    type: ChannelType  # Literal union rejects unknown types (CFG-03)
    concurrency: int = 1
    prompt: str | None = None
    source: Literal["gitlab", "github", "generic"] | None = None
    webhook: WebhookBlock | None = None
    cron: CronBlock | None = None
    queue: QueueBlock | None = None
    a2a: A2ABlock | None = None

    @model_validator(mode="after")
    def check_type_block_coherence(self) -> ChannelConfig:
        """D-04: enforce channel type↔sub-block coherence at config load time.

        Each channel type requires its sub-block and forbids foreign sub-blocks.
        Raises ValueError (wrapped by Pydantic into ValidationError → sys.exit(1)).
        """
        t = self.type
        if t == "webhook":
            if self.webhook is None:
                raise ValueError(
                    f"channel '{self.name}': type='webhook' requires a 'webhook' block"
                )
            if self.source is None:
                raise ValueError(f"channel '{self.name}': type='webhook' requires 'source' field")
            for foreign in ("cron", "queue", "a2a"):
                if getattr(self, foreign) is not None:
                    raise ValueError(
                        f"channel '{self.name}': type='webhook' forbids '{foreign}' block"
                    )
        elif t == "cron":
            if self.cron is None:
                raise ValueError(f"channel '{self.name}': type='cron' requires a 'cron' block")
            for foreign in ("webhook", "queue", "a2a"):
                if getattr(self, foreign) is not None:
                    raise ValueError(
                        f"channel '{self.name}': type='cron' forbids '{foreign}' block"
                    )
        elif t == "queue":
            if self.queue is None:
                raise ValueError(f"channel '{self.name}': type='queue' requires a 'queue' block")
            for foreign in ("webhook", "cron", "a2a"):
                if getattr(self, foreign) is not None:
                    raise ValueError(
                        f"channel '{self.name}': type='queue' forbids '{foreign}' block"
                    )
        elif t == "a2a":
            if self.a2a is None:
                raise ValueError(f"channel '{self.name}': type='a2a' requires an 'a2a' block")
            for foreign in ("webhook", "cron", "queue"):
                if getattr(self, foreign) is not None:
                    raise ValueError(f"channel '{self.name}': type='a2a' forbids '{foreign}' block")
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
    memory: MemoryBlock | None = None
    limits: LimitsBlock = Field(default_factory=LimitsBlock)
    engine: EngineBlock = Field(default_factory=EngineBlock)
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

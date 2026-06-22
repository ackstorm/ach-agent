# SPDX-License-Identifier: Apache-2.0
"""Full CONTRACT §2 Pydantic v2 config schema + hard-fail loader (CFG-01/02/03, D-01).

Models every block from the rendered runtime config. All blocks carry
ConfigDict(extra='forbid') so unknown keys cause a hard-fail at load time.

Constraint: NEVER import the router or any Hermes type here (RTR-06).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Leaf / utility blocks
# ---------------------------------------------------------------------------


class AgentBlock(BaseModel):
    """CONTRACT §2 agent block."""

    model_config = ConfigDict(extra="forbid")

    name: str
    namespace: str
    generation: int = 0


class SharedEngineBlock(BaseModel):
    """CONTRACT §2 engine.shared sub-block."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ttl_seconds: int = Field(default=0, alias="ttlSeconds")

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EngineBlock(BaseModel):
    """CONTRACT §2 engine block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str = "opencode"
    binary_path: str = Field(default="opencode", alias="binaryPath")
    work_dir: str = Field(default="/workspace", alias="workDir")
    session_dir: str = Field(default="/var/lib/ach-agent/opencode/sessions", alias="sessionDir")
    thinking_level: str = Field(default="medium", alias="thinkingLevel")
    steps: int = 50
    startup_timeout_seconds: int = Field(default=30, alias="startupTimeoutSeconds")
    shared: SharedEngineBlock = Field(default_factory=SharedEngineBlock)


class ModelBlock(BaseModel):
    """CONTRACT §2 model block."""

    model_config = ConfigDict(extra="forbid")

    default: str = "gpt-4o-mini"
    provider: str = "openai"


class LimitsBlock(BaseModel):
    """CONTRACT §2 limits block (§18.6 — all finite, always enforced)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_concurrent_invocations: int = Field(default=1, alias="maxConcurrentInvocations")
    max_invocation_seconds: int = Field(default=1800, alias="maxInvocationSeconds")
    max_queued_total: int = Field(default=100, alias="maxQueuedTotal")
    idempotency_window_seconds: int = Field(default=3600, alias="idempotencyWindowSeconds")


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

    base: str = ""
    compose: str = "append"


class MemoryBlock(BaseModel):
    """CONTRACT §2 memory block (fail-open — §31)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint: str
    mission: str = ""
    scope: str = ""
    mental_models: list[str] = Field(default_factory=list, alias="mentalModels")


# ---------------------------------------------------------------------------
# Channel sub-blocks
# ---------------------------------------------------------------------------


class SessionBlock(BaseModel):
    """CONTRACT §2 channel session block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    mode: str = "auto"
    continuity: str = "ephemeral"
    ttl_seconds: int = Field(default=86400, alias="ttlSeconds")


class ResponseBlock(BaseModel):
    """CONTRACT §2 channel response block."""

    model_config = ConfigDict(extra="forbid")

    mode: str = "actionRequired"
    fallback: str = "fail"


class WebhookAuthBlock(BaseModel):
    """CONTRACT §2 webhook.auth sub-block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: str = "hmac"
    secret_path: str = Field(default="", alias="secretPath")


class WebhookDeliverBlock(BaseModel):
    """CONTRACT §2 webhook.deliver sub-block."""

    model_config = ConfigDict(extra="forbid")

    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class WebhookBlock(BaseModel):
    """CONTRACT §2 webhook channel sub-block."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    auth: WebhookAuthBlock = Field(default_factory=WebhookAuthBlock)
    deliver: WebhookDeliverBlock | None = None
    deliver_only: bool = Field(default=False, alias="deliverOnly")


class A2AAuthBlock(BaseModel):
    """CONTRACT §2 a2a.auth sub-block (§14.6 / §3 bearer discipline)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    header: str = Field(default="x-a2a-custom-api-key")
    secret_path: str = Field(default="", alias="secretPath")


class A2ABlock(BaseModel):
    """CONTRACT §2 a2a channel sub-block (CHN-05)."""

    model_config = ConfigDict(extra="forbid")

    auth: A2AAuthBlock = Field(default_factory=A2AAuthBlock)


class CronBlock(BaseModel):
    """CONTRACT §2 cron channel sub-block (CHN-02)."""

    model_config = ConfigDict(extra="forbid")

    schedule: str  # cron expression, e.g. "* * * * *"


class ResponseActionBlock(BaseModel):
    """CONTRACT §2 responseActions entry."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    kind: str
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="inputSchema")
    # D-04: Phase 5 frozen-seam revision — consentTier is OPTIONAL, default "consent" (safe).
    # Backward-compatible: existing configs without consentTier resolve to "consent".
    # Absent → same as "consent". extra="forbid" still holds.
    consent_tier: Literal["auto", "consent"] = Field(default="consent", alias="consentTier")


# ---------------------------------------------------------------------------
# ChannelType + ChannelConfig
# ---------------------------------------------------------------------------

# D-02: unrecognized channel type → ValidationError → hard-fail
ChannelType = Literal["webhook", "slack", "telegram", "a2a", "cron"]


class ChannelConfig(BaseModel):
    """CONTRACT §2 channel entry. extra=forbid catches unknown channel-level keys."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    type: ChannelType  # Literal union rejects unknown types (CFG-03)
    concurrency: int = 1
    expire: int = 120
    session: SessionBlock = Field(default_factory=SessionBlock)
    response: ResponseBlock = Field(default_factory=ResponseBlock)
    prompt: str | None = None
    webhook: WebhookBlock | None = None
    cron: CronBlock | None = None
    a2a: A2ABlock | None = None
    response_actions: list[ResponseActionBlock] = Field(
        default_factory=list, alias="responseActions"
    )


# ---------------------------------------------------------------------------
# Root AgentConfig
# ---------------------------------------------------------------------------


class AgentConfig(BaseModel):
    """Full CONTRACT §2 rendered runtime config (D-01: modeled in one pass).

    ConfigDict(extra='forbid', strict=True) ensures unknown top-level keys
    cause a ValidationError that hard-fails the process (CFG-02).
    """

    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)

    schema_version: Literal["1"] = Field(alias="schemaVersion")
    agent: AgentBlock
    engine: EngineBlock
    model: ModelBlock
    limits: LimitsBlock = Field(default_factory=LimitsBlock)
    channels: list[ChannelConfig] = Field(default_factory=list)
    governed: bool = False
    prompt: PromptBlock | None = None
    memory: MemoryBlock | None = None
    persistence: PersistenceBlock = Field(default_factory=PersistenceBlock)
    health: HealthBlock = Field(default_factory=HealthBlock)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def load_config(path: str) -> AgentConfig:
    """Load and validate the rendered runtime config (CFG-01/02/03).

    Hard-fails with sys.exit(1) on schema mismatch or file-not-found.
    Never raises to the caller.
    """
    try:
        raw = Path(path).read_text(encoding="utf-8")
        return AgentConfig.model_validate_json(raw)
    except ValidationError as exc:
        log.error("config schema mismatch — exiting", errors=exc.errors())
        sys.exit(1)
    except FileNotFoundError:
        log.error("config file not found — exiting", path=path)
        sys.exit(1)

# SPDX-License-Identifier: MIT
"""PublicConfig (mini-harness projection) and ExecutionBootstrap (contract §11).

PublicConfig never carries control broker/tokens/private keys, ach.identity, channel auth or
handoff secretEnv — public_config() constructs it explicitly field by field rather than
copying RuntimeConfig and deleting known secrets, so a field added to RuntimeConfig later
does not leak into the public projection by default. It holds only execution-required
policies/prompts/hooks/approved engine env, refs/versions and public connection metadata
(not a credential or proof of successful enrollment).

ExecutionBootstrap is the separate, strict, secret-free document mounted at
`/etc/ach-runtime/bootstrap.json` (contract §11) — independent of RuntimeConfig/PublicConfig:
no model, prompts, engine.env or configVersion. Task-1 scope reset: it carries only
`bootstrapVersion`, agent identity and the public control/facade endpoints — the
projected-token/CA pod-auth scheme it used to carry is excluded, not deferred; D2 instead
reuses the existing signed mini-harness bearer and HMAC facade authentication for the
execution pod's outbound connection to the Harness.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ach_runtime.config.schema import (
    ENDPOINT_PATTERN,
    AchCapabilityBlock,
    AgentIdentity,
    CompactionBlock,
    ConfigError,
    HooksBlock,
    LimitsBlock,
    ModelBlock,
    PromptBlock,
    RuntimeConfig,
    _require_endpoint,
)


class PublicEngineBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    env: dict[str, str] = Field(default_factory=dict)
    startup_timeout_seconds: int = Field(gt=0, alias="startupTimeoutSeconds")
    compaction: CompactionBlock
    deny_tools: list[str] = Field(default_factory=list, alias="denyTools")


class PublicInfrastructureBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    # `json_schema_extra` puts the SAME pattern directly into this field's RAW
    # `model_json_schema()` export (fix-2 gap C re-check: a test patching the exported dict
    # after the fact is not the same guarantee as the actual product export already
    # carrying the constraint — this is validated as its own standalone document, not merely
    # copied from an already-checked RuntimeConfig).
    facade_endpoint: str = Field(
        alias="facadeEndpoint", json_schema_extra={"pattern": ENDPOINT_PATTERN}
    )

    @field_validator("facade_endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        # Validated as its own leaf, not assumed safe merely because public_config()
        # happens to copy it from an already-checked RuntimeConfig (review I5).
        return _require_endpoint(v, "PublicConfig.infrastructure endpoint")


class PublicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    schema_version: Literal["workspace-v2"] = Field(alias="schemaVersion")
    agent: AgentIdentity
    config_version: str = Field(alias="configVersion", pattern=r"^[0-9a-f]{64}$")
    model: ModelBlock
    capability: AchCapabilityBlock
    prompt: PromptBlock | None = None
    limits: LimitsBlock
    engine: PublicEngineBlock
    hooks: HooksBlock
    infrastructure: PublicInfrastructureBlock


def public_config(config: RuntimeConfig, engine_env: dict[str, str]) -> PublicConfig:
    """Construct the mini-harness projection explicitly — never a filtered copy of `config`.

    `engine_env` is externally supplied by the caller — validated here against
    `config.engine.env`, the producer-approved literal name->value map for this
    configVersion (contract §5), not merely type-checked as `dict[str, str]`. An unapproved
    name or a value that drifted from the approved literal is a configuration error, not a
    silently-forwarded value.
    """
    approved = config.engine.env
    for name, value in engine_env.items():
        if name not in approved:
            raise ConfigError(f"engine_env name is not approved by engine.env: {name!r}")
        if approved[name] != value:
            raise ConfigError(
                f"engine_env value for {name!r} does not match the approved literal"
            )

    facade_endpoint = config.infrastructure.facade_endpoint
    if facade_endpoint is None:
        raise ConfigError("infrastructure.facadeEndpoint is required")
    return PublicConfig(
        schemaVersion=config.schema_version,
        agent=config.agent,
        configVersion=config.config_version,
        model=config.model,
        capability=config.ach.capability,
        prompt=config.prompt,
        limits=config.limits,
        engine=PublicEngineBlock(
            env=dict(engine_env),
            startupTimeoutSeconds=config.engine.startup_timeout_seconds,
            compaction=config.engine.compaction,
            denyTools=list(config.engine.deny_tools),
        ),
        hooks=config.hooks,
        infrastructure=PublicInfrastructureBlock(
            facadeEndpoint=facade_endpoint,
        ),
    )


class ExecutionBootstrap(BaseModel):
    """`bootstrap.json` — strict, independent of RuntimeConfig/PublicConfig (contract §11).

    No model/prompts/engine.env/configVersion. Public connection metadata only — not a
    credential or proof of successful enrollment; D1/D2 own authenticating the execution
    pod's outbound connection to the Harness separately.
    """

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    bootstrap_version: Literal["workspace-v2"] = Field(alias="bootstrapVersion")
    agent: AgentIdentity
    facade_endpoint: str = Field(
        alias="facadeEndpoint", json_schema_extra={"pattern": ENDPOINT_PATTERN}
    )

    @field_validator("facade_endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        return _require_endpoint(v, "ExecutionBootstrap endpoint")

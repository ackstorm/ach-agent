# SPDX-License-Identifier: Apache-2.0
"""Public config surface for ach-agent.

All downstream modules import from here, not from config.schema directly.

Constraint: NEVER import the router or any Hermes type here (RTR-06).
Engine never imports this module (D-08).
"""

from ach_agent.config.schema import (
    A2AAuthBlock,
    A2ABlock,
    AgentBlock,
    AgentConfig,
    CapabilityAchBlock,
    CapabilityBlock,
    CapabilityFilterBlock,
    CapabilityFilterExcludeBlock,
    ChannelConfig,
    ChannelType,
    CronBlock,
    HealthBlock,
    LimitsBlock,
    MemoryBlock,
    PersistenceBlock,
    PromptBlock,
    QueueBlock,
    WebhookAuthBlock,
    WebhookBlock,
    load_config,
)

__all__ = [
    "A2AAuthBlock",
    "A2ABlock",
    "AgentBlock",
    "AgentConfig",
    "CapabilityAchBlock",
    "CapabilityBlock",
    "CapabilityFilterBlock",
    "CapabilityFilterExcludeBlock",
    "ChannelConfig",
    "ChannelType",
    "CronBlock",
    "HealthBlock",
    "LimitsBlock",
    "MemoryBlock",
    "PersistenceBlock",
    "PromptBlock",
    "QueueBlock",
    "WebhookAuthBlock",
    "WebhookBlock",
    "load_config",
]

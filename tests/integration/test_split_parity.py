# SPDX-License-Identifier: Apache-2.0
"""Static contract checks for the task-owned live split acceptance.

The native binary evidence is collected by ``scripts/test-split.sh``.  These checks
keep its fixture/configuration contract reviewable in the ordinary Docker test gate
without silently turning a missing Docker daemon into a passing test.
"""

from __future__ import annotations

import json
from pathlib import Path

from ach_agent.config import load_config
from ach_agent.config.schema import ChannelSourceConfig

ROOT = Path(__file__).parents[2]


def test_acceptance_config_and_channel_projection_match() -> None:
    config = load_config(str(ROOT / "docker/split/config-acceptance.yaml"))
    projection = json.loads((ROOT / "docker/split/channels-acceptance.json").read_text())
    assert config.agent.name == "split-acceptance"
    assert {channel.name for channel in config.channels} == {"acceptance", "cancel"}
    source = ChannelSourceConfig.model_validate(projection["channels"][0])
    assert source.name == config.channels[0].name == "acceptance"
    assert "prompt" not in projection["channels"][0]
    assert "session" not in projection["channels"][0]

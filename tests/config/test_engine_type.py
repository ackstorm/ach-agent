# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import EngineBlock


def test_engine_type_defaults_to_opencode() -> None:
    assert EngineBlock().type == "opencode"


def test_engine_type_pi_accepted_with_subblock() -> None:
    eng = EngineBlock.model_validate({"type": "pi", "pi": {"binaryPath": "pi"}})
    assert eng.type == "pi"
    assert eng.pi is not None and eng.pi.binary_path == "pi"


def test_engine_type_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        EngineBlock.model_validate({"type": "pymono"})  # renamed to 'pi' — old name rejected


def test_pi_subblock_defaults() -> None:
    from ach_agent.config.schema import PiEngineBlock

    pi = PiEngineBlock()
    assert pi.binary_path == "pi"
    assert pi.mcp_adapter_path == ""

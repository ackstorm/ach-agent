# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from ach_agent.engine.base.driver import EngineDriver
from ach_agent.engine.pi.driver import PiDriver


def test_pi_driver_satisfies_protocol() -> None:
    assert isinstance(PiDriver(), EngineDriver)
    assert PiDriver().engine_type == "pi"


async def test_pi_driver_launch_stub_raises_until_phase_8() -> None:
    from ach_agent.engine.base.driver import EngineConfig

    with pytest.raises(NotImplementedError):
        await PiDriver().launch(EngineConfig(engine_type="pi"), "k1")

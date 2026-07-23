# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from ach_agent.engine.base.driver import EngineDriver
from ach_agent.engine.pi.driver import PiDriver


def test_pi_driver_satisfies_protocol() -> None:
    assert isinstance(PiDriver(), EngineDriver)
    assert PiDriver().engine_type == "pi"


def test_pi_driver_run_turn_signature_required_kwonly_params() -> None:
    import inspect

    sig = inspect.signature(PiDriver.run_turn)
    params = sig.parameters

    for param_name in ("on_text", "on_tool", "max_tool_calls", "stats"):
        assert param_name in params, f"missing param {param_name}"
        p = params[param_name]
        assert p.kind == inspect.Parameter.KEYWORD_ONLY, f"{param_name} must be keyword-only"
        assert p.default is inspect.Parameter.empty, f"{param_name} must have no default"

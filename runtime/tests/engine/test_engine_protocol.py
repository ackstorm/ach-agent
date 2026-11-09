# SPDX-License-Identifier: Apache-2.0
from ach_runtime.engine.base import Engine
from ach_runtime.engine.opencode import OpenCodeEngine
from ach_runtime.engine.pi import PiEngine


def test_engines_satisfy_the_protocol() -> None:
    assert isinstance(OpenCodeEngine(), Engine)
    assert isinstance(PiEngine(), Engine)


def test_each_engine_names_its_family() -> None:
    assert OpenCodeEngine.family == "opencode"
    assert PiEngine.family == "pi"

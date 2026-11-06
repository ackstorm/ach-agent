# SPDX-License-Identifier: MIT
from ach_runtime.engine.base import Engine
from ach_runtime.engine.opencode import OpenCodeEngine


def test_opencode_engine_satisfies_the_protocol() -> None:
    assert isinstance(OpenCodeEngine(), Engine)

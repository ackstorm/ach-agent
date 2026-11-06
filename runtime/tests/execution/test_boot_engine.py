# SPDX-License-Identifier: MIT
"""The execution process starts the engine the bootstrap selects."""

from __future__ import annotations

from pathlib import Path

import pytest

from ach_runtime.engine.opencode import OpenCodeEngine
from ach_runtime.engine.pi import PiEngine
from ach_runtime.execution.boot import build_execution_app, load_bootstrap
from ach_runtime.harness.tokens import engine_verify_key

BOOTSTRAP_FIXTURE = Path(__file__).resolve().parents[1] / "config" / "fixtures" / "bootstrap.json"


@pytest.mark.parametrize("engine_type, expected", [("pi", PiEngine), ("opencode", OpenCodeEngine)])
def test_build_selects_the_engine_from_the_bootstrap(tmp_path: Path, engine_type: str, expected: type) -> None:
    environ = {"ACH_SANDBOX_VERIFY_KEY": engine_verify_key(b"k" * 32), "ACH_WORKSPACE_NAME": "ws", "ACH_WORKSPACE_REF": "ref"}
    bootstrap = load_bootstrap(BOOTSTRAP_FIXTURE, {}).model_copy(update={"engine": engine_type})
    app = build_execution_app(bootstrap, environ=environ, workspace_dir=tmp_path / "workspace", sessions_root=tmp_path / "sessions")
    assert type(app.state.engine) is expected

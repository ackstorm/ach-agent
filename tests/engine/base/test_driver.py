# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

from ach_agent.engine.base.driver import EngineConfig, EngineDriver, TurnResult


def test_engine_config_defaults_engine_type_opencode() -> None:
    cfg = EngineConfig()
    assert cfg.engine_type == "opencode"


def test_lifecycle_reexports_the_same_engine_config() -> None:
    # The shim in lifecycle.py must resolve to the SAME class object (identity), so
    # every existing `from ach_agent.engine.lifecycle import EngineConfig` is unaffected.
    from ach_agent.engine.lifecycle import EngineConfig as LifecycleEngineConfig

    assert LifecycleEngineConfig is EngineConfig


def test_turn_result_defaults() -> None:
    r = TurnResult(text="hi", session_ref="ses_1")
    assert r.text == "hi"
    assert r.session_ref == "ses_1"
    assert r.aborted is False


def test_stub_satisfies_engine_driver_protocol() -> None:
    class _Stub:
        engine_type = "opencode"

        def skills_dir(self, home: Path) -> Path:
            return home / "skills"

        async def launch(self, cfg: EngineConfig, session_key: str) -> Any:
            return object()

        async def health(self, server: Any) -> bool:
            return True

        async def run_turn(
            self,
            server: Any,
            *,
            conv_key: str,
            prompt: str,
            reuse: bool,
            sessions: MutableMapping[str, str],
            session_ref: str | None = None,
            on_text: Callable[[str], None] | None,
            on_tool: Callable[[Any], None] | None,
            max_tool_calls: int,
            stats: dict[str, Any],
        ) -> TurnResult:
            return TurnResult(text="", session_ref="ses_1")

        async def discard_session(self, server: Any, session_ref: str) -> None: ...
        async def compact_session(self, server: Any, session_ref: str) -> None: ...
        async def stop(self, server: Any) -> None: ...

    assert isinstance(_Stub(), EngineDriver)


def test_run_turn_max_tool_calls_and_stats_are_required_kwonly() -> None:
    # Canonical contract (index.md Shared interface contract): on_text, on_tool,
    # max_tool_calls, and stats all have NO default (stats is also not Optional) —
    # callers must always supply all four.
    import inspect

    sig = inspect.signature(EngineDriver.run_turn)
    assert sig.parameters["on_text"].default is inspect.Parameter.empty
    assert sig.parameters["on_tool"].default is inspect.Parameter.empty
    assert sig.parameters["max_tool_calls"].default is inspect.Parameter.empty
    assert sig.parameters["stats"].default is inspect.Parameter.empty

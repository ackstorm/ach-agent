# SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from ach_agent import main as main_mod


def test_main_runs_preflight_before_config(monkeypatch):
    calls: list[str] = []

    def fake_preflight() -> None:
        calls.append("preflight")

    def fake_load_config(_path):
        calls.append("load_config")
        raise RuntimeError("stop-after-config")

    monkeypatch.setattr(main_mod, "run_preflight", fake_preflight)
    monkeypatch.setattr(main_mod, "load_config", fake_load_config)

    with pytest.raises(RuntimeError, match="stop-after-config"):
        asyncio.run(main_mod.main())

    assert calls == ["preflight", "load_config"]

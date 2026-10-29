# SPDX-License-Identifier: MIT
"""Policy gap I5: native startup (URL discovery, SSE subscription, config/model/agent
readiness) must share ONE deadline derived from configured `engine.startupTimeoutSeconds`
clipped by the original invocation/restore deadline -- never each phase's own fixed or
independently-refreshed timer. Native method/source path regressions: a real (fake)
loopback subprocess stands in for `opencode2` (tests/engine/fixtures/fake_native_server.py,
also used by test_native_control.py/test_process_kill.py), real owned-process-group
tracking, not an unreachable real native acceptance claim."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ach_runtime.config.public import PublicConfig, public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine import opencode as opencode_module
from ach_runtime.engine.opencode import EngineInvocation, NativeLaunchFailed, OpenCodeEngine

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"
_LOOPBACK_SERVER = Path(__file__).parent / "fixtures" / "fake_native_server.py"


def _public(*, startup_timeout_seconds: int) -> PublicConfig:
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    wire["engine"]["startupTimeoutSeconds"] = startup_timeout_seconds
    config = RuntimeConfig.model_validate(wire)
    return public_config(config, engine_env=config.engine.env)


async def test_native_launch_and_readiness_share_nondefault_startup_bound(
    tmp_path: Path, monkeypatch
) -> None:
    """A real (fake) native subprocess never satisfies `/api/model` readiness (the
    loopback server's default `{}` response), so the ONLY thing that can end this run is
    the shared startup deadline. Using a small nondefault bound (well below the old
    independent `STARTUP_TIMEOUT_SECONDS=20`/`EVENTS_READY_TIMEOUT_SECONDS=10`/
    `READINESS_TIMEOUT_SECONDS=20` constants) proves URL discovery, SSE and readiness are
    now one shared clock: the old code would hang for 20-50s; this must fail in a few."""
    monkeypatch.setattr(opencode_module, "OPENCODE_BIN", str(_LOOPBACK_SERVER))
    public = _public(startup_timeout_seconds=2)
    engine = OpenCodeEngine()
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    invocation = EngineInvocation(
        invocation_id="startup-1",
        session_ref="startup-session-1",
        workspace_dir=workspace_dir,
        prompt="must never be sent",
        public=public,
        engine_env=dict(public.engine.env),
    )

    started_at = time.monotonic()
    with pytest.raises(NativeLaunchFailed, match="readiness timed out"):
        await asyncio.wait_for(
            engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch"),
            timeout=15.0,
        )
    elapsed = time.monotonic() - started_at

    # The old independent constants alone sum to 50s (20 URL + 10 SSE + 20 readiness); a
    # shared ~2s bound must finish in a small fraction of that.
    assert elapsed < 10.0

    session = engine._sessions["startup-session-1"]
    assert session.alive is False
    assert session._confirmed_halted is True  # owned halt cleanup was requested
    assert "ses_fake" not in "".join(
        str(call) for call in getattr(session, "_event_log", [])
    )  # no prompt-created session event ever arrived


async def test_native_startup_clips_original_invocation_and_restore_deadline(
    tmp_path: Path, monkeypatch
) -> None:
    """The shared deadline is `min(configured startupTimeoutSeconds, remaining original
    deadline)`: a 7s configured bound clips down to ~2s when only 2s remains on the
    original ticket, and the full 7s applies with no ticket at all (restore without one).
    An already-expired deadline must launch no native process whatsoever."""
    public = _public(startup_timeout_seconds=7)

    near_deadline = datetime.now(timezone.utc) + timedelta(seconds=2)
    clipped = OpenCodeEngine._startup_deadline(public.engine.startup_timeout_seconds, near_deadline)
    remaining_clipped = clipped - time.monotonic()
    assert 0 < remaining_clipped <= 2.2

    unclipped = OpenCodeEngine._startup_deadline(public.engine.startup_timeout_seconds, None)
    remaining_unclipped = unclipped - time.monotonic()
    assert 6.8 < remaining_unclipped <= 7.0

    engine = OpenCodeEngine()
    spawn_calls: list[object] = []
    original_exec = opencode_module.asyncio.create_subprocess_exec

    async def tracking_exec(*args, **kwargs):
        spawn_calls.append(args)
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(opencode_module.asyncio, "create_subprocess_exec", tracking_exec)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    invocation = EngineInvocation(
        invocation_id="expired-1",
        session_ref="expired-session",
        workspace_dir=workspace_dir,
        prompt="must never launch",
        public=public,
        engine_env=dict(public.engine.env),
        deadline_at=expired,
    )

    with pytest.raises(NativeLaunchFailed, match="deadline already expired"):
        await engine.start(invocation, home=tmp_path / "home", scratch=tmp_path / "scratch")

    assert spawn_calls == []


async def test_readiness_bounds_each_request_by_the_remaining_startup_budget(tmp_path: Path) -> None:
    """A stalled readiness endpoint must not outlive the startup deadline: each request
    gets only the remaining budget instead of the 30s default request timeout."""
    session = opencode_module._NativeSession("stall", tmp_path / "home", tmp_path / "scratch")
    killed: list[bool] = []

    async def stalled_request(*_args: object, **_kwargs: object) -> tuple[int, object]:
        await asyncio.sleep(30)
        return 200, []

    async def kill(*, confirm_timeout: float) -> bool:
        killed.append(True)
        return True

    session.request = stalled_request  # type: ignore[method-assign]
    session._kill = kill  # type: ignore[method-assign]
    started_at = time.monotonic()
    with pytest.raises(NativeLaunchFailed, match="readiness timed out"):
        await opencode_module._await_native_readiness(
            session, wid="/workspace", managed_agent="build", required_mcp=[],
            provider_id="p", model_id="m", max_steps=1, deadline=time.monotonic() + 0.05,
        )
    assert time.monotonic() - started_at < 1.0
    assert killed == [True]

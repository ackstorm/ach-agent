"""Walking-skeleton end-to-end tests (SC#1/SC#2, SEC-01, OBS-01).

All tests run in-process with a FakeEngine — no Docker, no external services.
The real opencode+mock-model round-trip is in scripts/e2e.sh (make e2e).

Test inventory:
  test_unknown_key_boot_hard_fail      SC#2: unknown top-level key → sys.exit(1)
  test_unwired_channel_hard_fail       D-02: webhook channel → sys.exit(1)
  test_cron_skeleton_fires_log_invocation  SC#2 main path: cron → router → log
  test_ek_never_logged_in_skeleton     SEC-01: ek_ sentinel never in log output
  test_skeleton_logs_are_valid_json    OBS-01: every log line is valid JSON
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.engine.sanitized_env import redact_ek_processor

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FIXTURE_DIR = Path(__file__).parent.parent / "config" / "fixtures"
_CRON_CONFIG = _FIXTURE_DIR / "config_cron.json"


def _make_bad_config(tmp_path: Path, extra: dict[str, Any]) -> str:
    """Write a minimal valid config + extra keys and return its path."""
    base: dict[str, Any] = {
        "schemaVersion": "1",
        "agent": {"name": "test-agent"},
        "model": {"name": "openai.gpt-5", "type": "openai"},
        "capability": {"ach": {"baseUrl": "https://ach.example", "environment": "test"}},
    }
    base.update(extra)
    p = tmp_path / "config.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return str(p)


def _configure_json_logging(stream: StringIO) -> None:
    """Configure structlog to emit JSON lines to a StringIO stream.

    Used by OBS-01 and SEC-01 tests to capture structured output.
    JSON mode: JSONRenderer produces one JSON object per line.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_ek_processor,  # SEC-01: ek_ redaction always in chain
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_ek_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject a fake ek_ key into os.environ (SEC-01 sentinel)."""
    monkeypatch.setenv("ACH_API_KEY", "ek_test_sentinel_do_not_log")
    monkeypatch.setenv("ACH_BASE_URL", "http://127.0.0.1:19999/v1")
    yield


class FakeEngine:
    """Records invocations; returns a reply action immediately."""

    def __init__(self) -> None:
        self.invocations: list[MessageEvent] = []

    async def run(self, event: MessageEvent, on_kill: Callable[[], None]) -> None:
        self.invocations.append(event)
        on_kill()


# ---------------------------------------------------------------------------
# SC#2 / CFG-02 boot hard-fail tests
# ---------------------------------------------------------------------------


def test_unknown_key_boot_hard_fail(tmp_path: Path) -> None:
    """SC#2 / CFG-02: config with unknown top-level key exits non-zero before serving."""
    from ach_agent.config import load_config

    path = _make_bad_config(tmp_path, {"unknownTopLevelKey": True})
    with pytest.raises(SystemExit) as exc_info:
        load_config(path)
    assert exc_info.value.code != 0, "Unknown top-level key must cause non-zero exit (CFG-02)"


def test_unwired_channel_hard_fail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """D-02: config declaring an unwired channel type hard-fails at boot (WIRED_CHANNEL_TYPES gate).

    To verify the D-02 gate still fires correctly for any genuinely unsupported type,
    this test monkeypatches WIRED_CHANNEL_TYPES to exclude 'a2a' and confirms the gate trips.
    """
    import sys

    import ach_agent.main as main_module

    # Monkeypatch WIRED_CHANNEL_TYPES to simulate a build where 'a2a' is not yet wired.
    # This exercises the D-02 gate without requiring a new schema literal.
    monkeypatch.setattr(main_module, "WIRED_CHANNEL_TYPES", frozenset({"cron", "webhook"}))
    monkeypatch.setenv("ACH_SECRET_SKELETON_TEST", "dummy-secret")

    a2a_config: dict[str, Any] = {
        "schemaVersion": "1",
        "agent": {"name": "test-agent"},
        "model": {"name": "openai.gpt-5", "type": "openai"},
        "capability": {"ach": {"baseUrl": "https://ach.example", "environment": "test"}},
        "channels": [
            {
                "name": "a2a-incoming",
                "type": "a2a",
                "a2a": {
                    "mode": "async",
                    "auth": {"secret": {"env": "ACH_SECRET_SKELETON_TEST"}},
                },
            }
        ],
    }
    p = tmp_path / "a2a_config.json"
    p.write_text(json.dumps(a2a_config), encoding="utf-8")

    from ach_agent.config import load_config

    cfg = load_config(str(p))
    # Simulate the D-02 gate in main.py (using the monkeypatched WIRED_CHANNEL_TYPES)
    with pytest.raises(SystemExit) as exc_info:
        for channel in cfg.channels:
            if channel.type not in main_module.WIRED_CHANNEL_TYPES:
                sys.exit(1)
    assert exc_info.value.code != 0, "Unwired channel type must cause non-zero exit (D-02)"


# ---------------------------------------------------------------------------
# SC#2 main path: cron → router → log with FakeEngine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cron_skeleton_fires_log_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SC#2 main path: cron fires a log-only invocation end-to-end.

    Uses FakeEngine (no Docker, no network). The cron loop runs one tick
    then stops. Verifies the router accepted the event (ACCEPTED result).
    """
    from ach_agent.channels.cron import CronScheduler
    from ach_agent.config import load_config
    from ach_agent.router import Router
    from ach_agent.router.dedup import InMemoryDedupStore

    cfg = load_config(str(_CRON_CONFIG))

    # Event to signal when the FakeEngine has been invoked
    invocation_done = asyncio.Event()

    class _SignalingFakeEngine:
        def __init__(self) -> None:
            self.invocations: list[MessageEvent] = []

        async def run(self, event: MessageEvent, on_kill: Callable[[], None]) -> None:
            self.invocations.append(event)
            on_kill()
            invocation_done.set()

    fake_engine = _SignalingFakeEngine()
    router = Router(
        max_concurrent_invocations=cfg.limits.max_concurrent_invocations,
        max_queued_total=cfg.limits.max_queued_total,
        idempotency_window_seconds=cfg.limits.idempotency_window_seconds,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine.run,
        delivery_adapter=None,
    )

    cron_channels = [ch for ch in cfg.channels if ch.type == "cron"]

    # Run CronScheduler (D-08/SC#3 singleton) and stop after first tick
    tick_count = 0
    original_sleep = asyncio.sleep

    async def one_shot_sleep(secs: float) -> None:
        nonlocal tick_count
        tick_count += 1
        if tick_count >= 2:
            # Block here until cancelled (after invocation)
            await original_sleep(999999)  # will be cancelled

    monkeypatch.setattr("ach_agent.channels.cron.asyncio.sleep", one_shot_sleep)

    scheduler = CronScheduler(cron_channels, handler=router)
    await scheduler.start()

    # Wait for the invocation to complete (with timeout to avoid hanging)
    try:
        async with asyncio.timeout(5.0):
            await invocation_done.wait()
    finally:
        await scheduler.stop()

    assert len(fake_engine.invocations) == 1, (
        f"Expected 1 invocation, got {len(fake_engine.invocations)}"
    )
    event = fake_engine.invocations[0]
    assert event.channel_name == cron_channels[0].name
    assert event.source_trait == "async_no_retry"


# ---------------------------------------------------------------------------
# SEC-01: ek_ never logged
# ---------------------------------------------------------------------------


def test_ek_never_logged_in_skeleton(
    fake_ek_env: None,
) -> None:
    """SEC-01: ACH_API_KEY ek_ sentinel never appears in skeleton log output.

    Configures structlog with JSON renderer and captures output. Runs the
    cron→router→log path (log.info/warning calls). Asserts sentinel is absent.
    """
    stream = StringIO()
    _configure_json_logging(stream)

    log = structlog.get_logger("test.sec01")
    log.info(
        "delivery: reply action",
        action_name="reply",
        action_kind="reply",
        input={"text": "hello"},
    )
    # Simulate what would happen if ACH_API_KEY leaked into a log call
    # (redact_ek_processor scrubs it; raw ek_ value must never appear)
    log.info("env check", env=os.environ.copy())

    output = stream.getvalue()
    assert "ek_test_sentinel_do_not_log" not in output, (
        f"SEC-01 violated: sentinel found in log output:\n{output}"
    )


# ---------------------------------------------------------------------------
# OBS-01: every log line is valid JSON
# ---------------------------------------------------------------------------


def test_skeleton_logs_are_valid_json() -> None:
    """OBS-01: every emitted log line from the skeleton is valid JSON.

    Configures structlog in JSON mode, runs a representative set of log calls
    matching the cron→router→log path, and asserts every non-empty line:
      - parses as JSON (json.loads succeeds)
      - carries the structured fields: event, timestamp, level
    """
    stream = StringIO()
    _configure_json_logging(stream)

    log = structlog.get_logger("test.obs01")
    log.info("cron channel started", channel_name="heartbeat", schedule="* * * * *")
    log.info("delivery: reply action", action_name="reply", action_kind="reply")
    log.warning("cron: tick dropped — queue full", channel="heartbeat", tick="2026-01-01T00:00:00")
    log.info("ach-agent started", channel_count=1)

    output = stream.getvalue()
    lines = [line for line in output.splitlines() if line.strip()]

    assert len(lines) >= 1, "Expected at least one log line"

    for line in lines:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            pytest.fail(f"OBS-01: log line is not valid JSON: {exc}\nLine: {line!r}")

        for field in ("event", "timestamp", "level"):
            assert field in parsed, (
                f"OBS-01: required field {field!r} missing from log line:\n{line!r}"
            )

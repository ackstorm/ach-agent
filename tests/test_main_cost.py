# SPDX-License-Identifier: Apache-2.0
"""Task 1.10 turn-boundary cost override tests (A.9, AC-2)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from prometheus_client import REGISTRY
from structlog.testing import capture_logs

from ach_agent.channels.message_event import MessageEvent
from ach_agent.engine.base.events import OpenCodeUsage
from ach_agent.engine.cost import CostAccountant, ModelPrices, PriceTable, TokenUsage
from ach_agent.main import _make_engine_runner
from ach_agent.stats import metrics
from ach_agent.stats.sink import StatsSink


MODEL = "task-1-10-model"
CHANNEL = "task-1-10-channel"


def _counter_value() -> float:
    return REGISTRY.get_sample_value(
        "ach_agent_turn_cost_usd_total", {"model": MODEL, "channel": CHANNEL}
    ) or 0.0


def _event() -> MessageEvent:
    return MessageEvent(
        idempotency_key="task-1-10-id",
        session_key="task-1-10-session",
        channel_name=CHANNEL,
        payload={"scheduled_tick": "prompt"},
    )


def _usage(cost: float = 99.0) -> OpenCodeUsage:
    return OpenCodeUsage(
        session_id="session",
        message_id="message",
        input_tokens=12,
        output_tokens=3,
        cache_read=4,
        cache_write=5,
        cost=cost,
        duration_ms=250,
    )


async def _run_turn(
    *,
    source: str,
    accountant: CostAccountant | None,
    usage: OpenCodeUsage | None,
) -> tuple[list[object], list[dict[str, object]], float]:
    token = accountant.mint_token() if accountant is not None else ""
    server = SimpleNamespace(cost_token=token)
    pool = SimpleNamespace(
        acquire=AsyncMock(return_value=server),
        release=AsyncMock(),
        sessions={},
    )
    driver = SimpleNamespace()
    recorded: list[object] = []

    def on_record(stat: object) -> None:
        recorded.append(stat)
        metrics.observe(stat)  # type: ignore[arg-type]

    stats_sink = StatsSink(None, on_record=on_record)
    summaries: list[dict[str, object]] = []

    async def fake_run_contract_turn(*args: object, **kwargs: object) -> dict[str, object]:
        stats = kwargs["stats"]
        assert isinstance(stats, dict)
        if usage is not None:
            stats["usage"] = usage
        stats["tool_count"] = 2
        if accountant is not None and usage is not None:
            accountant.record_usage(
                token,
                TokenUsage(
                    prompt_tokens=10,
                    completion_tokens=2,
                    cached_read_tokens=0,
                    cache_creation_tokens=0,
                ),
            )
        return {"action": "none", "text": "done"}

    with patch("ach_agent.engine.base.terminal.run_contract_turn", fake_run_contract_turn):
        runner = _make_engine_runner(
            pool=pool,
            driver=driver,
            engine_cfg=SimpleNamespace(model=MODEL),
            max_invocation_seconds=30,
            stats_sink=stats_sink,
            accountant=accountant,
            cost_source=source,
        )
        with capture_logs() as logs:
            await runner(_event(), lambda: None)
    summaries.extend(record for record in logs if record.get("event") == "engine: summary")
    return recorded, summaries, _counter_value()


@pytest.mark.asyncio
async def test_litellm_usage_override_is_identical_in_summary_stat_and_counter() -> None:
    table = PriceTable("http://unused", "ek")
    table._prices[MODEL] = ModelPrices(0.1, 0.2, 0.1, 0.1)
    accountant = CostAccountant("litellm_usage", "openai", table, MODEL)
    before = _counter_value()

    recorded, summaries, after = await _run_turn(
        source="litellm_usage", accountant=accountant, usage=_usage()
    )

    expected = 10 * 0.1 + 2 * 0.2
    assert len(recorded) == 1
    assert len(summaries) == 1
    assert recorded[0].cost == pytest.approx(expected)  # type: ignore[attr-defined]
    assert summaries[0]["cost_usd"] == pytest.approx(expected)
    assert after - before == pytest.approx(expected)


@pytest.mark.asyncio
async def test_engine_source_preserves_engine_cost_in_summary_stat_and_counter() -> None:
    before = _counter_value()

    recorded, summaries, after = await _run_turn(source="engine", accountant=None, usage=_usage())

    assert recorded[0].cost == 99.0  # type: ignore[attr-defined]
    assert summaries[0]["cost_usd"] == 99.0
    assert after - before == 99.0


@pytest.mark.asyncio
async def test_none_source_suppresses_cost_but_preserves_the_session_record() -> None:
    before = _counter_value()

    recorded, summaries, after = await _run_turn(source="none", accountant=None, usage=_usage())

    stat = recorded[0]
    assert stat.cost == 0.0  # type: ignore[attr-defined]
    assert stat.input_tokens == 12  # type: ignore[attr-defined]
    assert stat.output_tokens == 3  # type: ignore[attr-defined]
    assert stat.cache_read == 4  # type: ignore[attr-defined]
    assert stat.cache_write == 5  # type: ignore[attr-defined]
    assert stat.duration_ms == 250  # type: ignore[attr-defined]
    assert stat.status == "completed"  # type: ignore[attr-defined]
    assert summaries[0]["cost_usd"] == 0.0
    assert after - before == 0.0


@pytest.mark.asyncio
async def test_missing_usage_record_is_normalized_without_raising() -> None:
    accountant = CostAccountant("litellm_usage", "openai", None, MODEL)
    recorded, summaries, _after = await _run_turn(
        source="litellm_usage", accountant=accountant, usage=None
    )

    assert len(recorded) == 1
    assert recorded[0].cost == 0.0  # type: ignore[attr-defined]
    assert summaries[0]["cost_usd"] == 0.0


@pytest.mark.asyncio
async def test_price_load_wiring_is_only_active_for_litellm_usage() -> None:
    calls: list[tuple[str, str]] = []

    class FakePriceTable:
        def __init__(self, base_url: str, token: str) -> None:
            calls.append((base_url, token))

        async def load(self, model_name: str) -> object:
            calls.append(("load", model_name))
            return SimpleNamespace(failure=None)

    with patch("ach_agent.main.PriceTable", FakePriceTable):
        usage_table, usage_accountant = await _build_cost_accounting_for_test("litellm_usage")
        header_table, header_accountant = await _build_cost_accounting_for_test("litellm_headers")
        none_table, none_accountant = await _build_cost_accounting_for_test("none")

    assert usage_table is not None
    assert usage_accountant is not None
    assert header_table is None and header_accountant is not None
    assert none_table is None and none_accountant is None
    assert calls == [("http://post-auth", "ek"), ("load", MODEL)]


async def _build_cost_accounting_for_test(source: str) -> tuple[object | None, object | None]:
    """The production seam is added by Task 1.10; this keeps the RED test explicit."""
    from ach_agent.main import _build_cost_accounting

    return await _build_cost_accounting(
        source=source,
        wire="openai",
        model_name=MODEL,
        model_up_base="http://post-auth",
        model_up_token="ek",
    )

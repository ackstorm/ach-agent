from datetime import datetime
from zoneinfo import ZoneInfo

from app.aggregate import build_contract, build_leaderboard, build_totals, month_start_ms


def _row(**over):
    base = dict(
        ts_ms=1,
        session_key="k",
        channel="cron",
        source="cron",
        model="glm-5-2",
        task="t",
        input_tokens=100,
        output_tokens=50,
        cache_read=0,
        cache_write=0,
        cost=0.01,
        turns=1,
        duration_ms=1000,
        tokens_per_s=50.0,
        status="completed",
        retry=False,
    )
    base.update(over)
    return base


def test_totals_sum_and_count_include_aborted():
    rows = [
        _row(cost=0.10, input_tokens=100, output_tokens=50),
        _row(cost=0.20, input_tokens=200, output_tokens=100, status="aborted"),
    ]
    t = build_totals(rows)
    assert t["sessions"] == 2
    assert round(t["spend"], 2) == 0.30
    assert t["tokens"] == 450  # (100+50)+(200+100)
    assert t["aborted"] == 1
    assert round(t["avg_cost_per_session"], 3) == 0.15


def test_totals_empty_guards_denominator():
    t = build_totals([])
    assert t["sessions"] == 0
    assert t["avg_cost_per_session"] is None  # null, not 0/0


def test_leaderboard_groups_by_model_sorted_by_spend_desc():
    rows = [
        _row(model="glm-5-2", cost=0.10, output_tokens=100, duration_ms=1000),
        _row(model="claude-opus-4-8", cost=0.90, output_tokens=100, duration_ms=1000),
        _row(model="glm-5-2", cost=0.10, output_tokens=100, duration_ms=1000),
    ]
    lb = build_leaderboard(rows)
    assert lb["sorted_by"] == "spend"
    assert lb["rows"][0]["model"] == "claude-opus-4-8"
    assert lb["rows"][0]["rank"] == 1
    assert lb["rows"][1]["model"] == "glm-5-2"
    assert lb["rows"][1]["sessions"] == 2
    assert lb["rows"][1]["spend"] == 0.20
    assert lb["rows"][0]["provider"] == "Anthropic"
    assert lb["rows"][0]["tag"] == "Frontier"
    assert lb["rows"][0]["score"] is None  # eval seam, filled by B


def test_leaderboard_derived_fields():
    rows = [
        _row(model="glm-5-2", cost=0.50, output_tokens=1000, duration_ms=2000, input_tokens=1000)
    ]
    r = build_leaderboard(rows)["rows"][0]
    assert r["speed_tok_s"] == 500.0  # 1000 tok / 2 s
    assert r["cost_per_mtok"] == 250.0  # 0.50 / (2000 tokens / 1e6)


def test_month_start_respects_tz():
    # 2026-03-01 00:30 Madrid (UTC+1) == 2026-02-28 23:30 UTC.
    # month_start in Madrid = Mar 1 00:00 CET.
    now = int(datetime(2026, 3, 1, 0, 30, tzinfo=ZoneInfo("Europe/Madrid")).timestamp() * 1000)
    ms = month_start_ms(now, "Europe/Madrid")
    got = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("Europe/Madrid"))
    assert (got.year, got.month, got.day, got.hour) == (2026, 3, 1, 0)


def test_contract_partial_flags_when_coverage_after_window_start():
    now = 2_000_000_000_000
    contract = build_contract(
        window_rows=[_row(cost=0.1)],
        recent_rows=[_row(cost=0.1)],
        coverage_start_ms=1_999_999_999_999,  # later than range_start -> partial
        now_ms=now,
        tz="UTC",
        range_start_ms=1_000_000_000_000,
        range_end_ms=now,
    )
    assert contract["totals"]["partial"] is True
    assert contract["range"]["coverage_start"] == 1_999_999_999_999
    assert contract["range"]["tz"] == "UTC"


def test_contract_not_partial_when_full_coverage():
    now = 2_000_000_000_000
    contract = build_contract(
        window_rows=[_row(cost=0.1)],
        recent_rows=[_row(cost=0.1)],
        coverage_start_ms=500_000_000_000,  # earlier than range_start -> complete
        now_ms=now,
        tz="UTC",
        range_start_ms=1_000_000_000_000,
        range_end_ms=now,
    )
    assert contract["totals"]["partial"] is False


def test_contract_recent_shape():
    now = 2_000_000_000_000
    contract = build_contract(
        window_rows=[],
        recent_rows=[_row(task="Review !7", status="aborted", retry=True)],
        coverage_start_ms=None,
        now_ms=now,
        tz="UTC",
        range_start_ms=1_000_000_000_000,
        range_end_ms=now,
    )
    rec = contract["recent"][0]
    assert rec["task"] == "Review !7"
    assert rec["status"] == "aborted"
    assert rec["retry"] is True

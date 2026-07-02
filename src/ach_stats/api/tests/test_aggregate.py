from app.aggregate import build_leaderboard, build_totals


def _row(**over):
    base = dict(
        ts_ms=1, session_key="k", channel="cron", source="cron", model="glm-5-2", task="t",
        input_tokens=100, output_tokens=50, cache_read=0, cache_write=0, cost=0.01, turns=1,
        duration_ms=1000, tokens_per_s=50.0, status="completed", retry=False,
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
    rows = [_row(model="glm-5-2", cost=0.50, output_tokens=1000, duration_ms=2000,
                 input_tokens=1000)]
    r = build_leaderboard(rows)["rows"][0]
    assert r["speed_tok_s"] == 500.0            # 1000 tok / 2 s
    assert r["cost_per_mtok"] == 250.0          # 0.50 / (2000 tokens / 1e6)

# SPDX-License-Identifier: Apache-2.0
"""Pure aggregation: parsed rows -> page-ready leaderboard contract (spec §4.4).

null-vs-0 discipline: an UNAVAILABLE figure (0 denominator) is None; a genuine zero stays 0.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

# Static model -> (provider, tag) map. provider/tag are metadata, never measured (spec §4.4).
_META: dict[str, tuple[str, str | None]] = {
    "claude-opus-4-8": ("Anthropic", "Frontier"),
    "claude-fable-5": ("Anthropic", "Mythos-tier"),
    "claude-sonnet-5": ("Anthropic", "Balanced"),
    "glm-5-2": ("Zhipu AI", "Open Weight"),
}


def resolve(model: str) -> tuple[str, str | None]:
    return _META.get(model, ("unknown", None))


def _safe_div(n: float, d: float) -> float | None:
    return (n / d) if d else None


def to_recent_row(r: dict[str, Any]) -> dict[str, Any]:
    """A parsed row -> the page-ready `recent[]` shape (shared with /api/sessions)."""
    return {
        "ts": r["ts_ms"],
        "task": r["task"],
        "model": r["model"],
        "tokens": r["input_tokens"] + r["output_tokens"],
        "cost": r["cost"],
        "turns": r["turns"],
        "status": r["status"],
        "retry": r["retry"],
    }


def build_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sessions = len(rows)
    spend = sum(r["cost"] for r in rows)
    tokens = sum(r["input_tokens"] + r["output_tokens"] for r in rows)
    aborted = sum(1 for r in rows if r["status"] == "aborted")
    return {
        "sessions": sessions,
        "tokens": tokens,
        "spend": spend,
        "aborted": aborted,
        "avg_cost_per_session": _safe_div(spend, sessions),
    }


def build_leaderboard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_model: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "spend": 0.0,
            "sessions": 0.0,
            "output_tokens": 0.0,
            "duration_ms": 0.0,
            "total_tokens": 0.0,
        }
    )
    for r in rows:
        m = by_model[r["model"]]
        m["spend"] += r["cost"]
        m["sessions"] += 1
        m["output_tokens"] += r["output_tokens"]
        m["duration_ms"] += r["duration_ms"]
        m["total_tokens"] += r["input_tokens"] + r["output_tokens"]

    out: list[dict[str, Any]] = []
    for model, m in by_model.items():
        provider, tag = resolve(model)
        speed = _safe_div(m["output_tokens"], m["duration_ms"] / 1000.0)
        cost_per_mtok = _safe_div(m["spend"], m["total_tokens"] / 1_000_000.0)
        out.append(
            {
                "rank": 0,  # assigned after sort
                "model": model,
                "provider": provider,
                "tag": tag,
                "score": None,  # eval seam — Sub-project B fills this
                "speed_tok_s": speed,
                "cost_per_mtok": cost_per_mtok,
                "spend": m["spend"],
                "sessions": int(m["sessions"]),
            }
        )

    out.sort(key=lambda r: r["spend"], reverse=True)
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return {"sorted_by": "spend", "rows": out}


def month_start_ms(now_ms: int, tz: str) -> int:
    zone = ZoneInfo(tz)
    now = datetime.fromtimestamp(now_ms / 1000, tz=zone)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _day_key(ts_ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo(tz)).strftime("%Y-%m-%d")


def build_contract(
    *,
    window_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
    coverage_start_ms: int | None,
    now_ms: int,
    tz: str,
    range_start_ms: int,
    range_end_ms: int,
) -> dict[str, Any]:
    partial = coverage_start_ms is not None and coverage_start_ms > range_start_ms

    totals = build_totals(window_rows)
    totals["partial"] = partial

    leaderboard = build_leaderboard(window_rows)

    # cost per session by model (avg cost per invocation).
    cps: list[dict[str, Any]] = []
    for row in leaderboard["rows"]:
        avg = _safe_div(row["spend"], row["sessions"])
        cps.append({"model": row["model"], "avg": avg})

    # calendar month-to-date, in tz.
    m_start = month_start_ms(now_ms, tz)
    month_rows = [r for r in window_rows if r["ts_ms"] >= m_start]
    month_counts: dict[str, int] = defaultdict(int)
    for r in month_rows:
        month_counts[r["model"]] += 1
    sessions_this_month = {
        "rows": [
            {"model": m, "count": c}
            for m, c in sorted(month_counts.items(), key=lambda kv: kv[1], reverse=True)
        ],
        "partial": coverage_start_ms is not None and coverage_start_ms > m_start,
    }

    # daily series.
    day_acc: dict[str, dict[str, float]] = defaultdict(
        lambda: {"spend": 0.0, "sessions": 0.0, "tokens": 0.0}
    )
    for r in window_rows:
        d = day_acc[_day_key(r["ts_ms"], tz)]
        d["spend"] += r["cost"]
        d["sessions"] += 1
        d["tokens"] += r["input_tokens"] + r["output_tokens"]
    series = [
        {
            "date": day,
            "spend": v["spend"],
            "sessions": int(v["sessions"]),
            "tokens": int(v["tokens"]),
            "partial": coverage_start_ms is not None
            and coverage_start_ms
            > int(
                datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ZoneInfo(tz)).timestamp() * 1000
            ),
        }
        for day, v in sorted(day_acc.items())
    ]

    recent = [to_recent_row(r) for r in recent_rows]

    days = max(1, round((range_end_ms - range_start_ms) / 86_400_000))
    return {
        "range": {
            "start": range_start_ms,
            "end": range_end_ms,
            "days": days,
            "coverage_start": coverage_start_ms,
            "tz": tz,
        },
        "totals": totals,
        "leaderboard": leaderboard,
        "cost_per_session": cps,
        "sessions_this_month": sessions_this_month,
        "series": series,
        "recent": recent,
    }

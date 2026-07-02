# SPDX-License-Identifier: Apache-2.0
"""Pure aggregation: parsed rows -> page-ready leaderboard contract (spec §4.4).

null-vs-0 discipline: an UNAVAILABLE figure (0 denominator) is None; a genuine zero stays 0.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.model_meta import resolve


def _safe_div(n: float, d: float) -> float | None:
    return (n / d) if d else None


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
        lambda: {"spend": 0.0, "sessions": 0.0, "output_tokens": 0.0, "duration_ms": 0.0,
                 "total_tokens": 0.0}
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
        out.append({
            "rank": 0,  # assigned after sort
            "model": model,
            "provider": provider,
            "tag": tag,
            "score": None,  # eval seam — Sub-project B fills this
            "speed_tok_s": speed,
            "cost_per_mtok": cost_per_mtok,
            "spend": m["spend"],
            "sessions": int(m["sessions"]),
        })

    out.sort(key=lambda r: r["spend"], reverse=True)
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return {"sorted_by": "spend", "rows": out}

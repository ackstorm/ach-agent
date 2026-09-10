# SPDX-License-Identifier: Apache-2.0
import asyncio
import time

import fakeredis.aioredis
from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz():
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def _seed_app():
    app = create_app()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.redis = fake
    app.state.tz = "UTC"
    now = int(time.time() * 1000)

    async def seed():
        await fake.xadd(
            "ach:sessions",
            {
                "v": "1",
                "model": "claude-opus-4-8",
                "cost": "0.90",
                "output_tokens": "100",
                "duration_ms": "1000",
                "input_tokens": "100",
                "status": "completed",
                "turns": "2",
                "task": "Set up flags",
            },
            id=f"{now - 1000}-0",
        )
        await fake.xadd(
            "ach:sessions",
            {
                "v": "1",
                "model": "glm-5-2",
                "cost": "0.10",
                "output_tokens": "50",
                "duration_ms": "1000",
                "input_tokens": "50",
                "status": "completed",
                "turns": "1",
                "task": "Add pagination",
            },
            id=f"{now - 500}-0",
        )

    # No running loop in this sync test function, so asyncio.run() is safe (unlike
    # asyncio.get_event_loop().run_until_complete(), deprecated since 3.10/3.12).
    asyncio.run(seed())
    return app


def test_leaderboard_route():
    client = TestClient(_seed_app())
    r = client.get("/api/leaderboard?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["leaderboard"]["sorted_by"] == "spend"
    assert body["leaderboard"]["rows"][0]["model"] == "claude-opus-4-8"
    assert body["totals"]["sessions"] == 2


def test_sessions_route():
    client = TestClient(_seed_app())
    r = client.get("/api/sessions?n=10")
    assert r.status_code == 200
    assert r.json()["recent"][0]["model"] == "glm-5-2"  # newest first


def test_leaderboard_monthly_counts_independent_of_selected_range():
    """finding 12: month-to-date counts must reflect the WHOLE month, not just
    whatever the selected day-range happens to cover. Two sessions land in
    September (day 2 and day 19, fixed 'now' = day 20); a 7-day range only
    covers day 19's session, but monthly counts must still be 2 for both a
    7-day and a 30-day request."""
    from datetime import UTC, datetime
    from unittest.mock import patch

    app = create_app()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.redis = fake
    app.state.tz = "UTC"

    fixed_now = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)
    sept_2_ms = int(datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)
    sept_19_ms = int(datetime(2026, 9, 19, 12, 0, 0, tzinfo=UTC).timestamp() * 1000)

    async def seed():
        for ts in (sept_2_ms, sept_19_ms):
            await fake.xadd(
                "ach:sessions",
                {
                    "v": "1",
                    "model": "glm-5-2",
                    "cost": "0.10",
                    "output_tokens": "50",
                    "duration_ms": "1000",
                    "input_tokens": "50",
                    "status": "completed",
                    "turns": "1",
                    "task": "t",
                },
                id=f"{ts}-0",
            )

    asyncio.run(seed())

    client = TestClient(app)
    with patch("app.main.time.time", return_value=fixed_now.timestamp()):
        resp_7 = client.get("/api/leaderboard?days=7")
        resp_30 = client.get("/api/leaderboard?days=30")

    assert resp_7.status_code == 200
    assert resp_30.status_code == 200
    body_7 = resp_7.json()
    body_30 = resp_30.json()

    # Selected-range totals DO depend on the range (the bug/fix boundary).
    assert body_7["totals"]["sessions"] == 1, "7-day range only covers the Sept 19 session"
    assert body_30["totals"]["sessions"] == 2, "30-day range covers both sessions"

    # Monthly counts must be independent of the selected range.
    month_count_7 = sum(r["count"] for r in body_7["sessions_this_month"]["rows"])
    month_count_30 = sum(r["count"] for r in body_30["sessions_this_month"]["rows"])
    assert month_count_7 == 2, "monthly count must include both September sessions"
    assert month_count_30 == 2, "monthly count must match regardless of the wider range"

    # The coverage/partial flag is a property of retained data vs month start —
    # it must be reported the SAME way regardless of the selected range.
    assert body_7["sessions_this_month"]["partial"] == body_30["sessions_this_month"]["partial"]

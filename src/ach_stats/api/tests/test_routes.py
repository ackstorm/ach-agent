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
        await fake.xadd("ach:sessions",
                        {"v": "1", "model": "claude-opus-4-8", "cost": "0.90",
                         "output_tokens": "100", "duration_ms": "1000", "input_tokens": "100",
                         "status": "completed", "turns": "2", "task": "Set up flags"},
                        id=f"{now - 1000}-0")
        await fake.xadd("ach:sessions",
                        {"v": "1", "model": "glm-5-2", "cost": "0.10", "output_tokens": "50",
                         "duration_ms": "1000", "input_tokens": "50", "status": "completed",
                         "turns": "1", "task": "Add pagination"},
                        id=f"{now - 500}-0")

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

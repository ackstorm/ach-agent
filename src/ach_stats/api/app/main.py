# SPDX-License-Identifier: Apache-2.0
"""FastAPI app: leaderboard/sessions JSON + the built SPA. See design spec §6."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any, cast

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.aggregate import build_contract
from app.reader import read_coverage_start, read_recent, read_window


def _redis(request: Request) -> Any:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        import redis.asyncio as redis_asyncio

        # redis.asyncio.from_url has no return annotation upstream, so --strict flags it
        # as an untyped call. Route it through a typed factory alias.
        from_url = cast(Callable[..., Any], redis_asyncio.from_url)
        client = from_url(os.environ["ACH_STATS_REDIS_URL"], decode_responses=True)
        request.app.state.redis = client
    return client


def _tz(request: Request) -> str:
    return getattr(request.app.state, "tz", os.environ.get("ACH_STATS_TZ", "UTC"))


def create_app() -> FastAPI:
    app = FastAPI(title="ach-stats")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/leaderboard")
    async def leaderboard(request: Request, days: int = Query(30, ge=1, le=62)) -> JSONResponse:
        client = _redis(request)
        now = int(time.time() * 1000)
        start = now - days * 86_400_000
        window = await read_window(client, start, now)
        recent = await read_recent(client, 12)
        coverage = await read_coverage_start(client)
        contract = build_contract(
            window_rows=window,
            recent_rows=recent,
            coverage_start_ms=coverage,
            now_ms=now,
            tz=_tz(request),
            range_start_ms=start,
            range_end_ms=now,
        )
        return JSONResponse(contract)

    @app.get("/api/sessions")
    async def sessions(request: Request, n: int = Query(50, ge=1, le=200)) -> JSONResponse:
        client = _redis(request)
        recent = await read_recent(client, n)
        payload = [
            {
                "ts": r["ts_ms"],
                "task": r["task"],
                "model": r["model"],
                "tokens": r["input_tokens"] + r["output_tokens"],
                "cost": r["cost"],
                "turns": r["turns"],
                "status": r["status"],
                "retry": r["retry"],
            }
            for r in recent
        ]
        return JSONResponse({"recent": payload})

    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parent.parent / "ui" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="spa")

    return app

# SPDX-License-Identifier: Apache-2.0
"""FastAPI app: leaderboard/sessions JSON + the built SPA. See design spec §6."""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="ach-stats")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app

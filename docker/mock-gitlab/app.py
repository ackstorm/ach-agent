"""Mock GitLab receiver for ach-agent e2e testing.

Captures MR note POST calls and exposes an /assert endpoint so e2e tests
can verify that the harness delivered the expected note.

DEV TOOLING ONLY — never used in production.

Endpoints:
  POST /api/v4/projects/{project_id}/merge_requests/{iid}/notes  — capture note
  GET  /assert                                                    — return captured notes
  GET  /health                                                    — health check
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s mock-gitlab %(message)s")
log = logging.getLogger("mock-gitlab")

app = FastAPI(title="mock-gitlab")

# In-memory capture list — reset at process start; safe for single-test processes
_notes: list[dict] = []


@app.post("/api/v4/projects/{project_id}/merge_requests/{iid}/notes")
async def capture_note(project_id: int, iid: int, request: Request) -> JSONResponse:
    """Capture a GitLab MR note POST (mirrors the real GitLab API).

    Stores {project_id, iid, body} in _notes for later /assert retrieval.
    Returns 201 Created with a minimal note object.
    """
    body = await request.json()
    note_body = body.get("body", "")
    _notes.append({"project_id": project_id, "iid": iid, "body": note_body})
    log.info("captured note project_id=%s iid=%s body_len=%s", project_id, iid, len(note_body))
    return JSONResponse(
        {"id": len(_notes), "body": note_body, "author": {"id": 1, "username": "ach-bot"}},
        status_code=201,
    )


@app.get("/assert")
async def get_notes() -> JSONResponse:
    """Return all captured notes for e2e assertion.

    E2e tests poll this endpoint (bounded asyncio.timeout) to verify the harness
    delivered the expected note after an engine run.
    """
    return JSONResponse({"notes": _notes})


@app.get("/health")
async def health() -> JSONResponse:
    """Health check — always 200 while process is alive."""
    return JSONResponse({"status": "ok"})

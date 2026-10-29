#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Positive-readiness variant of `fake_native_server.Server` for the mini TCP boot test.

The base fake deliberately answers `{}` at `/api/model` and `/api/agent/*` so startup-policy
tests can only end on the shared deadline. This keeps every base behavior and adds what a
configured native reports: the configured model, the managed agent with the configured
step limit, and a session GET echoing the location/model/agent the session was created with.

Usage: ready_native_server.py PROVIDER_ID MODEL_ID MAX_STEPS
"""

from __future__ import annotations

import asyncio
import json
import sys

from fake_native_server import Server, main


class ReadyServer(Server):
    def __init__(self, provider_id: str, model_id: str, max_steps: int) -> None:
        super().__init__()
        self._model = {"providerID": provider_id, "id": model_id, "enabled": True}
        self._max_steps = max_steps
        self._identity: dict[str, dict] = {}

    async def handle(self, method: str, path: str, body: bytes) -> tuple[int, bytes, dict]:
        path_only = path.partition("?")[0]
        if method == "GET" and path_only == "/api/model":
            return 200, json.dumps({"data": [self._model]}).encode(), {}
        if method == "GET" and path_only.startswith("/api/agent/"):
            name = path_only.rsplit("/", 1)[-1]
            agent = {"id": name, "steps": self._max_steps, "system": "managed system prompt"}
            return 200, json.dumps({"data": agent}).encode(), {}
        if method == "GET" and path_only.startswith("/api/session/") and path_only.count("/") == 3:
            sid = path_only.rsplit("/", 1)[-1]
            if sid in self._identity:
                return 200, json.dumps({"data": {"id": sid, **self._identity[sid]}}).encode(), {}
        status, resp, headers = await super().handle(method, path, body)
        if method == "POST" and path_only == "/api/session" and status == 200:
            created = json.loads(body) if body else {}
            sid = json.loads(resp)["data"]["id"]
            self._identity[sid] = {key: created.get(key) for key in ("location", "model", "agent")}
        return status, resp, headers


if __name__ == "__main__":
    asyncio.run(main(ReadyServer(sys.argv[1], sys.argv[2], int(sys.argv[3]))))

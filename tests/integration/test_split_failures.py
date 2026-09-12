# SPDX-License-Identifier: Apache-2.0
"""Failure and boundary assertions for the real split acceptance tooling."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.asyncio
async def test_controlled_upstream_rejects_missing_harness_auth() -> None:
    from tests.integration.fixtures.upstream import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://fixture") as client:
        denied = await client.post("/platform/hydrate")
        allowed = await client.post(
            "/platform/hydrate", headers={"x-ach-key": "split-acceptance-key"}
        )
    assert denied.status_code == 401
    assert allowed.status_code == 200

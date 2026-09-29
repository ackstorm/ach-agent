# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from ach_agent.sandbox.claims import AGENT_LABEL, ClaimClient, claim_body, ready_ip

NOW = dt.datetime(2026, 9, 29, 12, 0, tzinfo=dt.UTC)


def _client(tmp_path: Path, handler) -> ClaimClient:
    token = tmp_path / "token"
    token.write_text("sa-token\n")
    return ClaimClient(
        "ach",
        "wp",
        base_url="https://k8s",
        token_path=token,
        transport=httpx.MockTransport(handler),
    )


def test_claim_body_shape_and_no_env() -> None:
    body = claim_body("c1", {AGENT_LABEL: "bot"}, "wp", NOW)
    assert body["apiVersion"] == "extensions.agents.x-k8s.io/v1beta1"
    assert body["spec"]["warmPoolRef"] == {"name": "wp"}
    assert body["spec"]["lifecycle"] == {
        "shutdownPolicy": "Delete",
        "shutdownTime": "2026-09-29T12:00:00Z",
    }
    assert "env" not in body["spec"]  # P9: never values in the claim


def test_ready_ip_requires_ready_and_ip() -> None:
    ready = {"type": "Ready", "status": "True"}
    assert (
        ready_ip({"status": {"conditions": [ready], "sandbox": {"podIPs": ["10.0.0.7"]}}})
        == "10.0.0.7"
    )
    assert ready_ip({"status": {"conditions": [ready], "sandbox": {}}}) is None
    assert ready_ip({"status": {"conditions": [], "sandbox": {"podIPs": ["10.0.0.7"]}}}) is None


async def test_create_get_touch_list_delete(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Bearer sa-token"
        if request.method == "GET" and request.url.path.endswith("/missing"):
            return httpx.Response(404)
        if request.method == "DELETE":
            return httpx.Response(404)
        return httpx.Response(200, json={"items": []})

    client = _client(tmp_path, handler)
    await client.create("c1", {AGENT_LABEL: "bot"}, NOW)
    assert await client.get("missing") is None
    await client.touch("c1", shutdown_at=NOW)
    await client.delete("c1")  # 404 is success
    await client.close()
    base = "/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/ach/sandboxclaims"
    assert seen[0].method == "POST" and seen[0].url.path == base
    assert seen[2].headers["content-type"] == "application/merge-patch+json"
    assert json.loads(seen[2].content) == {
        "spec": {"lifecycle": {"shutdownTime": "2026-09-29T12:00:00Z"}}
    }


async def test_wait_ready_and_wait_deleted_are_bounded(tmp_path: Path) -> None:
    client = _client(tmp_path, lambda r: httpx.Response(200, json={"status": {}}))
    with pytest.raises(TimeoutError):
        await client.wait_ready("c1", timeout=0.3, interval=0.05)
    with pytest.raises(TimeoutError):
        await client.wait_deleted("c1", timeout=0.3, interval=0.05)
    await client.close()

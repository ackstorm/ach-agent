# SPDX-License-Identifier: MIT
"""assemble() with capability.ach.key.exchange: exchange before hydrate, ACH's code on refusal."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.harness.control import assemble
from ach_runtime.harness.keys import ExchangeRefused


def _exchange_config(token_path: Path):
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    ach = wire["capability"]["ach"]
    ach["key"] = {"exchange": {"identity": "ai.run@example.com", "environment": ach["environment"],
                               "tokenPath": str(token_path)}}
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def _ach(exchange_reply: httpx.Response, seen: list[tuple[str, str | None, str | None]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("x-ach-key"), request.headers.get("authorization")))
        if request.url.path == "/platform/keys/exchange":
            return exchange_reply
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json={
                "environment": "engineering-prod",
                "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                            "mcpServers": [{"id": "mcp-x", "endpoint": "https://ach.ackstorm.ai/mcp/mcp-x"}], "a2aAgents": []},
                "context": {"skills": [], "prompts": [], "artifacts": []},
            })
        if request.url.path == "/v2/model/info":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url)
    return httpx.MockTransport(handler)


async def test_boot_exchanges_the_projected_token_before_hydrate(tmp_path) -> None:
    token = tmp_path / "token"
    token.write_text("sa-token\n")
    seen: list[tuple[str, str | None, str | None]] = []
    control = await assemble(
        _exchange_config(token), environ={"ACH_SANDBOX_KEY": "secret"},
        ach_transport=_ach(httpx.Response(200, json={"token": "jwt-1", "expires_in": 900, "key_id": "ekid_1"}), seen),
    )
    try:
        assert seen[0] == ("/platform/keys/exchange", None, "Bearer sa-token")
        assert ("/platform/hydrate", "jwt-1", None) in seen
        assert control.ach_key.current() == "jwt-1" and control.ach_key.ready()
        assert control._model_headers == {} and control._mcp_headers == {"mcp-x": {}}
        assert control.upstream_secrets == ()
    finally:
        await control.close()


async def test_a_refused_exchange_fails_boot_with_achs_code(tmp_path) -> None:
    token = tmp_path / "token"
    token.write_text("sa-token")
    with pytest.raises(ExchangeRefused, match="not_granted"):
        await assemble(
            _exchange_config(token), environ={"ACH_SANDBOX_KEY": "secret"},
            ach_transport=_ach(httpx.Response(403, json={"error": {"code": "not_granted", "message": "x"}}), []),
        )

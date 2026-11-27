# SPDX-License-Identifier: Apache-2.0
import httpx

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.harness.credential_proxy import create_proxy_app
from ach_runtime.harness.keys import KeySource


async def test_proxy_swaps_bearer_for_ach_key_and_refuses_others() -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app("https://ach.example/", KeySource("ek_test"), "bearer-1",
                           ProcessIdentity(agent="hermes", environment="search"),
                           httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
        ok = await client.post("/v1/chat/completions", json={"m": 1},
                               headers={"Authorization": "Bearer bearer-1", "x-ach-key": "forged"})
        mcp = await client.get("/mcp/mcp-serper", headers={"Authorization": "Bearer bearer-1"})
        wrong = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer nope"})
        other = await client.get("/platform/hydrate", headers={"Authorization": "Bearer bearer-1"})

    assert (ok.status_code, mcp.status_code, wrong.status_code, other.status_code) == (200, 200, 401, 404)
    assert [str(r.url) for r in seen] == ["https://ach.example/v1/chat/completions", "https://ach.example/mcp/mcp-serper"]
    sent = seen[0].headers
    assert sent["x-ach-key"] == "ek_test" and "authorization" not in sent
    assert (sent["x-ach-agent"], sent["x-ach-environment"]) == ("hermes", "search")

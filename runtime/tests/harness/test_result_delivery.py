# SPDX-License-Identifier: Apache-2.0
import json
from types import SimpleNamespace

import httpx
import pytest

from ach_runtime.channels.metrics import DELIVERY_FAILURES
from ach_runtime.harness.result_delivery import ResultDelivery


def _status(state: str, result=None, error=None):
    return SimpleNamespace(state=state, result=result, error=error)


@pytest.mark.asyncio
async def test_delivery_posts_the_final_result_with_its_headers() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    delivery = ResultDelivery(
        {"alerts": ("https://hooks.example.com/r", {"Authorization": "Bearer t"})},
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    delivery.send("alerts", "inv-1", _status("completed", {"text": "done"}), None)
    delivery.send("other", "inv-2", _status("completed", {"text": "x"}), None)  # no target: nothing sent
    await delivery.close()

    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer t"
    assert json.loads(seen[0].content) == {
        "invocation_id": "inv-1", "channel": "alerts", "state": "completed", "text": "done", "error": None}


@pytest.mark.asyncio
async def test_delivery_reports_a_run_that_raised_and_counts_a_failed_post() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(500)

    before = DELIVERY_FAILURES.labels(channel="alerts")._value.get()
    delivery = ResultDelivery({"alerts": ("https://hooks.example.com/r", {})},
                              client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    delivery.send("alerts", "inv-3", None, "sandbox did not start")
    await delivery.close()

    assert bodies == [{"invocation_id": "inv-3", "channel": "alerts", "state": "failed",
                       "text": None, "error": "sandbox did not start"}]
    assert DELIVERY_FAILURES.labels(channel="alerts")._value.get() == before + 1

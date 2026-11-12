# SPDX-License-Identifier: Apache-2.0
"""POST the final result of a run to its channel's `delivery.url`."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ach_runtime.channels.metrics import DELIVERY_FAILURES
from ach_runtime.observability.runs import redact

log = logging.getLogger(__name__)
_TIMEOUT_SECONDS = 10.0


class ResultDelivery:
    """One attempt per run, off the invocation's path: a slow receiver holds no permit."""

    def __init__(self, targets: dict[str, tuple[str, dict[str, str]]],
                 client: httpx.AsyncClient | None = None) -> None:
        self._targets = targets
        for channel in targets:
            DELIVERY_FAILURES.labels(channel=channel)
        self._client = client or httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, follow_redirects=False)
        self._tasks: set[asyncio.Task[None]] = set()

    def send(self, channel: str, invocation_id: str, status: Any, error: str | None) -> None:
        target = self._targets.get(channel)
        if target is None:
            return
        result = getattr(status, "result", None)
        text = result.get("text") if isinstance(result, dict) else None
        body = {
            "invocation_id": invocation_id,
            "channel": channel,
            "state": getattr(status, "state", "failed"),
            "text": text if isinstance(text, str) else None,
            "error": error if status is None else getattr(status, "error", None),
        }
        task = asyncio.get_running_loop().create_task(self._post(channel, invocation_id, target, body))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _post(self, channel: str, invocation_id: str, target: tuple[str, dict[str, str]],
                    body: dict[str, Any]) -> None:
        url, headers = target
        try:
            response = await self._client.post(url, json=body, headers=headers, timeout=_TIMEOUT_SECONDS)
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - a delivery failure never fails the run
            DELIVERY_FAILURES.labels(channel=channel).inc()
            log.warning("result delivery failed: %s (channel %s)", redact(str(exc)), channel,
                        extra={"invocation_id": invocation_id})

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._client.aclose()

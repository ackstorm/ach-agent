# SPDX-License-Identifier: MIT
"""KeySource: exchange, rotation at 80% on the monotonic clock, clamped backoff, single-flight
re-exchange, and KeyAuth's one retry on ACH's own rejection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from prometheus_client import REGISTRY

from ach_runtime.harness.keys import Exchange, ExchangeRefused, KeySource

EX = Exchange("https://ach.example", "ai.run@example.com", "prod", "")
REJECTED = {"error": {"code": "expired_or_revoked", "message": "key expired or revoked"}, "request_id": "req_1"}


class FakeTime:
    """Monotonic and wall clocks that advance only when the rotation loop sleeps. The
    `stop_after`-th sleep is recorded and then ends the loop."""

    def __init__(self, stop_after: int, probe: Callable[[], object] | None = None) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.probes: list[object] = []
        self._stop_after, self._probe = stop_after, probe

    def clock(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.now + 4000.0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self._probe is not None:
            self.probes.append(self._probe())
        if len(self.sleeps) >= self._stop_after:
            raise asyncio.CancelledError
        self.now += seconds


def ok(token: str, expires_in: float = 100) -> httpx.Response:
    return httpx.Response(200, json={"token": token, "expires_in": expires_in, "key_id": "ekid_1"})


def refused(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "x"}, "request_id": "req_1"})


def _exchanger(replies: list[httpx.Response], calls: list[httpx.Request]) -> httpx.AsyncClient:
    """Answers each exchange with the next reply; the last one repeats."""
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(0)
        return replies.pop(0) if len(replies) > 1 else replies[0]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _token(tmp_path: Path, value: str = "sa-1") -> str:
    path = tmp_path / "token"
    path.write_text(value + "\n")
    return str(path)


def _keys(tmp_path: Path, replies, calls, time: FakeTime | None = None) -> KeySource:
    kwargs = {} if time is None else {"clock": time.clock, "wall": time.wall, "sleep": time.sleep}
    return KeySource(exchange=replace(EX, token_path=_token(tmp_path)), client=_exchanger(replies, calls), **kwargs)


def _failures() -> float:
    return REGISTRY.get_sample_value("ach_agent_key_refresh_failures_total") or 0.0


async def test_each_exchange_reads_the_token_file_and_posts_identity_and_environment(tmp_path) -> None:
    calls: list[httpx.Request] = []
    time = FakeTime(stop_after=99)
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2")], calls, time)
    assert await keys.refresh_rejected(0)
    time.now += 11
    (tmp_path / "token").write_text("sa-2")
    assert await keys.refresh_rejected(1)
    assert [c.headers["authorization"] for c in calls] == ["Bearer sa-1", "Bearer sa-2"]
    assert str(calls[0].url) == "https://ach.example/platform/keys/exchange"
    assert json.loads(calls[0].content) == {"identity": "ai.run@example.com", "environment": "prod"}
    assert (keys.current(), keys.generation) == ("jwt-2", 2)
    await keys.close()


@pytest.mark.parametrize("reply,code", [
    (refused(403, "not_granted"), "not_granted"),
    (refused(401, "invalid_token"), "invalid_token"),
    (httpx.Response(307, headers={"location": "https://elsewhere.example/x"}), "http_307"),
    (httpx.Response(200, json={"token": "", "expires_in": 900}), "invalid_response"),
])
async def test_start_fails_with_achs_code_and_follows_no_redirect(tmp_path, reply, code) -> None:
    calls: list[httpx.Request] = []
    keys = _keys(tmp_path, [reply], calls)
    with pytest.raises(ExchangeRefused, match=code) as raised:
        await keys.start()
    assert raised.value.code == code and len(calls) == 1
    await keys.close()


async def test_rotation_runs_at_80_percent_of_expires_in_on_the_monotonic_clock(tmp_path) -> None:
    time, calls = FakeTime(stop_after=3), []
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2"), ok("jwt-3")], calls, time)
    await keys.start()
    await asyncio.gather(keys._task, return_exceptions=True)
    assert time.sleeps == pytest.approx([80.0, 80.0, 80.0])
    assert (len(calls), keys.generation, keys.current()) == (3, 3, "jwt-3")
    assert REGISTRY.get_sample_value("ach_agent_key_expiry_timestamp_seconds") == pytest.approx(time.wall() + 100)
    assert REGISTRY.get_sample_value("ach_agent_key_lifetime_seconds") == 100
    await keys.close()


async def test_refresh_failures_back_off_but_always_retry_before_expiry(tmp_path) -> None:
    time, calls = FakeTime(stop_after=10), []
    keys = _keys(tmp_path, [ok("jwt-1"), refused(503, "not_ready")], calls, time)
    before = _failures()
    await keys.start()
    await asyncio.gather(keys._task, return_exceptions=True)
    # Refresh due at 1080, expiry at 1100: retries at 1081, 1083, 1087, 1093.5, 1096.75,
    # 1098.375, 1099.375 (all before expiry), then 1100.375, then the 60 s cap.
    assert time.sleeps == pytest.approx([80, 1, 2, 4, 6.5, 3.25, 1.625, 1, 1, 60])
    assert _failures() - before == 9 and len(calls) == 10
    assert keys.current() == "jwt-1" and not keys.ready()
    await keys.close()


async def test_not_ready_after_expiry_until_an_exchange_succeeds(tmp_path) -> None:
    calls: list[httpx.Request] = []
    holder: dict[str, KeySource] = {}
    time = FakeTime(stop_after=5, probe=lambda: holder["keys"].ready())
    replies = [ok("jwt-1", 10), refused(503, "not_ready"), refused(503, "not_ready"), refused(503, "not_ready"), ok("jwt-2", 10)]
    holder["keys"] = keys = _keys(tmp_path, replies, calls, time)
    await keys.start()
    await asyncio.gather(keys._task, return_exceptions=True)
    assert time.sleeps == pytest.approx([8, 1, 1, 4, 8])
    assert time.probes == [True, True, True, False, True]
    assert keys.current() == "jwt-2" and keys.ready()
    await keys.close()


async def test_concurrent_rejections_share_one_reexchange(tmp_path) -> None:
    calls: list[httpx.Request] = []
    time = FakeTime(stop_after=99)
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2")], calls, time)
    assert await keys.refresh_rejected(0)
    time.now += 11
    results = await asyncio.gather(*(keys.refresh_rejected(1) for _ in range(5)))
    assert results == [True] * 5 and len(calls) == 2 and keys.generation == 2
    await keys.close()


async def test_concurrent_rejections_share_one_failed_reexchange(tmp_path) -> None:
    calls: list[httpx.Request] = []
    time = FakeTime(stop_after=99)
    keys = _keys(tmp_path, [ok("jwt-1"), refused(503, "not_ready")], calls, time)
    assert await keys.refresh_rejected(0)
    time.now += 11
    before = _failures()
    results = await asyncio.gather(*(keys.refresh_rejected(1) for _ in range(5)))
    assert results == [False] * 5 and len(calls) == 2 and _failures() - before == 1
    await keys.close()


async def test_a_static_key_never_reexchanges() -> None:
    keys = KeySource("ek_static")
    await keys.start()
    assert (keys.current(), keys.ready(), keys.generation) == ("ek_static", True, 0)
    assert await keys.refresh_rejected(0) is False
    await keys.close()


def _upstream(seen: list[str], reply: Callable[[str], httpx.Response]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return reply(request.headers["x-ach-key"])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_keyauth_retries_once_with_the_new_generation_when_ach_rejects(tmp_path) -> None:
    calls, seen = [], []
    time = FakeTime(stop_after=99)
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2")], calls, time)
    await keys.refresh_rejected(0)
    time.now += 11
    client = _upstream(seen, lambda key: httpx.Response(401, json=REJECTED) if key == "jwt-1" else httpx.Response(200))
    response = await client.get("https://ach.example/v1/models", auth=keys.auth)
    assert response.status_code == 200 and seen == ["jwt-1", "jwt-2"] and len(calls) == 2
    await keys.close()


@pytest.mark.parametrize("reply", [
    httpx.Response(401, json={"error": {"message": "invalid api key", "type": "auth_error", "code": "401"}}),
    httpx.Response(401, text="unauthorized"),
    httpx.Response(403, json={"error": {"code": "expired_or_revoked"}}),
])
async def test_keyauth_passes_other_rejections_through(tmp_path, reply) -> None:
    calls, seen = [], []
    keys = _keys(tmp_path, [ok("jwt-1")], calls)
    await keys.refresh_rejected(0)
    response = await _upstream(seen, lambda key: reply).get("https://ach.example/v1/models", auth=keys.auth)
    assert response.status_code == reply.status_code and seen == ["jwt-1"] and len(calls) == 1
    await keys.close()


async def test_keyauth_with_a_static_key_does_not_retry() -> None:
    seen: list[str] = []
    keys = KeySource("ek_static")
    response = await _upstream(seen, lambda key: httpx.Response(401, json=REJECTED)).get("https://ach.example/x", auth=keys.auth)
    assert response.status_code == 401 and seen == ["ek_static"]


async def test_a_cancelled_initiator_does_not_abort_the_shared_reexchange(tmp_path) -> None:
    gate, started, calls = asyncio.Event(), asyncio.Event(), []
    replies = [ok("jwt-1"), ok("jwt-2")]

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 2:
            started.set()
            await gate.wait()
        return replies.pop(0)

    time = FakeTime(stop_after=99)
    keys = KeySource(exchange=replace(EX, token_path=_token(tmp_path)), clock=time.clock, wall=time.wall,
                     client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await keys.start()
    keys._task.cancel()
    time.now += 11
    initiator = asyncio.create_task(keys.refresh_rejected(1))
    await started.wait()
    waiters = [asyncio.create_task(keys.refresh_rejected(1)) for _ in range(3)]
    await asyncio.sleep(0)
    initiator.cancel()
    gate.set()
    assert await asyncio.gather(*waiters) == [True] * 3
    assert (len(calls), keys.generation, keys.current()) == (2, 2, "jwt-2")


async def test_a_rejection_right_after_an_exchange_is_not_reexchanged(tmp_path) -> None:
    calls: list[httpx.Request] = []
    time = FakeTime(stop_after=99)
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2")], calls, time)
    assert await keys.refresh_rejected(0)
    time.now += 9
    assert not await keys.refresh_rejected(1) and len(calls) == 1
    time.now += 2
    assert await keys.refresh_rejected(1) and len(calls) == 2
    await keys.close()


async def test_retry_after_on_429_sets_the_retry_delay_within_the_clamps(tmp_path) -> None:
    def limited(seconds: str) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": seconds}, json={"error": {"code": "rate_limited"}})

    time, calls = FakeTime(stop_after=4), []
    keys = _keys(tmp_path, [ok("jwt-1", 100), limited("5"), limited("500"), limited("30")], calls, time)
    await keys.start()
    await asyncio.gather(keys._task, return_exceptions=True)
    # Due at 1080 (expiry 1100): Retry-After 5 is honoured; 500 is clamped by half the time
    # left to expiry (1085 -> 7.5); 30 by the same rule (1092.5 -> 3.75).
    assert time.sleeps == pytest.approx([80, 5, 7.5, 3.75])
    await keys.close()

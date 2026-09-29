# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator

import aiohttp
import pytest
from aiohttp import web

from ach_agent.engine.trace import tokenize_url
from ach_agent.sandbox.gateway import FacadeGateway
from ach_agent.sandbox.tokens import facade_token

K = b"k" * 32
CLAIM = "ach-bot-" + "0" * 32


class Upstream:
    def __init__(self) -> None:
        self.paths: list[str] = []
        self.first_chunk = asyncio.Event()
        self.port = 0
        self._runner: web.AppRunner | None = None

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        self.paths.append(request.path)
        if request.path.endswith("/echo"):
            await request.content.readany()
            self.first_chunk.set()
            return web.Response(text="got-first-chunk")
        if request.path.endswith("/gz"):
            return web.Response(body=gzip.compress(b"zipped"), headers={"Content-Encoding": "gzip"})
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(b"one-")
        await resp.write(b"two")
        await resp.write_eof()
        return resp

    async def start(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.port = self._runner.addresses[0][1]

    async def stop(self) -> None:
        assert self._runner is not None
        await self._runner.cleanup()


@pytest.fixture
async def upstream() -> AsyncIterator[Upstream]:
    up = Upstream()
    await up.start()
    yield up
    await up.stop()


async def _gateway(live: bool = True) -> tuple[FacadeGateway, int, list[tuple[str, int]]]:
    archives: list[tuple[str, int]] = []

    async def is_live(claim: str) -> bool:
        return live and claim == CLAIM

    async def on_archive(claim: str, request: web.Request) -> web.StreamResponse:
        archives.append((claim, len(await request.read())))
        return web.Response(status=204)

    gw = FacadeGateway(key=K, is_live=is_live, on_archive=on_archive)
    return gw, await gw.start("127.0.0.1", 0), archives


async def _get(url: str) -> tuple[int, bytes, dict[str, str]]:
    async with aiohttp.ClientSession() as s, s.get(url) as r:
        return r.status, await r.read(), dict(r.headers)


async def test_rewrite_and_relay(upstream: Upstream) -> None:
    gw, port, _ = await _gateway()
    try:
        gw.allow_port(upstream.port)
        token = facade_token(K, CLAIM)
        base = f"http://gw:{port}"
        url = gw.rewrite(f"http://127.0.0.1:{upstream.port}/v1", token, public_base=base)
        local = url.replace("http://gw", "http://127.0.0.1")
        status, body, _ = await _get(f"{local}/models")
        assert (status, body) == (200, b"one-two")
        assert upstream.paths[-1] == "/v1/models"
        traced = tokenize_url(local, "trace")
        await _get(f"{traced}/x")
        assert upstream.paths[-1] == "/t/trace/v1/x"
    finally:
        await gw.stop()


async def test_rejections(upstream: Upstream) -> None:
    gw, port, archives = await _gateway()
    try:
        gw.allow_port(upstream.port)
        good = facade_token(K, CLAIM)
        root = f"http://127.0.0.1:{port}"
        assert (await _get(f"{root}/s/{facade_token(b'x' * 32, CLAIM)}/{upstream.port}/a"))[
            0
        ] == 404
        assert (await _get(f"{root}/s/{facade_token(K, 'other')}/{upstream.port}/a"))[0] == 404
        assert (await _get(f"{root}/s/{good}/{upstream.port + 1}/a"))[0] == 404  # unregistered
        assert upstream.paths == []
        async with aiohttp.ClientSession() as s:
            async with s.put(f"{root}/s/nope/session/archive", data=b"x") as r:
                assert r.status == 404
            assert archives == []
            async with s.put(f"{root}/s/{good}/session/archive", data=b"12345") as r:
                assert r.status == 204
        assert archives == [(CLAIM, 5)]
        with pytest.raises(ValueError):
            gw.rewrite("http://example.com/v1", good, public_base=root)
    finally:
        await gw.stop()


async def test_dead_claim_is_404(upstream: Upstream) -> None:
    gw, port, _ = await _gateway(live=False)
    try:
        gw.allow_port(upstream.port)
        url = f"http://127.0.0.1:{port}/s/{facade_token(K, CLAIM)}/{upstream.port}/a"
        assert (await _get(url))[0] == 404
    finally:
        await gw.stop()


async def test_gzip_upstream_arrives_decoded(upstream: Upstream) -> None:
    gw, port, _ = await _gateway()
    try:
        gw.allow_port(upstream.port)
        url = f"http://127.0.0.1:{port}/s/{facade_token(K, CLAIM)}/{upstream.port}/gz"
        status, body, headers = await _get(url)
        assert (status, body) == (200, b"zipped")
        assert "content-encoding" not in {k.lower() for k in headers}
    finally:
        await gw.stop()


async def test_relay_streams_request_body_instead_of_buffering(upstream: Upstream) -> None:
    gw, port, _ = await _gateway()
    try:
        gw.allow_port(upstream.port)
        url = f"http://127.0.0.1:{port}/s/{facade_token(K, CLAIM)}/{upstream.port}/echo"

        async def body() -> AsyncIterator[bytes]:
            yield b"a" * 1024
            # A buffering relay never forwards chunk one until this generator ends.
            await asyncio.wait_for(upstream.first_chunk.wait(), timeout=5)
            yield b"b" * 1024

        async with aiohttp.ClientSession() as s, s.post(url, data=body()) as r:
            assert (r.status, await r.text()) == (200, "got-first-chunk")
    finally:
        await gw.stop()

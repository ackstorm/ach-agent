# SPDX-License-Identifier: Apache-2.0
"""HTTP/SSE client for the opencode serve REST API.

Provides:
  - find_free_port: allocate an ephemeral port avoiding in-memory reserved set
  - OpenCodeClient: aiohttp-based client for opencode HTTP endpoints and SSE stream

Hardening:
  - read_bufsize=2**20 (H-01: 1MB — prevents "Chunk too big" on large SSE events)
  - bytearray buffer in iter_sse_events (H-06: safe UTF-8 parsing across chunk boundaries)
  - ClientTimeout(total=0) for SSE subscribe (no HTTP timeout on streaming connection)

Constraint: No router or Hermes imports (D-08, RTR-06).
"""

from __future__ import annotations

import json
import socket
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Any

import aiohttp
import structlog

from ach_agent.engine.events import OpenCodeEvent, parse_opencode_event

log = structlog.get_logger(__name__)


def _trace_sse(data_str: str) -> None:
    """Raw SSE event trace at DEBUG level (enable with ``ACH_LOG_LEVEL=debug``).

    Logs EVERY opencode event — including tool calls / step events that parse to None and
    the final EOF-flushed event — so the complete engine↔harness wire is visible with
    nothing truncated or dropped. The raw ``data:`` JSON is logged as-is (the redact
    processors still scrub any ek_/token); ``length`` flags the size of giant events
    (e.g. gemini thought-signatures) without hiding their tail. Gated solely by the log
    level: ``log.debug`` is a no-op below DEBUG, so there is no separate opt-in flag.
    """
    log.debug("sse event", length=len(data_str), raw=data_str)


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

_reserved_ports: set[int] = set()


def find_free_port() -> int:
    """Find a free TCP port, avoiding ports already in the in-memory reserved set.

    Binds a socket to port 0 (OS assigns a free port), then releases the
    socket. Adds the port to _reserved_ports immediately to prevent a
    concurrent caller from picking the same port before opencode binds it.

    Note: The full 20-retry collision loop (H-04) is hardened by 00-02.
    This implementation provides the basic helper + reserved set.
    """
    for attempt in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = int(s.getsockname()[1])
        if port not in _reserved_ports:
            _reserved_ports.add(port)
            return port
        log.debug("port already reserved, retrying", port=port, attempt=attempt + 1)
    raise RuntimeError(
        f"Could not find a free port after 20 attempts (reserved: {_reserved_ports})"
    )


def release_port(port: int) -> None:
    """Release a previously reserved port back to the pool."""
    _reserved_ports.discard(port)


# ---------------------------------------------------------------------------
# OpenCodeClient
# ---------------------------------------------------------------------------


class OpenCodeClient:
    """HTTP/SSE client for opencode serve REST API.

    Usage (async context manager)::

        async with OpenCodeClient("http://127.0.0.1:PORT") as client:
            session = await client.create_session()
            await client.send_message(session["id"], "hello")
            resp = await client.subscribe_events()
            async for event in OpenCodeClient.iter_sse_events(resp):
                ...

    Or explicit open/close::

        client = OpenCodeClient("http://127.0.0.1:PORT")
        await client.open()
        ...
        await client.close()
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def open(self) -> None:
        """Create the underlying aiohttp ClientSession."""
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=100)
            # REST endpoints: 300s total, 10s connect, 300s sock_read
            timeout = aiohttp.ClientTimeout(total=300, connect=10, sock_read=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
                read_bufsize=2**20,  # H-01: 1MB — do not reduce
            )

    async def close(self) -> None:
        """Close the underlying aiohttp ClientSession."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> OpenCodeClient:
        await self.open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # REST API methods
    # ------------------------------------------------------------------

    async def check_health(self) -> bool:
        """GET /app — return True iff HTTP 200 (body is HTML, ignore it).

        Used as the readiness probe after launch.
        """
        if self._session is None:
            return False
        try:
            async with self._session.get(
                f"{self._base_url}/app",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    async def create_session(self) -> dict[str, Any]:
        """POST /session — create a new opencode session.

        Returns the session dict: {"id": "ses_...", "slug": ..., ...}
        """
        assert self._session is not None, "Call open() first"
        async with self._session.post(f"{self._base_url}/session") as resp:
            resp.raise_for_status()
            return await resp.json()  # type: ignore[no-any-return]

    async def send_message(self, session_id: str, text: str) -> None:
        """POST /session/{id}/message — submit a prompt.

        Body: {"parts": [{"type": "text", "text": text}]}
        Response arrives via the SSE stream (GET /event), not the HTTP response.
        """
        assert self._session is not None, "Call open() first"
        body = {"parts": [{"type": "text", "text": text}]}
        async with self._session.post(
            f"{self._base_url}/session/{session_id}/message",
            json=body,
        ) as resp:
            resp.raise_for_status()
            await resp.read()  # drain the response body

    async def abort_session(self, session_id: str) -> None:
        """POST /session/{id}/abort — abort the active turn."""
        assert self._session is not None, "Call open() first"
        async with self._session.post(f"{self._base_url}/session/{session_id}/abort") as resp:
            resp.raise_for_status()
            await resp.read()

    async def delete_session(self, session_id: str) -> None:
        """DELETE /session/{id} — remove the session from opencode's store.

        Used by session type='none' post-turn cleanup and overflow='rotate', so
        stateless turns leave no residue in the persistent home (opencode.db).
        """
        assert self._session is not None, "Call open() first"
        async with self._session.delete(f"{self._base_url}/session/{session_id}") as resp:
            resp.raise_for_status()
            await resp.read()

    async def compact_session(self, session_id: str) -> None:
        """POST /session/{id}/compact — summarize history in place (bounds tokens)."""
        assert self._session is not None, "Call open() first"
        async with self._session.post(
            f"{self._base_url}/session/{session_id}/compact", json={}
        ) as resp:
            resp.raise_for_status()
            await resp.read()

    async def subscribe_events(self) -> aiohttp.ClientResponse:
        """GET /event — open the SSE event stream.

        Returns the raw aiohttp response. Use iter_sse_events() to parse.
        Uses ClientTimeout(total=0) — no HTTP timeout for the SSE connection.
        """
        assert self._session is not None, "Call open() first"
        return await self._session.get(
            f"{self._base_url}/event",
            headers={"Accept": "text/event-stream"},
            timeout=aiohttp.ClientTimeout(total=0),  # no timeout for SSE
        )

    # ------------------------------------------------------------------
    # SSE parser
    # ------------------------------------------------------------------

    @staticmethod
    async def iter_sse_events(
        response: aiohttp.ClientResponse,
    ) -> AsyncGenerator[OpenCodeEvent, None]:
        """Parse SSE events from an aiohttp streaming response.

        H-06: Uses bytearray buffer to accumulate raw bytes and split on b"\\n"
        before decoding — prevents UnicodeDecodeError when multi-byte UTF-8
        characters are split across aiohttp chunk boundaries.

        Only `data: <JSON>` lines are parsed; `event:`, `retry:`, and comment
        lines (starting with ':') are ignored. Events are separated by empty lines.
        """
        data_lines: list[str] = []
        buffer = bytearray()

        async for chunk in response.content:
            buffer.extend(chunk)

            # Process all complete lines in the buffer
            while b"\n" in buffer:
                line_bytes, buffer = buffer.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")

                if not line:
                    # Empty line = SSE event boundary
                    if data_lines:
                        data_str = "\n".join(data_lines)
                        _trace_sse(data_str)
                        try:
                            data = json.loads(data_str)
                            event = parse_opencode_event(data)
                            if event is not None:
                                yield event
                        except json.JSONDecodeError:
                            log.debug("SSE bad JSON", preview=data_str[:200])
                        data_lines.clear()
                    continue

                if line.startswith((":", "retry:", "event:")):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())

        # Flush any remaining buffered data lines after stream ends. This is a real event
        # (e.g. the final session.idle when the stream closes without a trailing blank line);
        # it must be traced too — otherwise the LAST SSE message is silently missing.
        if data_lines:
            data_str = "\n".join(data_lines)
            _trace_sse(data_str)
            try:
                data = json.loads(data_str)
                event = parse_opencode_event(data)
                if event is not None:
                    yield event
            except json.JSONDecodeError:
                log.debug("SSE bad JSON at stream end", preview=data_str[:200])

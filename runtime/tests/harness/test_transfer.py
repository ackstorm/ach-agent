# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio

import httpx
import pytest

from ach_runtime.harness.transfer import get_archive, put_archive
from ach_runtime.harness.workspaces import InstanceMismatch, WorkspaceEndpoint, WorkspaceRef

_AGENT = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
_REF = "a" * 64


def test_get_archive_streams_confirmed_pod_response() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/sessions/x/archive"
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "pod"}, content=b"abc")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", _AGENT, _REF), "ws", "pod", "http://mini")
        chunks = [chunk async for chunk in get_archive(client, endpoint, {"Authorization": "Bearer x"}, "/sessions/x/archive")]
        assert b"".join(chunks) == b"abc"
        await client.aclose()
    asyncio.run(scenario())


def test_put_archive_with_another_instance_id_reports_the_loss_once() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "foreign"}, json={"ok": True})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", _AGENT, _REF), "ws", "pod", "http://mini")
        lost: list[WorkspaceEndpoint] = []
        async def chunks():
            yield b"archive"
        with pytest.raises(InstanceMismatch):
            await put_archive(client, endpoint, {}, "/workspace/archive", chunks(), on_mismatch=lost.append)
        assert lost == [endpoint]
        await client.aclose()
    asyncio.run(scenario())


def test_get_archive_with_another_instance_id_reports_the_loss_once() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"X-ACH-Instance-Id": "foreign"}, content=b"abc")
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", _AGENT, _REF), "ws", "pod", "http://mini")
        lost: list[WorkspaceEndpoint] = []
        with pytest.raises(InstanceMismatch):
            async for _ in get_archive(client, endpoint, {}, "/workspace/archive", on_mismatch=lost.append):
                pytest.fail("a foreign instance must not yield archive bytes")
        assert lost == [endpoint]
        await client.aclose()
    asyncio.run(scenario())


def test_get_archive_interrupted_body_is_checked_at_response_start_not_a_mismatch() -> None:
    """Q2: the header is checked when the response starts; a body cut short afterwards is a
    transport error (retryable), never a loss report."""
    async def scenario() -> None:
        class Cut(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"partial"
                raise httpx.ReadError("connection cut")

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request, headers={"X-ACH-Instance-Id": "pod"}, stream=Cut())
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        endpoint = WorkspaceEndpoint(WorkspaceRef("ach", _AGENT, _REF), "ws", "pod", "http://mini")
        lost: list[WorkspaceEndpoint] = []
        with pytest.raises(httpx.ReadError):
            async for _ in get_archive(client, endpoint, {}, "/workspace/archive", on_mismatch=lost.append):
                pass
        assert lost == []
        await client.aclose()
    asyncio.run(scenario())


def test_get_archive_http_error_preserves_private_json_cause() -> None:
    async def scenario() -> None:
        closed = False

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"type":"Failed","message":"[Errno 13] Permission denied: \'/.ach-archive-test.tar.gz\'"}'

            async def aclose(self):
                nonlocal closed
                closed = True

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                502, request=request, headers={"X-ACH-Instance-Id": "pod"}, stream=Body()
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", _AGENT, _REF), "ws", "pod", "http://mini"
        )
        yielded = []
        try:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                async for chunk in get_archive(client, endpoint, {}, "/workspace/archive"):
                    yielded.append(chunk)
            error = raised.value
            assert "HTTP 502" in str(error)
            assert "Failed" in str(error)
            assert "Permission denied" in str(error)
            assert error.response.status_code == 502
            assert error.request.url.path == "/workspace/archive"
            from ach_runtime.harness.lifecycle import _retryable_archive_error
            assert _retryable_archive_error(error) is True
            assert yielded == []
            assert closed is True
            assert error.response.is_closed
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_get_archive_invalid_error_body_preserves_status() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                422, request=request, headers={"X-ACH-Instance-Id": "pod"}, content=b"not json"
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", _AGENT, _REF), "ws", "pod", "http://mini"
        )
        try:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                async for _ in get_archive(client, endpoint, {}, "/workspace/archive"):
                    pytest.fail("an error response must not yield archive bytes")
            error = raised.value
            assert error.response.status_code == 422
            assert "422 Unprocessable Entity" in str(error)
            assert "archive export HTTP" not in str(error)
            from ach_runtime.harness.lifecycle import _retryable_archive_error
            assert _retryable_archive_error(error) is False
            assert error.response.is_closed
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_get_archive_unavailable_error_body_preserves_status() -> None:
    async def scenario() -> None:
        closed = False
        responses = []

        class UnavailableBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                if False:
                    yield b""
                raise httpx.ReadError("private body unavailable")

            async def aclose(self):
                nonlocal closed
                closed = True

        async def handler(request: httpx.Request) -> httpx.Response:
            response = httpx.Response(
                502,
                request=request,
                headers={"X-ACH-Instance-Id": "pod"},
                stream=UnavailableBody(),
            )
            responses.append(response)
            return response

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        endpoint = WorkspaceEndpoint(
            WorkspaceRef("ach", _AGENT, _REF), "ws", "pod", "http://mini"
        )
        yielded = []
        try:
            with pytest.raises(httpx.HTTPStatusError) as raised:
                async for chunk in get_archive(client, endpoint, {}, "/workspace/archive"):
                    yielded.append(chunk)
            error = raised.value
            assert error.response.status_code == 502
            assert error.response is responses[0]
            assert error.request.url.path == "/workspace/archive"
            assert error.response.request is error.request
            assert "502 Bad Gateway" in str(error)
            assert "archive export HTTP" not in str(error)
            assert yielded == []
            assert closed is True
            assert error.response.is_closed
        finally:
            await client.aclose()

    asyncio.run(scenario())

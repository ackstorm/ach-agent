# SPDX-License-Identifier: MIT
"""Authenticated in-process operations for Workspace-owned artifacts."""

from __future__ import annotations

import base64
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from copy import deepcopy
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import BlobResourceContents, EmbeddedResource
from starlette.applications import Starlette
from starlette.routing import Mount

from ach_runtime.config.schema import ArtifactsBlock
from ach_runtime.harness.tokens import (
    WorkspaceAuthenticationFailed,
    authenticate_facade,
)
from ach_runtime.storage.api import (
    ArtifactDisabled,
    ArtifactProducer,
    ArtifactReadReceipt,
    ArtifactReceipt,
    Storage,
    StorageUnavailable,
)
from ach_runtime.storage.refs import StateRef
from ach_runtime.facades.memory import _AuthGuard


class ArtifactOperations:
    """Storage facade bound to the trusted Workspace, producer, and artifact policy."""

    def __init__(
        self,
        *,
        storage: Storage | None,
        owner: StateRef,
        producer: ArtifactProducer,
        policy: ArtifactsBlock,
        workspace_name: str,
        key: bytes,
        owned_workspace: Callable[[str], Awaitable[bool]],
    ) -> None:
        self._storage = storage
        self._owner = owner
        self._producer = producer
        self._policy = deepcopy(policy)
        self._workspace_name = workspace_name
        self._key = bytes(key)
        self._owned_workspace = owned_workspace

    async def _authenticate(self, token: str) -> None:
        claim = await authenticate_facade(
            self._key, token, owned_workspace=self._owned_workspace
        )
        if claim != self._workspace_name:
            raise WorkspaceAuthenticationFailed(claim)

    def _require_enabled_storage(self) -> Storage:
        if not self._policy.enabled:
            raise ArtifactDisabled("artifact operations are disabled")
        if self._storage is None:
            raise StorageUnavailable("no artifact storage backend is configured")
        return self._storage

    async def create_artifact(
        self,
        token: str,
        idempotency_key: str,
        stream: AsyncIterator[bytes],
        *,
        mime: str,
        name: str,
    ) -> ArtifactReceipt:
        await self._authenticate(token)
        storage = self._require_enabled_storage()
        try:
            return await storage.create_artifact(
                self._owner,
                self._producer,
                idempotency_key,
                stream,
                mime=mime,
                name=name,
                policy=deepcopy(self._policy),
            )
        except Exception:
            from ach_runtime.observability import alarm

            alarm("artifact_publication_failed", invocation_id=self._producer.invocation_id,
                  workspace_ref=self._owner.workspace_ref,
                  session_ref=self._producer.session_ref)
            raise

    async def read_artifact(self, token: str, artifact_id: str) -> ArtifactReadReceipt:
        await self._authenticate(token)
        storage = self._require_enabled_storage()
        return await storage.read_artifact(self._owner, artifact_id)


class ArtifactFacade:
    """Expose the accepted artifact operations through the real MCP SDK."""

    def __init__(
        self,
        *,
        key: bytes,
        owned_workspace: Callable[[str], Awaitable[bool]],
        operations: Callable[[str, str], Awaitable[ArtifactOperations]],
        exclude_tools: frozenset[str],
    ) -> None:
        self._key = bytes(key)
        self._owned_workspace = owned_workspace
        self._operations = operations
        # Mounted in the Harness's cluster-facing listener, not a loopback server.
        self._mcp: FastMCP[Any] = FastMCP("ach-artifacts", host="0.0.0.0", streamable_http_path="/")
        if "create_artifact" not in exclude_tools:
            self._register_create()
        if "read_artifact" not in exclude_tools:
            self._register_read()

    def _context(self, ctx: Context) -> tuple[str, str]:
        params = ctx.request_context.request.path_params
        return str(params["token"]), str(params["invocation_id"])

    def _register_create(self) -> None:
        @self._mcp.tool(
            name="create_artifact",
            description="Publish a UTF-8 text artifact for this invocation.",
        )
        async def create_artifact(
            idempotency_key: str,
            content: str,
            mime: str,
            name: str,
            ctx: Context,
        ) -> ArtifactReceipt:
            token, invocation_id = self._context(ctx)
            operations = await self._operations(token, invocation_id)
            payload = content.encode("utf-8")

            async def chunks() -> AsyncIterator[bytes]:
                for offset in range(0, len(payload), 64 * 1024):
                    yield payload[offset : offset + 64 * 1024]

            return await operations.create_artifact(
                token, idempotency_key, chunks(), mime=mime, name=name
            )

    def _register_read(self) -> None:
        @self._mcp.tool(
            name="read_artifact",
            description="Read a verified artifact as a standard MCP binary resource.",
            structured_output=False,
        )
        async def read_artifact(artifact_id: str, ctx: Context) -> list[EmbeddedResource]:
            token, invocation_id = self._context(ctx)
            operations = await self._operations(token, invocation_id)
            receipt = await operations.read_artifact(token, artifact_id)
            content = b"".join([part async for part in receipt.open()])
            return [
                EmbeddedResource(
                    type="resource",
                    resource=BlobResourceContents(
                        uri=f"artifact:{artifact_id}",
                        mimeType=receipt.metadata.mime,
                        blob=base64.b64encode(content).decode("ascii"),
                    ),
                )
            ]

    def app(self) -> Starlette:
        mcp_app = self._mcp.streamable_http_app()
        guarded = _AuthGuard(
            mcp_app, key=self._key, owned_workspace=self._owned_workspace
        )

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

        return Starlette(
            routes=[Mount("/t/{token}/runs/{invocation_id}", app=guarded)],
            lifespan=lifespan,
        )

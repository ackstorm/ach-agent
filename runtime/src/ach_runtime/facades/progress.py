# SPDX-License-Identifier: Apache-2.0
"""`inform_user`: progress messages from a running invocation to its A2A caller.

Mounted per run (`/runs/{invocation_id}/`) like the artifacts facade, so a tool call names
the invocation whose A2A task receives the message. Only A2A runs get this server in their
catalog (`StartRequest.inform_user`).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.routing import Mount

from ach_runtime.facades.memory import _AuthGuard
from ach_runtime.harness.tokens import FACADE_TOKEN_HEADER

log = logging.getLogger(__name__)
OwnedWorkspace = Callable[[str], Awaitable[bool]]
# (facade token, invocation id, message) -> whether a caller received it.
Inform = Callable[[str, str, str], Awaitable[bool]]

TOOL_NAME = "inform_user"


class ProgressFacade:
    def __init__(self, *, key: bytes, owned_workspace: OwnedWorkspace, inform: Inform) -> None:
        self._key = key
        self._owned_workspace = owned_workspace
        # Mounted in the Harness's cluster-facing listener, not a loopback server.
        self._mcp: FastMCP[None] = FastMCP("ach-progress", host="0.0.0.0", streamable_http_path="/")

        @self._mcp.tool(
            name=TOOL_NAME,
            description=(
                "Send a short progress update to the user while you keep working. "
                "The final answer still goes in your reply."
            ),
        )
        async def inform_user(message: str, ctx: Context) -> str:
            request = ctx.request_context.request
            token = request.headers.get(FACADE_TOKEN_HEADER, "")
            invocation_id = str(request.path_params["invocation_id"])
            if not await inform(token, invocation_id, message):
                log.warning("inform_user: no caller is listening (invocation %s)", invocation_id)
            return "Update sent to user"

    def app(self) -> Starlette:
        mcp_app = self._mcp.streamable_http_app()
        guarded = _AuthGuard(mcp_app, key=self._key, owned_workspace=self._owned_workspace)

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

        return Starlette(routes=[Mount("/runs/{invocation_id}", app=guarded)], lifespan=lifespan)

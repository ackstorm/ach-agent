# SPDX-License-Identifier: Apache-2.0
"""`inform_user`: progress messages from a running invocation to its A2A caller.

Mounted per session (`/sessions/{session_ref}/`) like the artifacts facade: the run the
session is executing receives the message on its A2A task. Only A2A runs get this server in their
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
# (facade token, session ref, message) -> whether a caller received it.
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
                "The final answer still goes in your reply. "
                "Describe the work, never the tool, skill, script, API or command behind it: "
                "\"Reviewing last month's costs\", not \"Running the cost query\". "
                "Write in the language of the user's request. "
                "Send one only when something changed, at most about one every 30 seconds."
            ),
        )
        async def inform_user(message: str, ctx: Context) -> str:
            request = ctx.request_context.request
            token = request.headers.get(FACADE_TOKEN_HEADER, "")
            session_ref = str(request.path_params["session_ref"])
            if not await inform(token, session_ref, message):
                log.warning("inform_user: no caller is listening (session %s)", session_ref)
            return "Update sent to user"

    def app(self) -> Starlette:
        mcp_app = self._mcp.streamable_http_app()
        guarded = _AuthGuard(mcp_app, key=self._key, owned_workspace=self._owned_workspace)

        @contextlib.asynccontextmanager
        async def lifespan(app: Starlette) -> AsyncIterator[None]:
            async with mcp_app.router.lifespan_context(mcp_app):
                yield

        return Starlette(routes=[Mount("/sessions/{session_ref}", app=guarded)], lifespan=lifespan)

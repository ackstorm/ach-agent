# SPDX-License-Identifier: MIT
"""`ach-mcp`: call one MCP tool from a lifecycle hook script, as the agent.

Hook scripts get `ACH_MCP_URLS` (JSON `{serverId: url}`): this pod's MCP facade URLs, the
same ones the engine uses, so a call carries the agent identity and its tool exclusions.

    ach-mcp call <server> <tool> ['<json arguments>']

Prints the tool's text content on stdout. Exit 1: the call failed, the tool is unknown to
the server or returned an error. Exit 2: usage error (unknown server, invalid arguments).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from ach_runtime.facades._mcp_session import mcp_session
from ach_runtime.execution.sessions import redact_facade_tokens
from ach_runtime.facades._memory_client import _describe


def resolve_tool(advertised: set[str], tool: str) -> str:
    """What the server calls `tool`. Gateways prefix tool names (LiteLLM:
    `<server>.<tool>`); an exact match wins, then a single `.<tool>`/`/<tool>` suffix match.
    Otherwise the bare name is sent and the server reports the error."""
    if tool in advertised:
        return tool
    hits = [name for name in advertised if name.endswith((f".{tool}", f"/{tool}"))]
    return hits[0] if len(hits) == 1 else tool


async def call_tool(url: str, tool: str, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Return (is_error, text) for one tool call; `tool` may be bare (see `resolve_tool`)."""
    async with mcp_session(url, None) as session:
        name = resolve_tool({t.name for t in (await session.list_tools()).tools}, tool)
        result = await session.call_tool(name, arguments)
    text = "\n".join(
        item.text for item in result.content or [] if isinstance(getattr(item, "text", None), str)
    )
    return bool(getattr(result, "isError", False)), text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ach-mcp", description="Call MCP tools from hook scripts.")
    commands = parser.add_subparsers(dest="command", required=True)
    call = commands.add_parser("call", help="call one tool and print its text result")
    call.add_argument("server")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}", help="JSON object (default {})")
    args = parser.parse_args(argv)

    try:
        urls = json.loads(os.environ.get("ACH_MCP_URLS") or "{}")
    except ValueError:
        urls = {}
    url = urls.get(args.server) if isinstance(urls, dict) else None
    if not isinstance(url, str):
        known = ", ".join(sorted(urls)) if isinstance(urls, dict) and urls else "none"
        print(f"ach-mcp: unknown MCP server {args.server!r} (known: {known})", file=sys.stderr)
        return 2
    try:
        arguments = json.loads(args.arguments)
    except ValueError as exc:
        print(f"ach-mcp: arguments are not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(arguments, dict):
        print("ach-mcp: arguments must be a JSON object", file=sys.stderr)
        return 2

    try:
        is_error, text = asyncio.run(call_tool(url, args.tool, arguments))
    except Exception as exc:  # noqa: BLE001 - reported to the hook script as exit 1
        reason = redact_facade_tokens(_describe(exc))
        print(f"ach-mcp: {args.server} {args.tool} failed: {reason}", file=sys.stderr)
        return 1
    if is_error:
        print(f"ach-mcp: {args.server} {args.tool} returned an error: {text}", file=sys.stderr)
        return 1
    sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

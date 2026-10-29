# SPDX-License-Identifier: MIT
"""Unit tests use placeholder MCP URLs nothing listens on: stub the pre-send `tools/list`
probe to report one tool. E2E runs keep the real probe; a test can re-patch it."""

from __future__ import annotations

import pytest

import ach_runtime.execution.service as service_module


@pytest.fixture(autouse=True)
def _mcp_servers_list_tools(request, monkeypatch):
    if request.node.path.parent.name == "e2e":
        return

    async def one_tool(url, headers):
        return 1

    monkeypatch.setattr(service_module, "_count_mcp_tools", one_tool)

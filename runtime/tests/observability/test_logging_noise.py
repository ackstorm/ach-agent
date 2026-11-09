# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json
import subprocess
import sys

SCRIPT = """
import logging
from anyio import ClosedResourceError
from mcp.server.fastmcp import FastMCP
from ach_runtime.observability.runs import configure_logging

configure_logging()
FastMCP("noise")

def raised(logger, message):
    try:
        raise ClosedResourceError
    except ClosedResourceError:
        logging.getLogger(logger).exception(message)

logging.getLogger("mcp.server.lowlevel.server").info("library info")
logging.getLogger("httpx").info("library info")
logging.getLogger("ach_runtime.harness").info("runtime info")
logging.getLogger("a2a.server").warning("library warning")
raised("mcp.server.streamable_http", "Error in standalone SSE writer")
raised("mcp.server.streamable_http", "Error in standalone SSE response")
"""


def test_every_line_is_json_and_library_chatter_stays_out() -> None:
    """A subprocess, because pytest's root handlers would turn FastMCP's basicConfig into a no-op."""
    done = subprocess.run([sys.executable, "-c", SCRIPT], capture_output=True, text=True, check=True)
    records = [json.loads(line) for line in done.stderr.splitlines()]
    assert [record["message"] for record in records] == [
        "runtime info", "library warning", "Error in standalone SSE response",
    ]
    assert "ClosedResourceError" in records[-1]["traceback"]

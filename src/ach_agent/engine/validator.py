# SPDX-License-Identifier: Apache-2.0
"""Terminal-contract extraction for engine output.

Extracts the single terminal object {"action":...,"text":...,"thoughts":...} from
accumulated SSE text deltas. The terminal contract is a single object — NOT a list.

Egress is the agent's responsibility via external MCP tools; the harness only relays
the terminal `text` (reply mode / on_complete) and otherwise does nothing.

Constraint: No router or Hermes imports (D-08, RTR-06).
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger(__name__)

# Strip markdown ```json ... ``` fences before searching for JSON
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


# ---------------------------------------------------------------------------
# Terminal contract models (single object — CONTRACT_v3)
# ---------------------------------------------------------------------------


class NoneAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["none"]
    text: str = ""
    thoughts: str = ""


class A2AReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["a2a_reply"]
    text: str
    thoughts: str = ""


# ---------------------------------------------------------------------------
# Extraction algorithm (Pattern 6 from 00-RESEARCH.md)
# ---------------------------------------------------------------------------


def _find_matching_brace(text: str, start: int) -> int:
    """Return the closing brace index for the '{' at text[start].

    Returns -1 if no matching brace is found.
    Handles nested objects, arrays, and quoted strings (including escapes).
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
        if not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def extract_terminal(accumulated_text: str) -> dict[str, Any] | None:
    """Find the last {"action": ...} object in the model's text output.

    Algorithm:
      1. Strip markdown code fences (```json ... ```)
      2. Find last occurrence of '{"action"' via rfind (handles preamble + multi-blob)
      3. Match the closing brace via _find_matching_brace
      4. json.loads the matched slice

    Returns None on: no marker, unmatched brace, JSONDecodeError.
    """
    text = accumulated_text
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    pos = text.rfind('{"action"')
    if pos == -1:
        return None
    end = _find_matching_brace(text, pos)
    if end == -1:
        return None
    try:
        result: dict[str, Any] = json.loads(text[pos : end + 1])
        return result
    except json.JSONDecodeError:
        return None

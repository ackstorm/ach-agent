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
from typing import Annotated, Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

log = structlog.get_logger(__name__)

# Strip markdown ```json ... ``` fences before searching for JSON
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)

# Terminal object opener: '{' + optional whitespace + '"action"'. rfind on the tight
# literal missed pretty-printed output and forced a pointless repair turn.
_OPENER_RE = re.compile(r'\{\s*"action"')


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
      2. Find every '{' + optional whitespace + '"action"' opener (handles preamble + multi-blob)
      3. Walk the openers from last to first; return the first one that both brace-matches
         (_find_matching_brace) and json.loads successfully. This keeps "last one wins" while
         skipping trailing prose that merely looks like an opener (e.g. a post-hoc mention of
         '{ "action"' after the real terminal object).

    Returns None if no opener yields a parseable object.
    """
    text = accumulated_text
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    matches = list(_OPENER_RE.finditer(text))
    for match in reversed(matches):
        pos = match.start()
        end = _find_matching_brace(text, pos)
        if end == -1:
            continue
        try:
            result: dict[str, Any] = json.loads(text[pos : end + 1])
        except json.JSONDecodeError:
            continue
        return result
    return None


class NoneAction(BaseModel):
    """Async channel classes (webhook, cron, queue) — CONTRACT §8."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["none"]
    text: str = ""
    thoughts: str = ""


class A2AReply(BaseModel):
    """a2a channel class — the reply text is what the caller receives (CONTRACT §8)."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["a2a_reply"]
    text: str
    thoughts: str = ""


# Single object, NOT a list. ConsentRequest is RESERVED for v1.1 and deliberately absent.
_TERMINAL_ADAPTER: TypeAdapter[NoneAction | A2AReply] = TypeAdapter(
    Annotated[NoneAction | A2AReply, Field(discriminator="action")]
)


def validate_terminal(obj: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate an extracted terminal object against the §8 model union.

    Returns the normalized dict (defaults filled in) or None on any miss: unknown
    action, missing required field, wrong type, extra field. None in → None out.
    """
    if obj is None:
        return None
    try:
        model = _TERMINAL_ADAPTER.validate_python(obj)
    except ValidationError as exc:
        # Never log `obj`/the validation input — it carries model text that can contain
        # secrets. `loc`/`type` alone name which field(s) tripped, not their values.
        log.warning(
            "terminal object failed validation",
            action=obj.get("action"),
            errors=[{"loc": e["loc"], "type": e["type"]} for e in exc.errors()],
        )
        return None
    return model.model_dump()

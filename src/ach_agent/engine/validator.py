# SPDX-License-Identifier: Apache-2.0
"""Action extraction and schema validation for engine output.

Extracts {"actions":[...]} from accumulated SSE text deltas and validates
against the channel's responseActions schema. Bounded repair turn on invalid
output (max 2 attempts total — ENG-05).

A1-confirmed extraction branch (00-01b): {"actions":[...]} arrives as plain
text in message.part.updated SSE events. rfind + brace-match is the correct
extraction strategy.

Constraint: No router or Hermes imports (D-08, RTR-06).
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import TypedDict, cast

import jsonschema
import structlog

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class InvocationResult(TypedDict):
    """Result returned by run_invocation after successful action extraction."""

    actions: list[dict[str, object]]


# ---------------------------------------------------------------------------
# Extraction algorithm (Pattern 6 from 00-RESEARCH.md)
# ---------------------------------------------------------------------------

# Strip markdown ```json ... ``` fences before searching for JSON
_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


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


def extract_actions(accumulated_text: str) -> list[dict[str, object]] | None:
    """Extract {"actions":[...]} JSON from accumulated SSE text deltas.

    A1-confirmed: {"actions":[...]} arrives as plain text in message.part.updated.
    Algorithm:
      1. Strip markdown code fences (```json ... ```)
      2. Find last occurrence of '{"actions"' via rfind (handles preamble + multi-blob)
      3. Match the closing brace via _find_matching_brace
      4. json.loads the matched slice; return data["actions"] or None

    Returns None on: no marker, unmatched brace, JSONDecodeError.
    """
    text = accumulated_text

    # Strip markdown fences — model may wrap the JSON in ```json ... ```
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    # Find last {"actions" marker (rfind: handles preamble + multiple blobs)
    marker = '{"actions"'
    pos = text.rfind(marker)
    if pos == -1:
        return None

    end = _find_matching_brace(text, pos)
    if end == -1:
        return None

    try:
        data = json.loads(text[pos : end + 1])
        return cast(list[dict[str, object]] | None, data.get("actions"))
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Schema validation (ENG-05 — responseActions inputSchema)
# ---------------------------------------------------------------------------


def validate_actions(
    actions: list[dict[str, object]],
    response_actions_schema: list[dict[str, object]],
) -> list[str]:
    """Validate each action's input against the matching responseActions inputSchema.

    Returns a list of error strings (empty list = valid).

    For each action in actions, find the matching schema entry by name and
    validate action["input"] against inputSchema using jsonschema.
    Actions with no matching schema entry are passed through (no schema = no constraint).
    """
    errors: list[str] = []
    # Build a name -> inputSchema map for O(1) lookup
    schema_map = {
        entry["name"]: entry.get("inputSchema")
        for entry in response_actions_schema
        if "name" in entry
    }

    for i, action in enumerate(actions):
        name = action.get("name", f"action[{i}]")
        input_schema = schema_map.get(str(name))
        if input_schema is None:
            continue  # no schema constraint for this action

        action_input = action.get("input", {})
        try:
            jsonschema.validate(instance=action_input, schema=input_schema)
        except jsonschema.ValidationError as exc:
            errors.append(f"action {name!r} input validation failed: {exc.message}")

    return errors


# ---------------------------------------------------------------------------
# Bounded repair turn (ENG-05 — max 2 attempts total)
# ---------------------------------------------------------------------------


async def repair_turn(
    send_fn: Callable[[str], Awaitable[str]],
    accumulated_text: str,
    response_actions_schema: list[dict[str, object]],
    max_attempts: int = 2,
) -> list[dict[str, object]] | None:
    """Issue bounded repair turns when action output fails schema validation.

    Attempt 0 (free): extract and validate the accumulated_text already on hand.
    If valid — return immediately with 0 send_fn calls.
    If invalid — issue one repair turn (send_fn call) with the validation error
    embedded in the prompt, then re-extract and re-validate.

    max_attempts=2 means: 1 initial check + at most 1 repair call.
    After max_attempts exhausted with invalid output — returns None.

    Args:
        send_fn: async callable(prompt) -> new accumulated text (wraps
                 lifecycle.send_message + consume_sse_after_send for that session)
        accumulated_text: SSE accumulated text from the current turn
        response_actions_schema: channel responseActions schema list (CONTRACT §2)
        max_attempts: total attempts including the initial extraction (default=2)
    """
    current_text = accumulated_text

    for attempt in range(max_attempts):
        actions = extract_actions(current_text)
        if actions is None:
            errors = ['Could not extract {"actions":[...]} from model output']
        else:
            errors = validate_actions(actions, response_actions_schema)

        if not errors:
            # Valid — return immediately
            return actions

        # Validation failed
        if attempt + 1 >= max_attempts:
            # Exhausted all attempts
            log.warning(
                "repair_turn exhausted max_attempts with invalid output",
                max_attempts=max_attempts,
                errors=errors,
            )
            return None

        # Issue a repair turn
        error_summary = "; ".join(errors)
        repair_prompt = (
            f"Your previous response had a validation error: {error_summary}. "
            f"Please reply with a valid JSON object matching the responseActions schema: "
            f'{{"actions":[...]}}. '
            f"Each action must have name, kind, and input fields matching the required schema."
        )
        log.info(
            "issuing repair turn",
            attempt=attempt + 1,
            max_attempts=max_attempts,
            errors=errors,
        )
        current_text = await send_fn(repair_prompt)

    # Should not be reached (loop exits via return or max_attempts check above)
    return None

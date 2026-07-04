# SPDX-License-Identifier: Apache-2.0
"""Deterministic {{ }} template substitution for config-authored strings.

Greenfield, zero-dependency. Pure dict/list path traversal — NOT jinja, NOT eval:
no logic, no loops, no attribute access, no method calls. Two filters: default("literal")
and json (serializes ANY value — incl. dicts/lists — as compact JSON; the only way to
emit a whole container, e.g. {{ payload | json }}).

Namespaces (roots of the context dict): `payload`, `header`, `internal`. There is NO
`env` namespace — process env (where the ek_ lives) is structurally unreachable from a
template. That is the ek-hygiene guarantee at the template layer (CONTRACT §3).

Consumer: channel.prompt substitution (main.build_engine_prompt). The pure `resolve_path`
primitive is the deliberate substrate for the future per-event memory-tag resolver (see the
memory bank+tags design note); tag-omit semantics live in that resolver, not here.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# {{ ns.a.b.c }} | {{ ns.a.b | default("x") }} | {{ ns | json }} — ws inside braces ignored.
_TOKEN_RE = re.compile(
    r"\{\{\s*"
    r"([A-Za-z0-9_.\-]+)"  # group 1: dotted path
    r"(?:\s*\|\s*"
    r'(?:default\(\s*"([^"]*)"\s*\)'  # group 2: default literal
    r"|(json)))?"  # group 3: the json filter
    r"\s*\}\}"
)

_MISSING = object()  # sentinel: distinguishes a missing path from a present null/container.


def _resolve_raw(context: dict[str, Any], dotted_path: str) -> Any:
    """Pure traversal (dict keys / list int indices). Returns the value at the path,
    which MAY be a container or null; returns `_MISSING` when a segment is absent."""
    cur: Any = context
    for seg in dotted_path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return _MISSING
            cur = cur[seg]
        elif isinstance(cur, list):
            if not (seg.isdigit() and int(seg) < len(cur)):
                return _MISSING
            cur = cur[int(seg)]
        else:
            return _MISSING
    return cur


def resolve_path(context: dict[str, Any], dotted_path: str) -> str | None:
    """Resolve a dotted path against the context. Return the scalar as str, or None.

    None means a segment was missing OR the value is a container (dict/list) or null —
    not a usable scalar. Pure traversal: dict keys and list integer indices only.
    """
    cur = _resolve_raw(context, dotted_path)
    # bool is a subclass of int — both are acceptable scalars.
    if isinstance(cur, (str, int, float, bool)):
        return str(cur)
    return None


def render_template(template: str, context: dict[str, Any]) -> str:
    """Substitute every {{ path }} / {{ path | default("x") }} / {{ path | json }} token.

    Per token: scalar found → its value; missing/non-scalar with default → default;
    missing/non-scalar without default → empty string (logged). The `json` filter is the
    exception: it serializes whatever is present (dict, list, scalar, or null) as compact
    JSON, and only a genuinely missing path falls back to empty.
    """

    def _sub(m: re.Match[str]) -> str:
        path = m.group(1)
        default = m.group(2)
        if m.group(3):  # | json — serialize any present value (incl. dict/list/null)
            raw = _resolve_raw(context, path)
            if raw is _MISSING:
                log.warning("template: unresolved token -> empty", path=path)
                return ""
            return json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str)
        value = resolve_path(context, path)
        if value is not None:
            return value
        if default is not None:
            return default
        log.warning("template: unresolved token -> empty", path=path)
        return ""

    return _TOKEN_RE.sub(_sub, template)


def build_template_context(
    payload: dict[str, Any],
    *,
    channel_name: str,
    channel_type: str,
    channel_source: str,
    agent_name: str,
    memory_bank: str,
    event_id: str,
    session_key: str,
) -> dict[str, Any]:
    """Assemble the substitution context.

    `header` is reserved (empty) until inbound headers are threaded across the
    channel->router seam (deferred — current seam drops them by design).
    """
    return {
        "payload": payload,
        "header": {},
        "internal": {
            "channel": {
                "name": channel_name,
                "type": channel_type,
                "source": channel_source,
            },
            "agent": {"name": agent_name},
            "memory": {"bank": memory_bank},
            "event": {"id": event_id},
            "session": {"key": session_key},
        },
    }

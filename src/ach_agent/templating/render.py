# SPDX-License-Identifier: Apache-2.0
"""Deterministic {{ }} template substitution for config-authored strings.

Greenfield, zero-dependency. Pure dict/list path traversal — NOT jinja, NOT eval:
no logic, no loops, no attribute access, no method calls. One filter: default("literal").

Namespaces (roots of the context dict): `payload`, `header`, `internal`. There is NO
`env` namespace — process env (where the ek_ lives) is structurally unreachable from a
template. That is the ek-hygiene guarantee at the template layer (CONTRACT §3).

Consumer: channel.prompt substitution (main.build_engine_prompt). The pure `resolve_path`
primitive is the deliberate substrate for the future per-event memory-tag resolver (see the
memory bank+tags design note); tag-omit semantics live in that resolver, not here.
"""

from __future__ import annotations

import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# {{ ns.a.b.c }}  or  {{ ns.a.b | default("x") }} — whitespace inside braces ignored.
_TOKEN_RE = re.compile(
    r"\{\{\s*"
    r"([A-Za-z0-9_.\-]+)"  # group 1: dotted path
    r'(?:\s*\|\s*default\(\s*"([^"]*)"\s*\))?'  # group 2: optional default literal
    r"\s*\}\}"
)


def resolve_path(context: dict[str, Any], dotted_path: str) -> str | None:
    """Resolve a dotted path against the context. Return the scalar as str, or None.

    None means a segment was missing OR the value is a container (dict/list) or null —
    not a usable scalar. Pure traversal: dict keys and list integer indices only.
    """
    cur: Any = context
    for seg in dotted_path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        elif isinstance(cur, list):
            if not (seg.isdigit() and int(seg) < len(cur)):
                return None
            cur = cur[int(seg)]
        else:
            return None
    # bool is a subclass of int — both are acceptable scalars.
    if isinstance(cur, (str, int, float, bool)):
        return str(cur)
    return None


def render_template(template: str, context: dict[str, Any]) -> str:
    """Substitute every {{ path }} / {{ path | default("x") }} token.

    Per token: scalar found → its value; missing/non-scalar with default → default;
    missing/non-scalar without default → empty string (logged).
    """

    def _sub(m: re.Match[str]) -> str:
        path = m.group(1)
        default = m.group(2)
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

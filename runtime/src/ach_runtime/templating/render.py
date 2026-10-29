# SPDX-License-Identifier: MIT
"""Pure dict/list path traversal for `channels[].routing` override templates (contract §2).

Reused from old-ach-agent's `templating/render.py` `resolve_path` primitive — pure
traversal, no jinja/eval, no `env` namespace. Routing is stricter than the old prompt
renderer: a prompt token with no value quietly becomes "" (see old `render_template`);
a routing override must NEVER do that — a missing/non-scalar path rejects the WHOLE
render, even when the token sits behind a nonempty literal prefix (task-3 dispatch).
"""

from __future__ import annotations

import re
from typing import Any

# Matches ANY `{{ ... }}` span, including malformed ones — so an invalid expression is
# validated and rejected, never silently left as literal text (review finding 7).
_ANY_BRACE_RE = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_VALID_PATH_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")

_MISSING = object()


class UnresolvedTemplateError(ValueError):
    """A routing template referenced a path with no scalar value, or used invalid
    `{{ }}` syntax (unbalanced delimiters, a filter, anything but a bare dotted path) —
    reject, no fallback."""


def resolve_path(context: dict[str, Any], dotted_path: str) -> Any:
    """Pure traversal (dict keys / list int indices). `_MISSING` sentinel if absent."""
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


def _reject_stray_delimiters(literal: str) -> None:
    """A literal (non-token) segment must contain no `{{`/`}}` at all. Catches every
    malformed combination a count-equality check misses — reversed order
    (`"}} stray {{"`), one-sided, or any text a regex of complete matches simply skips
    over because it never finds a matching close/open (task-3 fix-1-review finding I7)."""
    if "{{" in literal or "}}" in literal:
        raise UnresolvedTemplateError(f"stray '{{{{' or '}}}}' delimiter outside a valid expression: {literal!r}")


def render_routing_template(template: str, context: dict[str, Any]) -> str:
    """Substitute every `{{ path }}` token; raise `UnresolvedTemplateError` on the first
    missing/non-scalar path, on any `{{ }}` span that is not a bare dotted path (a
    filter, an empty expression), or on any `{{`/`}}` left over outside a matched span —
    regardless of ordering. No `default()`/`json` filters — routing overrides only ever
    need a plain scalar `event.*`/`payload.*` value (contract §2)."""
    out: list[str] = []
    pos = 0
    for m in _ANY_BRACE_RE.finditer(template):
        literal = template[pos : m.start()]
        _reject_stray_delimiters(literal)
        out.append(literal)
        path = m.group(1).strip()
        if not _VALID_PATH_RE.match(path):
            raise UnresolvedTemplateError(f"invalid routing template expression: {{{{ {m.group(1)} }}}}")
        value = resolve_path(context, path)
        if value is _MISSING or value is None or isinstance(value, (dict, list)):
            raise UnresolvedTemplateError(f"unresolved or non-scalar path: {path!r}")
        out.append(str(value))
        pos = m.end()
    trailing = template[pos:]
    _reject_stray_delimiters(trailing)
    out.append(trailing)
    return "".join(out)

# SPDX-License-Identifier: Apache-2.0
"""Tolerant reader for the ach:sessions stream. The entry schema (v:1) is a cross-component
contract: missing field -> typed default, unknown field -> ignored, dispatch on `v` (spec §4.2)."""

from __future__ import annotations

from typing import Any

_STREAM = "ach:sessions"


def _int(fields: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(fields[key])
    except (KeyError, ValueError):
        return default


def _float(fields: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(fields[key])
    except (KeyError, ValueError):
        return default


def parse_entry(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """Typed, tolerant projection of one stream entry. `v` dispatch is forward-compatible."""
    ts_ms = _int(fields, "ts", default=int(entry_id.split("-")[0]))
    return {
        "v": fields.get("v", "1"),
        "ts_ms": ts_ms,
        "session_key": fields.get("session_key", "unknown"),
        "channel": fields.get("channel", "unknown"),
        "source": fields.get("source", "unknown"),
        "model": fields.get("model", "unknown"),
        "task": fields.get("task", ""),
        "input_tokens": _int(fields, "input_tokens"),
        "output_tokens": _int(fields, "output_tokens"),
        "cache_read": _int(fields, "cache_read"),
        "cache_write": _int(fields, "cache_write"),
        "cost": _float(fields, "cost"),
        "turns": _int(fields, "turns"),
        "duration_ms": _int(fields, "duration_ms"),
        "tokens_per_s": _float(fields, "tokens_per_s"),
        "status": fields.get("status", "unknown"),
        "retry": fields.get("retry", "false") == "true",
    }


async def read_window(client: Any, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    raw = await client.xrange(_STREAM, min=f"{start_ms}", max=f"{end_ms}")
    return [parse_entry(eid, fields) for eid, fields in raw]


async def read_recent(client: Any, n: int) -> list[dict[str, Any]]:
    raw = await client.xrevrange(_STREAM, count=n)
    return [parse_entry(eid, fields) for eid, fields in raw]


async def read_coverage_start(client: Any) -> int | None:
    raw = await client.xrange(_STREAM, count=1)
    if not raw:
        return None
    return int(parse_entry(raw[0][0], raw[0][1])["ts_ms"])

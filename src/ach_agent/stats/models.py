# SPDX-License-Identifier: Apache-2.0
"""SessionStat — one record per invocation, serialized to a versioned redis-stream entry.

Entry schema is a CROSS-COMPONENT CONTRACT (harness writes, ach-stats reads, deployed
independently). Every entry carries `v="1"`; a future breaking change bumps it. See design spec
§4.1/§4.2.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Truncate + redact the inbound task text before it is persisted to redis.
#
# The bearer `ek_` and provider keys must never leave the process in a stored record. We scrub
# FIRST (so a secret beyond the truncation boundary is still removed), then truncate to a bounded
# length for the recent-sessions table. Keep the scrub patterns aligned with the structlog `ek_`
# redaction processor (grep: `rg -n 'ek_' src/ach_agent | rg -i 'redact|scrub|processor'`).
_MAX = 80
# ek_… bearer, sk-… provider keys, generic long token after "bearer".
_SECRET = re.compile(r"(ek_[A-Za-z0-9_\-]+|sk-[A-Za-z0-9_\-]+)")


def redact_task(text: str) -> str:
    """Scrub bearer/API tokens, then truncate to <=80 chars."""
    scrubbed = _SECRET.sub("[redacted]", text)
    return scrubbed[:_MAX]


@dataclass(slots=True, frozen=True)
class SessionStat:
    ts_ms: int
    session_key: str
    channel: str
    source: str
    model: str
    provider: str
    task: str  # already redacted+truncated
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    cost: float
    turns: int
    duration_ms: int
    tokens_per_s: float
    status: str
    retry: bool

    @classmethod
    def build(
        cls,
        *,
        ts_ms: int,
        session_key: str,
        channel: str,
        source: str,
        model: str,
        provider: str,
        raw_task: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        cache_write: int,
        cost: float,
        turns: int,
        duration_ms: int,
        status: str,
        retry: bool,
    ) -> SessionStat:
        tps = (output_tokens / (duration_ms / 1000.0)) if duration_ms > 0 else 0.0
        return cls(
            ts_ms=ts_ms,
            session_key=session_key,
            channel=channel,
            source=source,
            model=model,
            provider=provider,
            task=redact_task(raw_task),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            cost=cost,
            turns=turns,
            duration_ms=duration_ms,
            tokens_per_s=tps,
            status=status,
            retry=retry,
        )

    def to_entry(self) -> dict[str, str]:
        """Redis-stream field map: all values are strings (stream fields are byte strings)."""
        return {
            "v": "1",
            "ts": str(self.ts_ms),
            "session_key": self.session_key,
            "channel": self.channel,
            "source": self.source,
            "model": self.model,
            "provider": self.provider,
            "task": self.task,
            "input_tokens": str(self.input_tokens),
            "output_tokens": str(self.output_tokens),
            "cache_read": str(self.cache_read),
            "cache_write": str(self.cache_write),
            "cost": repr(self.cost),
            "turns": str(self.turns),
            "duration_ms": str(self.duration_ms),
            "tokens_per_s": repr(self.tokens_per_s),
            "status": self.status,
            "retry": "true" if self.retry else "false",
        }


@dataclass(slots=True, frozen=True)
class ToolStat:
    """One record per tool call (Tier 1 agent trace). Fields are OTel gen_ai.*-named so a
    future OTLP export maps 1:1: tool→gen_ai.tool.name, session_key→gen_ai.conversation.id,
    status=error→error.type. Written to the ``ach:tools`` stream (parallel to ``ach:sessions``).

    Stores SIZES of tool input/output, never the raw args/result — those can carry secrets
    and inflate the stream. Add raw capture behind a flag if a consumer ever needs it.
    """

    ts_ms: int
    session_key: str
    channel: str
    source: str
    model: str
    provider: str
    tool: str  # cleaned display name (e.g. mcp-gitlab-ro/gitlab_get_merge_request, bash)
    tool_type: str  # "mcp" | "builtin"
    status: str  # "completed" | "error"
    duration_ms: int | None  # None when the 'running' event was missed (no start stamp)
    input_size: int
    output_size: int
    error: str  # truncated error text, "" on success

    def to_entry(self) -> dict[str, str]:
        return {
            "v": "1",
            "ts": str(self.ts_ms),
            "session_key": self.session_key,
            "channel": self.channel,
            "source": self.source,
            "model": self.model,
            "provider": self.provider,
            "tool": self.tool,
            "tool_type": self.tool_type,
            "status": self.status,
            "duration_ms": "" if self.duration_ms is None else str(self.duration_ms),
            "input_size": str(self.input_size),
            "output_size": str(self.output_size),
            "error": self.error,
        }

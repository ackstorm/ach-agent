# SPDX-License-Identifier: Apache-2.0
"""SessionStat — one record per invocation, serialized to a versioned redis-stream entry.

Entry schema is a CROSS-COMPONENT CONTRACT (harness writes, ach-stats reads, deployed
independently). Every entry carries `v="1"`; a future breaking change bumps it. See design spec
§4.1/§4.2.
"""

from __future__ import annotations

from dataclasses import dataclass

from ach_agent.stats.redact import redact_task


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

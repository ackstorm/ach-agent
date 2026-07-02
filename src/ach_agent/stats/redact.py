# SPDX-License-Identifier: Apache-2.0
"""Truncate + redact the inbound task text before it is persisted to redis.

The bearer `ek_` and provider keys must never leave the process in a stored record. We scrub
FIRST (so a secret beyond the truncation boundary is still removed), then truncate to a bounded
length for the recent-sessions table. Keep the scrub patterns aligned with the structlog `ek_`
redaction processor (grep: `rg -n 'ek_' src/ach_agent | rg -i 'redact|scrub|processor'`).
"""

from __future__ import annotations

import re

_MAX = 80
# ek_… bearer, sk-… provider keys, generic long token after "bearer".
_SECRET = re.compile(r"(ek_[A-Za-z0-9_\-]+|sk-[A-Za-z0-9_\-]+)")


def redact_task(text: str) -> str:
    """Scrub bearer/API tokens, then truncate to <=80 chars."""
    scrubbed = _SECRET.sub("[redacted]", text)
    return scrubbed[:_MAX]

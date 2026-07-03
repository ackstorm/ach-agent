# SPDX-License-Identifier: Apache-2.0
"""Codemem memory backend — static boot-time wiring (db_path + project resolution).

codemem is a stdio MCP server (model-managed, project-scoped). Its wiring is resolved
once at boot in resolve_codemem_wiring — codemem is static per-agent, so it does not
belong in the per-invocation hindsight adapter.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)

TYPE = "codemem"

TOOLS_SPEC = (
    "You have a persistent project memory (codemem)."
    " It is project-scoped; you do not pass the project.\n"
    "- `memory_search(query)`: hybrid lexical+semantic search"
    " — call BEFORE acting to recall prior context.\n"
    "- `memory_timeline()`: browse recent memories chronologically.\n"
    "- `memory_pack(query)`: build a focused context pack for a topic.\n"
    "- `memory_remember(content)`: store a durable fact/decision"
    " worth recalling in a future session.\n"
    "- `memory_forget(id)`: deactivate a memory that is wrong or obsolete.\n"
    "- `memory_get_observations(ids)`: fetch full detail for memory ids"
    " from a search/timeline result.\n"
    "Prefer search before remember to avoid duplicates;"
    " remember decisions and constraints, not chatter."
)


def resolve_codemem_wiring(cfg: Any) -> tuple[str, str]:
    """Resolve (db_path, project) for the codemem backend — static per-agent, at boot.

    Returns ("", "") when memory is not codemem, or when the `codemem` binary is not on
    PATH (fail-open, MEM-02/D-02: degrade, increment MEMORY_DEGRADED, never raise).

    db_path derives from persistence when omitted (mirrors resolve_engine_paths):
      - persistence.enabled → <mountPath>/state/codemem.db
      - else                → /tmp/ach-home/state/codemem.db
    project defaults to the schema constant ("ach-agent"); both are config-overridable.
    """
    from ach_agent.config.schema import CodememMemory

    if not isinstance(cfg.memory, CodememMemory):
        return "", ""

    import shutil

    if shutil.which("codemem") is None:
        from ach_agent.memory.hindsight import _inc_memory_degraded

        log.warning("codemem binary not on PATH — running degraded (MEM-02, D-02)")
        _inc_memory_degraded()
        return "", ""

    cm = cfg.memory.codemem
    base = cfg.persistence.mount_path if cfg.persistence.enabled else "/tmp/ach-home"
    db_path = cm.db_path or f"{base}/state/codemem.db"
    log.info("memory: codemem backend active", db_path=db_path, project=cm.project)
    return db_path, cm.project

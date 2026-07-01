# SPDX-License-Identifier: Apache-2.0
"""Thin re-export shim — logic has moved to ach_agent.memory.hindsight.

Import from ach_agent.memory.hindsight directly for new code.
This module is kept for back-compat so existing callers and test patches targeting
'ach_agent.memory.adapter.*' continue to work without modification.
"""

from __future__ import annotations

from ach_agent.memory.hindsight import (
    _inc_memory_degraded as _inc_memory_degraded,
)
from ach_agent.memory.hindsight import (
    fetch_mental_model_summaries as fetch_mental_model_summaries,
)
from ach_agent.memory.hindsight import (
    prepare_memory as prepare_memory,
)
from ach_agent.memory.hindsight import (
    probe_memory_endpoint as probe_memory_endpoint,
)

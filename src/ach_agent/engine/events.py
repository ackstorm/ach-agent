# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: event vocab split into engine/base/events.py (shared) and
engine/opencode/events.py (opencode SSE parser) in SP1. Re-exported so existing
`from ach_agent.engine.events import …` sites keep resolving."""

from ach_agent.engine.base.events import (  # noqa: F401
    EngineError,
    InvocationTimeout,
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ToolState,
    ToolStateCompleted,
    ToolStateError,
    ToolStateRunning,
)
from ach_agent.engine.opencode.events import (  # noqa: F401
    OpenCodeEvent,
    OpenCodeSessionError,
    OpenCodeSessionIdle,
    OpenCodeStreamReady,
    OpenCodeTextUpdate,
    OpenCodeUserMessage,
    ReplyAccumulator,
    _consume_events_from_response,
    _SendFailed,
    parse_opencode_event,
)

# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: event vocab split into engine/base/events.py (shared) and
engine/opencode/events.py (opencode SSE parser) in SP1. Re-exported so existing
`from ach_agent.engine.events import …` sites keep resolving."""

from ach_agent.engine.base.events import EngineError as EngineError
from ach_agent.engine.base.events import InvocationTimeout as InvocationTimeout
from ach_agent.engine.base.events import OpenCodeToolUpdate as OpenCodeToolUpdate
from ach_agent.engine.base.events import OpenCodeUsage as OpenCodeUsage
from ach_agent.engine.base.events import ToolState as ToolState
from ach_agent.engine.base.events import ToolStateCompleted as ToolStateCompleted
from ach_agent.engine.base.events import ToolStateError as ToolStateError
from ach_agent.engine.base.events import ToolStateRunning as ToolStateRunning
from ach_agent.engine.opencode.events import OpenCodeEvent as OpenCodeEvent
from ach_agent.engine.opencode.events import OpenCodeSessionError as OpenCodeSessionError
from ach_agent.engine.opencode.events import OpenCodeSessionIdle as OpenCodeSessionIdle
from ach_agent.engine.opencode.events import OpenCodeStreamReady as OpenCodeStreamReady
from ach_agent.engine.opencode.events import OpenCodeTextUpdate as OpenCodeTextUpdate
from ach_agent.engine.opencode.events import OpenCodeUserMessage as OpenCodeUserMessage
from ach_agent.engine.opencode.events import ReplyAccumulator as ReplyAccumulator
from ach_agent.engine.opencode.events import (
    _consume_events_from_response as _consume_events_from_response,
)
from ach_agent.engine.opencode.events import _SendFailed as _SendFailed
from ach_agent.engine.opencode.events import parse_opencode_event as parse_opencode_event

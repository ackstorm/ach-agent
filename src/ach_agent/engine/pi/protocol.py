# SPDX-License-Identifier: Apache-2.0
"""Pi --mode rpc wire vocabulary.

Isolated here so a real-pi mismatch is a one-line fix and unit-test fixtures stay
in sync.
"""

from __future__ import annotations

# Commands (harness → pi, over stdin)
CMD_PROMPT = "prompt"
CMD_ABORT = "abort"
CMD_NEW_SESSION = "new_session"
CMD_SWITCH_SESSION = "switch_session"
CMD_GET_STATE = "get_state"

# Events (pi → harness, over stdout)
EV_MESSAGE_UPDATE = "message_update"
EV_ASSISTANT_INNER = "assistantMessageEvent"
EV_INNER_TEXT_DELTA = "text_delta"
EV_TOOL_START = "tool_execution_start"
EV_TOOL_END = "tool_execution_end"
EV_AGENT_SETTLED = "agent_settled"
EV_AGENT_END = "agent_end"  # Pi 0.79 compatibility; newer builds use agent_settled.
EV_SESSION_CREATED = "session_created"
EV_EOF = "__eof__"

# Field names
F_SESSION_PATH = "sessionPath"
F_TEXT = "text"
F_DELTA = "delta"
F_TOOL_NAME = "toolName"
F_CALL_ID = "callId"
F_TOOL_CALL_ID = "toolCallId"
F_INPUT = "input"
F_ARGS = "args"
F_OUTPUT = "output"
F_RESULT = "result"
F_ERROR = "error"
F_IS_ERROR = "isError"
F_TITLE = "title"

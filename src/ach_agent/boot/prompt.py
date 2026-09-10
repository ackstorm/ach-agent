# SPDX-License-Identifier: Apache-2.0
"""Prompt assembly: what text the engine gets.

Builds the per-turn engine prompt from a MessageEvent + channel config, the harness-owned
terminal-contract <output_format> block (per channel class), and the resolved persona
(prompt.system) from the operator config.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig, PromptBlock
from ach_agent.templating import build_template_context, render_template

log = structlog.get_logger(__name__)


def build_engine_prompt(
    event: MessageEvent,
    channel_cfg: ChannelConfig | None = None,
    agent_name: str = "",
    memory_bank: str = "",
) -> str:
    """Build a meaningful engine prompt from a MessageEvent.

    When the channel declares a `prompt` template, it wins: it is rendered through the
    {{ }} engine against the event payload + harness internals (channel.prompt is the
    contract-specified per-channel instruction). Otherwise the legacy fallback applies:
    cron `scheduled_tick`, free-form `payload['text']`, or a built MR review instruction.

    Never raises; falls back to an empty string if no usable content is found.
    """
    # Channel-prompt path: render the contract-authored template (CONTRACT §2 channel.prompt)
    if channel_cfg is not None and channel_cfg.prompt:
        ctx = build_template_context(
            event.payload,
            channel_name=event.channel_name,
            channel_type=channel_cfg.type or "",
            channel_source=channel_cfg.source or "",
            agent_name=agent_name,
            memory_bank=memory_bank,
            event_id=event.idempotency_key,
            session_key=event.session_key,
        )
        return render_template(channel_cfg.prompt, ctx)

    # Cron path: payload has a scheduled_tick key
    scheduled_tick = event.payload.get("scheduled_tick")
    if scheduled_tick is not None:
        return str(scheduled_tick)

    # Free-form text path: the --tui console (and queue/a2a) carry the prompt verbatim
    # in payload['text']. In console mode the typed line IS the prompt.
    text = event.payload.get("text")
    if text:
        return str(text)

    # Webhook path: build prompt from delivery_context + payload, per event kind.
    # Missing "kind" defaults to merge_request (back-compat with pre-Task-1 events).
    dc = event.delivery_context
    project_id = dc.get("project_id", "")
    kind = dc.get("kind", "merge_request")

    obj_attrs: dict[str, Any] = {}
    raw_obj_attrs = event.payload.get("object_attributes")
    if isinstance(raw_obj_attrs, dict):
        obj_attrs = raw_obj_attrs

    if kind == "note":
        # A comment on an MR or issue: give the agent the note body + the target reference
        # so it can fetch context via MCP. Never emit an empty "Review MR !." line.
        target_type = dc.get("target_type", "")
        if target_type == "issue":
            ref = f"issue #{dc.get('issue_iid', '')}"
        else:
            ref = f"MR !{dc.get('mr_iid', '')}"
        raw_user = event.payload.get("user")
        user = raw_user.get("username", "") if isinstance(raw_user, dict) else ""
        note = obj_attrs.get("note", "")
        header = f"New comment on {ref} in project {project_id}"
        header = f"{header} by {user}:" if user else f"{header}:"
        parts = [header]
        if note:
            parts.append(str(note))
        return " ".join(parts)

    title = obj_attrs.get("title", "")
    description = obj_attrs.get("description", "")

    if kind == "issue":
        issue_iid = dc.get("issue_iid", "")
        parts = [f"Review issue #{issue_iid} in project {project_id}."]
    else:  # merge_request (default)
        mr_iid = dc.get("mr_iid", "")
        parts = [f"Review MR !{mr_iid} in project {project_id}."]
    if title:
        parts.append(f"Title: {title}")
    if description:
        parts.append(f"Description: {description}")

    return " ".join(parts)


# Harness-owned terminal-contract directive, appended per channel class to every
# structured turn (NOT free-form tui). The terminal JSON envelope is harness IP — the
# harness parses it — so operators never hand-write it in channel.prompt. This is the
# ONLY per-turn place the model is told which action its final object must carry; without
# it a model can emit a valid-but-wrong {"action":"none"} on an a2a turn, which
# extract_terminal accepts and the a2a path (main.py) then delivers to the caller as a
# FAILURE (on_fail). See operator contract §8.
#
# Each block exposes ONLY the action its channel class expects — the a2a block never
# names "none" (naming the wrong action just plants it: pink-elephant). The matching
# lifecycle repair/wrap turns are kept action-consistent via run_invocation(terminal_action=…).
A2A_OUTPUT_INSTRUCTIONS = (
    "<output_format>\n"
    "Do your reasoning and tool work first. Then END your reply with exactly one compact "
    "JSON object on its own line — this object is the ONLY thing delivered to the caller:\n"
    '{"action":"a2a_reply","text":"<RESULT>","thoughts":"<OPTIONAL>"}\n'
    '"text" is what the caller reads — keep it non-empty. Single line, keys in this order, '
    "no code fences.\n"
    "</output_format>"
)

NONE_OUTPUT_INSTRUCTIONS = (
    "<output_format>\n"
    "Do your reasoning and tool work first. Then END your reply with exactly one compact "
    "JSON object on its own line:\n"
    '{"action":"none","text":"<SUMMARY>","thoughts":"<OPTIONAL>"}\n'
    "Do all real work through your tools; this object only reports completion. Single line, "
    "keys in this order, no code fences.\n"
    "</output_format>"
)


def terminal_action_for(channel_cfg: ChannelConfig | None, free_form: bool) -> str:
    """The terminal action this turn's channel class expects — the single source of truth
    the harness reuses for both the up-front <output_format> block and the lifecycle
    repair/wrap turns. a2a → 'a2a_reply'; every other class (and a missing type) → 'none'.
    free_form (--tui) has no contract and skips extraction, so its value is unused ('none').
    """
    if not free_form and getattr(channel_cfg, "type", None) == "a2a":
        return "a2a_reply"
    return "none"


def build_output_instructions(channel_cfg: ChannelConfig | None, free_form: bool) -> str:
    """Return the harness-owned <output_format> block for this turn, or "".

    free_form (--tui console) → "" (no terminal contract). Otherwise the block for the
    channel class's expected action (terminal_action_for): a2a_reply for a2a, none else.
    """
    if free_form:
        return ""
    if terminal_action_for(channel_cfg, free_form) == "a2a_reply":
        return A2A_OUTPUT_INSTRUCTIONS
    return NONE_OUTPUT_INSTRUCTIONS


def resolve_system_prompt(prompt_block: PromptBlock | None, state_dir: Path) -> str:
    """Resolve prompt.system (text | file | ach | None) into the persona string.

    text → the inline text. file → <state_dir>/<file>. ach → the named hydrated prompt at
    <state_dir>/prompts/<ach>/ (its sole file, or the given `file` subpath). For every
    on-disk form the resolved REAL path is re-checked to stay inside state_dir (defense in
    depth over the schema validator, which only sees the literal path), and a missing file
    is a hard startup failure — a persona the operator declared but hydration did not deliver
    is a misconfiguration, not fail-open. None → "" (no persona).
    """
    if prompt_block is None or prompt_block.system is None:
        return ""
    system = prompt_block.system
    if system.type == "text":
        return str(system.text)
    root = state_dir.resolve()
    if system.type == "file":
        target = (root / str(system.file)).resolve()
    else:  # ach — resolve the named prompt dir, then pick its file
        prompt_dir = (root / "prompts" / str(system.ach)).resolve()
        if not prompt_dir.is_relative_to(root) or not prompt_dir.is_dir():
            log.error("prompt.system.ach not hydrated under .ach-state/prompts", ach=system.ach)
            sys.exit(1)
        if system.file:
            target = (prompt_dir / str(system.file)).resolve()
        else:
            files = sorted(p for p in prompt_dir.rglob("*") if p.is_file())
            if len(files) != 1:
                log.error(
                    "prompt.system.ach needs an explicit `file:` — the prompt dir has 0 or "
                    ">1 files",
                    ach=system.ach,
                    count=len(files),
                    files=[f.name for f in files],
                )
                sys.exit(1)
            target = files[0].resolve()
    if not target.is_relative_to(root):
        log.error("prompt.system file escapes .ach-state", path=str(target))
        sys.exit(1)
    if not target.is_file():
        log.error("prompt.system file not found under .ach-state", path=str(target))
        sys.exit(1)
    return target.read_text(encoding="utf-8")

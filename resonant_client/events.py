"""
Shared event protocol between engine and client.

All communication flows as typed events with JSON-serializable payloads.
This module is imported by both engine and TUI — keep it dependency-free.
"""

from enum import Enum
from typing import Any


# ── Backend-level events (from backends.py, unchanged) ─────────────────
# These are internal to the engine; clients never see them directly.
BACKEND_TEXT_DELTA = "text.delta"
BACKEND_TOOL_CALL = "tool_call"
BACKEND_DONE = "done"
BACKEND_ERROR = "error"


# ── Engine → Client events ─────────────────────────────────────────────
# These are what the TUI (or any future GUI/web client) consumes.

class EngineEvent(str, Enum):
    """Events streamed from engine to client."""

    # Session lifecycle
    SESSION_START = "session.start"           # New session created
    SESSION_END = "session.end"               # Session finished

    # Step lifecycle
    STEP_START = "step.start"                 # New agentic step beginning
    STEP_END = "step.end"                     # Step completed

    # Streaming text from LLM
    TEXT_DELTA = "text.delta"                 # Incremental text token
    TEXT_DONE = "text.done"                   # Full text block complete

    # Tool lifecycle
    TOOL_CALL = "tool.call"                   # Tool about to be executed
    TOOL_RESULT = "tool.result"               # Tool execution result
    TOOL_PERMISSION = "tool.permission"       # Requesting user permission

    # Plan mode
    PLAN_GENERATED = "plan.generated"         # Plan text ready for review
    PLAN_APPROVED = "plan.approved"           # User approved the plan
    PLAN_REJECTED = "plan.rejected"           # User rejected the plan
    AUTO_PLAN_TRIGGERED = "auto_plan.triggered"  # Auto-plan classified as complex

    # Sub-agent lifecycle
    SUBAGENT_START = "subagent.start"         # Sub-agent spawned
    SUBAGENT_END = "subagent.end"             # Sub-agent completed

    # Context management
    COMPRESSION = "context.compression"      # Context was compressed

    # Status / metadata
    STATUS = "status"                         # Model info, token counts, timing
    ERROR = "error"                           # Error occurred

    # Choices (model asks user to pick)
    CHOICES = "choices"                       # Model presented options

    # Task list progress (markdown `- [ ]` / `- [x]` in assistant text)
    TODOS_UPDATED = "todos.updated"


# ── Client → Engine commands ───────────────────────────────────────────

class ClientCommand(str, Enum):
    """Commands sent from client to engine."""

    # Chat
    MESSAGE = "message"                       # User message to process
    CANCEL = "cancel"                         # Cancel current operation

    # Tool permission responses
    APPROVE = "approve"                       # Approve tool execution
    DENY = "deny"                             # Deny tool execution

    # Plan responses
    PLAN_APPROVE = "plan.approve"
    PLAN_REJECT = "plan.reject"
    PLAN_EDIT = "plan.edit"                   # With refinement text

    # Choice response
    CHOICE_SELECT = "choice.select"           # Selected option index/text

    # Session management
    CLEAR = "clear"                           # Clear conversation
    SWITCH_MODEL = "switch.model"             # Change model
    SWITCH_BACKEND = "switch.backend"         # Change backend


def make_event(event_type: EngineEvent, **payload) -> dict:
    """Create a serializable event dict."""
    return {"event": event_type.value, **payload}


def make_command(cmd_type: ClientCommand, **payload) -> dict:
    """Create a serializable command dict."""
    return {"command": cmd_type.value, **payload}

"""
Hook System for Resonant Engine.

Hooks allow users to run shell commands in response to engine events.
Similar to Claude Code's hook system:
  - PreToolUse: Run before a tool executes (can block)
  - PostToolUse: Run after a tool executes
  - SessionStart: Run when a session begins
  - SessionEnd: Run when a session ends
  - UserPromptSubmit: Run when user submits a message
"""

import logging
import json
import os
import fnmatch
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from resonant_client.processes import background_process_kwargs

logger = logging.getLogger(__name__)


class HookType(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    PRE_TOOL_BATCH = "pre_tool_batch"
    POST_TOOL_BATCH = "post_tool_batch"
    BEFORE_MODEL = "before_model"
    AFTER_MODEL = "after_model"
    PERMISSION_REQUEST = "permission_request"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_STOP = "subagent_stop"
    TASK_CREATED = "task_created"
    TASK_COMPLETED = "task_completed"
    PRE_COMPACT = "pre_compact"
    POST_COMPACT = "post_compact"
    CHECKPOINT_CREATED = "checkpoint_created"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    VALIDATION_COMPLETE = "validation_complete"
    USER_INPUT_REQUEST = "user_input_request"
    WORKTREE_CREATE = "worktree_create"
    WORKTREE_REMOVE = "worktree_remove"
    SESSION_ERROR = "session_error"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    USER_PROMPT_SUBMIT = "user_prompt_submit"


@dataclass
class HookDefinition:
    """A single hook definition."""
    hook_type: HookType
    command: str  # Shell command to run
    tool_name: str = ""  # Optional filter — only trigger for this tool
    enabled: bool = True
    name: str = ""  # Display name
    matcher: str = ""
    input_format: str = "env"  # env (legacy) | json
    timeout_seconds: float = 30.0

    def matches(self, hook_type: HookType, tool_name: str = "") -> bool:
        """Check if this hook should trigger for the given event."""
        if not self.enabled:
            return False
        if self.hook_type != hook_type:
            return False
        pattern = self.matcher or self.tool_name
        if pattern and not fnmatch.fnmatch(tool_name or "", pattern):
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "hook_type": self.hook_type.value,
            "command": self.command,
            "tool_name": self.tool_name,
            "enabled": self.enabled,
            "name": self.name,
            "matcher": self.matcher,
            "input_format": self.input_format,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HookDefinition":
        return cls(
            hook_type=HookType(data.get("hook_type", "pre_tool_use")),
            command=data.get("command", ""),
            tool_name=data.get("tool_name", ""),
            enabled=data.get("enabled", True),
            name=data.get("name", ""),
            matcher=data.get("matcher", ""),
            input_format=("json" if data.get("input_format") == "json" else "env"),
            timeout_seconds=max(0.1, float(data.get("timeout_seconds", 30.0) or 30.0)),
        )


@dataclass
class HookResult:
    """Result of running a hook."""
    allowed: bool = True  # False = block the action
    output: str = ""
    exit_code: int = 0
    error: str = ""
    decision: str = "allow"
    reason: str = ""
    additional_context: str = ""
    modified_args: Optional[dict] = None
    retry: bool = False
    continue_run: bool = True
    metadata: Optional[dict] = None


class HookRunner:
    """Runs hooks based on settings."""

    def __init__(self, settings=None):
        self._settings = settings
        self._hooks: list[HookDefinition] = []
        if settings:
            self._load_from_settings()

    def _load_from_settings(self):
        """Load hook definitions from settings."""
        hook_data = self._settings.get("hooks") if self._settings else []
        if not hook_data:
            hook_data = []
        self._hooks = []
        for item in hook_data:
            if isinstance(item, dict):
                try:
                    self._hooks.append(HookDefinition.from_dict(item))
                except (ValueError, KeyError) as e:
                    logger.warning(f"Invalid hook definition: {e}")

    def reload(self):
        """Reload hooks from settings."""
        self._load_from_settings()

    def run_hooks(
        self,
        hook_type: HookType,
        context: Optional[dict] = None,
        tool_name: str = "",
    ) -> HookResult:
        """Run all matching hooks. Returns combined result.

        For PreToolUse: exit code 0 = allow, non-zero = block.
        For others: exit code is informational.

        Legacy hooks receive environment variables:
          RESONANT_HOOK_TYPE, RESONANT_TOOL_NAME, RESONANT_TOOL_ARGS,
          RESONANT_PROJECT_PATH
        """
        context = context or {}
        matching = [h for h in self._hooks if h.matches(hook_type, tool_name)]

        if not matching:
            return HookResult(allowed=True)

        combined = HookResult(allowed=True)

        for hook in matching:
            env = os.environ.copy()
            env["RESONANT_HOOK_TYPE"] = hook_type.value
            env["RESONANT_TOOL_NAME"] = tool_name or ""
            env["RESONANT_TOOL_ARGS"] = str(context.get("tool_args", ""))
            env["RESONANT_PROJECT_PATH"] = context.get("project_path", os.getcwd())
            payload = {
                "hook_event_name": hook_type.value,
                "tool_name": tool_name or "",
                "project_path": context.get("project_path", os.getcwd()),
                "timestamp": context.get("timestamp"),
                **context,
            }

            try:
                result = subprocess.run(
                    hook.command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    input=(json.dumps(payload, ensure_ascii=False, default=str)
                           if hook.input_format == "json" else None),
                    timeout=hook.timeout_seconds,
                    env=env,
                    cwd=context.get("project_path", None),
                    **background_process_kwargs(),
                )
                combined.output += result.stdout
                combined.exit_code = result.returncode

                if hook.input_format == "json" and result.stdout.strip():
                    try:
                        response = json.loads(result.stdout)
                    except json.JSONDecodeError:
                        response = {}
                        combined.error = "Structured hook returned invalid JSON"
                    if isinstance(response, dict):
                        decision = str(response.get("decision") or "allow").lower()
                        combined.decision = decision
                        combined.reason = str(response.get("reason") or "")
                        combined.additional_context += str(
                            response.get("additional_context")
                            or (response.get("hookSpecificOutput") or {}).get("additionalContext")
                            or ""
                        )
                        modified = response.get("modified_args") or response.get("updatedInput")
                        if isinstance(modified, dict):
                            combined.modified_args = modified
                        combined.retry = combined.retry or bool(response.get("retry"))
                        combined.continue_run = combined.continue_run and bool(
                            response.get("continue", True)
                        )
                        combined.metadata = response.get("metadata") or combined.metadata
                        if decision in {"deny", "block", "ask"}:
                            combined.allowed = False

                if result.returncode != 0:
                    combined.error = result.stderr or f"Hook exited with code {result.returncode}"
                    if hook_type in {
                        HookType.PRE_TOOL_USE,
                        HookType.PRE_TOOL_BATCH,
                        HookType.BEFORE_MODEL,
                        HookType.PERMISSION_REQUEST,
                        HookType.TASK_COMPLETED,
                        HookType.SUBAGENT_STOP,
                        HookType.VALIDATION_COMPLETE,
                    }:
                        combined.allowed = False
                        logger.info(f"Hook blocked tool {tool_name}: {hook.name or hook.command}")
                        break  # Stop on first blocking hook

            except subprocess.TimeoutExpired:
                combined.error = f"Hook timed out: {hook.command}"
                logger.warning(combined.error)
            except Exception as e:
                combined.error = str(e)
                logger.error(f"Hook execution error: {e}")

        return combined

    def emit(
        self,
        hook_type: HookType,
        context: Optional[dict] = None,
        *,
        tool_name: str = "",
    ) -> HookResult:
        """Semantic alias used by new lifecycle call sites."""
        return self.run_hooks(hook_type, context=context, tool_name=tool_name)

    @property
    def hooks(self) -> list[HookDefinition]:
        return self._hooks

    def add_hooks(self, hooks: list[HookDefinition]) -> None:
        """Register trusted capability-pack hooks without duplicating them."""
        existing = {
            (hook.hook_type, hook.command, hook.matcher, hook.tool_name)
            for hook in self._hooks
        }
        for hook in hooks:
            key = (hook.hook_type, hook.command, hook.matcher, hook.tool_name)
            if key not in existing:
                self._hooks.append(hook)
                existing.add(key)

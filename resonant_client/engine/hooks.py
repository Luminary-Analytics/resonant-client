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
import os
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class HookType(str, Enum):
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
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

    def matches(self, hook_type: HookType, tool_name: str = "") -> bool:
        """Check if this hook should trigger for the given event."""
        if not self.enabled:
            return False
        if self.hook_type != hook_type:
            return False
        if self.tool_name and tool_name and self.tool_name != tool_name:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "hook_type": self.hook_type.value,
            "command": self.command,
            "tool_name": self.tool_name,
            "enabled": self.enabled,
            "name": self.name,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "HookDefinition":
        return cls(
            hook_type=HookType(data.get("hook_type", "pre_tool_use")),
            command=data.get("command", ""),
            tool_name=data.get("tool_name", ""),
            enabled=data.get("enabled", True),
            name=data.get("name", ""),
        )


@dataclass
class HookResult:
    """Result of running a hook."""
    allowed: bool = True  # False = block the action
    output: str = ""
    exit_code: int = 0
    error: str = ""


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

        Environment variables set:
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

            try:
                result = subprocess.run(
                    hook.command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env,
                    cwd=context.get("project_path", None),
                )
                combined.output += result.stdout
                combined.exit_code = result.returncode

                if result.returncode != 0:
                    combined.error = result.stderr or f"Hook exited with code {result.returncode}"
                    if hook_type == HookType.PRE_TOOL_USE:
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

    @property
    def hooks(self) -> list[HookDefinition]:
        return self._hooks

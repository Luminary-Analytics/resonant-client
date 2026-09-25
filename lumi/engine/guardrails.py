"""Commands the agent never runs, in any permission mode.

A short list of commands that damage the computer rather than a project:
deleting a whole drive or the home folder, changing permissions on the
whole file system, formatting or partitioning disks, writing raw data over
a disk, a fork bomb, shutting down. They are refused twice:

* before any approval prompt, as deny rules that come first in every
  tier's execution policy, ahead of organization and repository rules
  (``policies.policy_for_tier`` and ``policies.project_execution_policy``);
* just before a command runs, in the irreversibility floor
  (``orchestration/autonomy.check_floor``), so a session without an
  execution policy, or a hook that rewrites a command, can't get past them.

Matching is by pattern over the command text. It catches the obvious
mistake, not a command written to hide from it; the shell sandbox
(``engine/os_sandbox.py``) is what limits where commands can write.
"""

from __future__ import annotations

import re
from typing import Iterable

# A command name counts only where a command starts: at the beginning, or
# after ; & | ( or $( . "echo shutdown" and "grep -r mkfs docs" stay allowed.
_START = r"(?:^|[;&|(]|\$\()\s*(?:sudo\s+)?"
# The end of a path argument: the command ends, or another argument follows.
_END = r"['\"]?(?=$|[\s;&|)])"

GUARDRAILS: tuple[tuple[str, str], ...] = (
    (_START + r"rm\s+(-\S*\s+)*-\S*r\S*\s+(-\S+\s+)*['\"]?(/|/\*|~|~/|~/\*|\$home/?|\$\{home\}/?)" + _END,
     "Deleting the whole file system or your home folder"),
    (_START + r"(chmod|chown)\s+(-\S+\s+)*-\S*r\S*\s+(\S+\s+)?['\"]?/" + _END,
     "Changing permissions on the whole file system"),
    (_START + r"mkfs(\.\w+)?\s", "Formatting a disk"),
    (_START + r"dd\s[^;&|]*\bof=/dev/(sd|nvme|hd|disk|mmcblk|xvd|vd)", "Writing raw data over a disk"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "A fork bomb"),
    (_START + r"format(\.com)?\s+[a-z]:", "Formatting a drive"),
    (_START + r"diskpart(\.exe)?(?=$|[\s;&|)])", "Changing disk partitions"),
    (_START + r"(rd|rmdir)\s+(/s\s+/q|/q\s+/s)\s+['\"]?[a-z]:\\?" + _END, "Deleting a whole drive"),
    (_START + r"del\s+(/\S+\s+)*/s(\s+/\S+)*\s+['\"]?[a-z]:\\(\*(\.\*)?)?" + _END, "Deleting a whole drive"),
    # PowerShell's Remove-Item and its aliases, recursive, on a drive or the home folder.
    (_START + r"(remove-item|rm|ri|del|erase|rd|rmdir)\s(?=(?:[^;&|]*\s)?-r[a-z]*\b)"
     r"(?=(?:[^;&|]*\s)?['\"]?([a-z]:\\?\*?|~[\\/]?\*?|\$env:userprofile[\\/]?\*?|\$home[\\/]?\*?)" + _END + ")",
     "Deleting a whole drive or your home folder"),
    (_START + r"(shutdown|reboot|halt|poweroff)(?=$|[\s;&|)])", "Shutting down or restarting the computer"),
    (_START + r"(stop-computer|restart-computer)(?=$|[\s;&|)])", "Shutting down or restarting the computer"),
)

_COMPILED = tuple((re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in GUARDRAILS)

# Tools whose ``command`` argument is shell text.
SHELL_TOOLS = ("bash", "check_run")
# Tools whose ``command`` argument is a program and its arguments.
ARGV_TOOLS = ("job_start", "preview_start")


def blocked(command: str) -> str:
    """Why a shell command is never run, or "" when it isn't on the list."""
    text = str(command or "")
    for pattern, reason in _COMPILED:
        if pattern.search(text):
            return reason
    return ""


def blocked_argv(argv: Iterable[object]) -> str:
    """The same for a program and its arguments.

    Checks the words joined (``["rm", "-rf", "/"]``) and each word alone, which
    is how ``["bash", "-c", "rm -rf /"]`` or ``["cmd", "/c", "rd /s /q C:\\"]``
    carry a whole command.
    """
    words = [str(word) for word in argv]
    for text in (" ".join(words), *words):
        reason = blocked(text)
        if reason:
            return reason
    return ""


def blocked_call(tool_name: str, args: dict) -> str:
    """Why this tool call runs a command that's never allowed, or ""."""
    command = args.get("command") if isinstance(args, dict) else None
    if tool_name in SHELL_TOOLS and isinstance(command, str):
        return blocked(command)
    if tool_name in ARGV_TOOLS and isinstance(command, list):
        return blocked_argv(command)
    return ""


def refusal(reason: str) -> str:
    """What the model is told."""
    return (f"{reason} is never allowed, in any permission mode. If it's really needed, "
            "ask the user to run it themselves.")


def policy_rules() -> list:
    """The guardrails as deny rules for an execution policy (engine/policies.py)."""
    from .policies import PolicyRule

    return [
        PolicyRule(tool_pattern=tool, action="deny", arg_patterns={"command": pattern}, reason=refusal(reason))
        for tool in SHELL_TOOLS
        for pattern, reason in GUARDRAILS
    ]

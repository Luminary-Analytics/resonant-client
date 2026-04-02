"""
Declarative Execution Policies for Resonant Sessions.

Defines rules that control which tools are allowed, prompted, or denied.
Inspired by Codex CLI's Starlark rule system, simplified to JSON.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class PolicyAction(str, Enum):
    ALLOW = "allow"
    PROMPT = "prompt"
    DENY = "deny"


@dataclass
class PolicyRule:
    """A single rule in an execution policy."""

    tool_pattern: str  # Glob pattern matching tool name (e.g., "bash", "file_*", "*")
    action: str = "allow"  # allow | prompt | deny
    arg_patterns: dict[str, str] = field(default_factory=dict)  # Regex patterns for args
    reason: str = ""  # Human-readable explanation

    def matches(self, tool_name: str, tool_args: dict) -> bool:
        """Check if this rule matches a tool call."""
        # Match tool name by glob
        if not fnmatch.fnmatch(tool_name, self.tool_pattern):
            return False

        # Match argument patterns (all must match)
        for arg_key, pattern in self.arg_patterns.items():
            arg_value = str(tool_args.get(arg_key, ""))
            try:
                if not re.search(pattern, arg_value, re.IGNORECASE):
                    return False
            except re.error:
                return False

        return True

    @classmethod
    def from_dict(cls, data: dict) -> "PolicyRule":
        return cls(
            tool_pattern=data.get("tool_pattern", "*"),
            action=data.get("action", "allow"),
            arg_patterns=data.get("arg_patterns", {}),
            reason=data.get("reason", ""),
        )


class ExecutionPolicy:
    """
    Ordered set of rules that evaluate tool calls.

    First matching rule wins. No match = ALLOW (default permissive).
    """

    def __init__(self, rules: Optional[list[PolicyRule]] = None):
        self.rules = rules or []

    def evaluate(self, tool_name: str, tool_args: dict) -> PolicyAction:
        """Evaluate a tool call against the policy. Returns the action to take."""
        for rule in self.rules:
            if rule.matches(tool_name, tool_args):
                try:
                    return PolicyAction(rule.action)
                except ValueError:
                    continue
        return PolicyAction.ALLOW  # Default: permissive

    def get_reason(self, tool_name: str, tool_args: dict) -> str:
        """Get the reason string for the matching rule, if any."""
        for rule in self.rules:
            if rule.matches(tool_name, tool_args):
                return rule.reason
        return ""

    @classmethod
    def from_rules(cls, rules: list[dict]) -> "ExecutionPolicy":
        return cls([PolicyRule.from_dict(r) for r in rules])

    @classmethod
    def from_file(cls, path: str | Path) -> Optional["ExecutionPolicy"]:
        """Load policy from a resonant-policy.json file."""
        p = Path(path)
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            rules = data.get("rules", [])
            return cls.from_rules(rules)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load policy from %s: %s", path, e)
            return None

    def merge(self, other: "ExecutionPolicy") -> "ExecutionPolicy":
        """Merge another policy (other's rules take precedence by being checked first)."""
        return ExecutionPolicy(other.rules + self.rules)


# ── Built-in tier policies ──────────────────────────────────────

def default_suggest_policy() -> ExecutionPolicy:
    """Suggest mode: deny all writes, allow reads."""
    return ExecutionPolicy([
        PolicyRule(tool_pattern="file_read", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="glob", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="grep", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="file_write", action="deny", reason="Write operations blocked in suggest mode"),
        PolicyRule(tool_pattern="file_edit", action="deny", reason="Write operations blocked in suggest mode"),
        PolicyRule(tool_pattern="bash", action="deny", reason="Shell commands blocked in suggest mode"),
        PolicyRule(tool_pattern="batch", action="deny", reason="Shell commands blocked in suggest mode"),
    ])


def default_auto_edit_policy() -> ExecutionPolicy:
    """Auto-edit mode: allow file writes, prompt for dangerous shell commands."""
    return ExecutionPolicy([
        PolicyRule(tool_pattern="file_*", action="allow", reason="File operations allowed in auto-edit mode"),
        PolicyRule(tool_pattern="glob", action="allow"),
        PolicyRule(tool_pattern="grep", action="allow"),
        # Dangerous bash patterns require prompting
        PolicyRule(
            tool_pattern="bash",
            action="deny",
            arg_patterns={"command": r"rm\s+(-rf?|--recursive)"},
            reason="Recursive delete blocked — use a safer alternative",
        ),
        PolicyRule(
            tool_pattern="bash",
            action="deny",
            arg_patterns={"command": r"chmod\s+[0-7]{3,4}\s+/"},
            reason="System permission changes blocked",
        ),
        PolicyRule(
            tool_pattern="bash",
            action="deny",
            arg_patterns={"command": r"curl.*\|\s*(sh|bash)"},
            reason="Piping remote scripts to shell blocked",
        ),
        PolicyRule(
            tool_pattern="bash",
            action="prompt",
            reason="Shell commands require approval in auto-edit mode",
        ),
    ])


def default_full_auto_policy() -> ExecutionPolicy:
    """Full-auto mode: allow everything (sandbox handles safety)."""
    return ExecutionPolicy([
        PolicyRule(tool_pattern="*", action="allow", reason="Full autonomy (sandboxed)"),
    ])


def policy_for_tier(tier: str) -> ExecutionPolicy:
    """Get the default policy for an autonomy tier."""
    policies = {
        "suggest": default_suggest_policy,
        "auto-edit": default_auto_edit_policy,
        "full-auto": default_full_auto_policy,
    }
    factory = policies.get(tier, default_auto_edit_policy)
    return factory()

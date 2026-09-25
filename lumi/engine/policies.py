"""
Declarative Execution Policies for Lumi Sessions.

Defines rules that control which tools are allowed, prompted, or denied.
Inspired by Codex CLI's Starlark rule system, simplified to JSON.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The source of rules read from a project's lumi-policy.json. A trusted
# repository's allow rule is the only allow that lets Auto-edit run a call
# without asking (ExecutionPolicy.repository_allows).
REPOSITORY = "repository"


class PolicyAction(str, Enum):
    ALLOW = "allow"
    PROMPT = "prompt"
    DENY = "deny"


# What chains, substitutes or redirects commands in sh and cmd.exe. A rule's
# glob or pattern matches the whole command text, so "npm test*" also matches
# "npm test && curl … | sh"; commands like that keep asking.
_SHELL_JOINERS = re.compile(r"[;&|<>`\r\n]|\$\(")


def _single_command(tool_name: str, tool_args: dict) -> bool:
    """Whether a call runs one command, with nothing chained, substituted or redirected.

    Shell tools (``bash``, ``check_run``) are checked as text, and program
    tools (``job_start``, ``preview_start``) word by word, since
    ``["sh", "-c", ...]`` carries a whole command. Other tools run no command.
    """
    from .guardrails import ARGV_TOOLS, SHELL_TOOLS

    command = tool_args.get("command", "")
    if tool_name in SHELL_TOOLS:
        return not _SHELL_JOINERS.search(str(command))
    if tool_name in ARGV_TOOLS:
        words = command if isinstance(command, list) else [command]
        return not any(_SHELL_JOINERS.search(str(word)) for word in words)
    return True


@dataclass
class PolicyRule:
    """A single rule in an execution policy."""

    tool_pattern: str  # Glob pattern matching tool name (e.g., "bash", "file_*", "*")
    action: str = "allow"  # allow | prompt | deny
    arg_patterns: dict[str, str] = field(default_factory=dict)  # Regex patterns for args
    arg_globs: dict[str, str | list[str]] = field(default_factory=dict)
    reason: str = ""  # Human-readable explanation
    # REPOSITORY for a project's own rules, "" for built-in and organization
    # rules. Set by whoever loads the rule, never read from the rule itself,
    # so a policy file can't claim another layer's standing.
    source: str = ""

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

        # Friendlier command policies can use shell-like globs instead of
        # embedding regular expressions in lumi-policy.json.  All listed
        # argument constraints must match; a list means any glob may match.
        for arg_key, patterns in self.arg_globs.items():
            arg_value = str(tool_args.get(arg_key, "")).lower()
            choices = [patterns] if isinstance(patterns, str) else patterns
            if not isinstance(choices, list) or not any(
                fnmatch.fnmatchcase(arg_value, str(pattern).lower())
                for pattern in choices
            ):
                return False

        return True

    @classmethod
    def from_dict(cls, data: dict, *, source: str = "") -> "PolicyRule":
        return cls(
            tool_pattern=data.get("tool_pattern", "*"),
            action=data.get("action", "allow"),
            arg_patterns=data.get("arg_patterns", {}),
            arg_globs=data.get("arg_globs", {}),
            reason=data.get("reason", ""),
            source=source,
        )

    def decides(self) -> bool:
        """Whether the rule's action is one the policy acts on; others are skipped."""
        try:
            PolicyAction(self.action)
        except ValueError:
            return False
        return True


class ExecutionPolicy:
    """
    Ordered set of rules that evaluate tool calls.

    First matching rule wins. No match = ALLOW (default permissive).
    """

    def __init__(self, rules: Optional[list[PolicyRule]] = None):
        self.rules = rules or []
        # SHA-256 of the file the rules were read from (from_file), else "".
        self.digest = ""

    def first_match(self, tool_name: str, tool_args: dict, *, source: Optional[str] = None) -> Optional[PolicyRule]:
        """The rule that decides a tool call, or None; with ``source``, among that layer's rules only."""
        for rule in self.rules:
            if source is not None and rule.source != source:
                continue
            if rule.matches(tool_name, tool_args) and rule.decides():
                return rule
        return None

    def evaluate(self, tool_name: str, tool_args: dict) -> PolicyAction:
        """Evaluate a tool call against the policy. Returns the action to take."""
        rule = self.first_match(tool_name, tool_args)
        return PolicyAction(rule.action) if rule else PolicyAction.ALLOW  # Default: permissive

    def repository_allows(self, tool_name: str, tool_args: dict) -> bool:
        """Whether a repository's own rules let a call run without asking.

        This is what lets Auto-edit skip its prompt
        (Session._resolve_tool_permission). The policy as a whole must allow
        the call, so the guardrails, organization rules and the tier's
        denies decide first. Then the repository's first matching rule must
        be ``allow``: an organization ``allow`` that matches first neither
        skips the prompt itself nor hides the repository's answer. A command
        must be a single command (_single_command). A repository's allow
        rules are only in the policy while the user trusts the project
        (project_execution_policy).
        """
        if not _single_command(tool_name, tool_args):
            return False
        if self.evaluate(tool_name, tool_args) != PolicyAction.ALLOW:
            return False
        rule = self.first_match(tool_name, tool_args, source=REPOSITORY)
        return rule is not None and rule.action == PolicyAction.ALLOW.value

    def get_reason(self, tool_name: str, tool_args: dict) -> str:
        """Get the reason string for the matching rule, if any."""
        for rule in self.rules:
            if rule.matches(tool_name, tool_args):
                return rule.reason
        return ""

    @classmethod
    def from_rules(cls, rules: list[dict], *, source: str = "") -> "ExecutionPolicy":
        return cls([PolicyRule.from_dict(r, source=source) for r in rules])

    @classmethod
    def from_file(cls, path: str | Path, *, source: str = "") -> Optional["ExecutionPolicy"]:
        """Load policy from a lumi-policy.json (or legacy resonant-policy.json) file.

        The file is read once, and ``digest`` is the SHA-256 of exactly the
        bytes the rules came from.
        """
        p = Path(path)
        if not p.exists():
            return None
        try:
            raw = p.read_bytes()
            data = json.loads(raw.decode("utf-8"))
            rules = data.get("rules", [])
            policy = cls.from_rules(rules, source=source)
        except (ValueError, OSError) as e:  # ValueError covers bad JSON and bad UTF-8
            logger.warning("Failed to load policy from %s: %s", path, e)
            return None
        policy.digest = hashlib.sha256(raw).hexdigest()
        return policy

    def merge(self, other: "ExecutionPolicy") -> "ExecutionPolicy":
        """Layer another policy, such as a repository's lumi-policy.json, over this one.

        The other policy's rules are checked before this policy's allow and
        prompt rules, so a repository can tighten or refine them. This policy's
        deny rules are checked before everything: a repository must not be able
        to weaken a built-in deny with an earlier ``allow``.
        """
        denies = [rule for rule in self.rules if rule.action == PolicyAction.DENY.value]
        defaults = [rule for rule in self.rules if rule.action != PolicyAction.DENY.value]
        return ExecutionPolicy(denies + other.rules + defaults)


# ── Built-in tier policies ──────────────────────────────────────

def default_suggest_policy() -> ExecutionPolicy:
    """Suggest mode: deny all writes, allow reads.

    The read-only tier, for runs where nobody can answer a prompt
    (``lumi run --mode ask``). The desktop's Ask mode asks instead; see
    default_ask_policy.
    """
    return ExecutionPolicy([
        PolicyRule(tool_pattern="file_read", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="glob", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="grep", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="file_write", action="deny", reason="Write operations blocked in suggest mode"),
        PolicyRule(tool_pattern="file_edit", action="deny", reason="Write operations blocked in suggest mode"),
        PolicyRule(tool_pattern="bash", action="deny", reason="Shell commands blocked in suggest mode"),
        PolicyRule(tool_pattern="batch", action="deny", reason="Shell commands blocked in suggest mode"),
    ])


def _dangerous_shell_rules() -> list[PolicyRule]:
    """Shell commands refused in the tiers that ask about the others.

    Approving a command must not run one of these; the guardrails
    (engine/guardrails.py) refuse worse ones in every tier.
    """
    return [
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
    ]


def default_ask_policy() -> ExecutionPolicy:
    """Ask mode: allow reads, ask before file changes and shell commands.

    The ask tier already asks before anything that isn't read-only
    (Session._should_auto_approve); these prompt rules say so for the
    changes the mode is named for. Unlike suggest, it refuses nothing
    outright except Auto-edit's dangerous commands (and the guardrails every
    tier starts with): approving a command in Ask must not run one that
    Auto-edit refuses.
    """
    return ExecutionPolicy([
        PolicyRule(tool_pattern="file_read", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="glob", action="allow", reason="Read-only access"),
        PolicyRule(tool_pattern="grep", action="allow", reason="Read-only access"),
        *_dangerous_shell_rules(),
        PolicyRule(tool_pattern="file_write", action="prompt", reason="Write operations require approval in ask mode"),
        PolicyRule(tool_pattern="file_edit", action="prompt", reason="Write operations require approval in ask mode"),
        PolicyRule(tool_pattern="bash", action="prompt", reason="Shell commands require approval in ask mode"),
        PolicyRule(tool_pattern="batch", action="prompt", reason="Batched calls require approval in ask mode"),
    ])


def default_auto_edit_policy() -> ExecutionPolicy:
    """Auto-edit mode: allow file writes, deny dangerous shell commands, ask about the rest."""
    return ExecutionPolicy([
        PolicyRule(tool_pattern="file_*", action="allow", reason="File operations allowed in auto-edit mode"),
        PolicyRule(tool_pattern="glob", action="allow"),
        PolicyRule(tool_pattern="grep", action="allow"),
        *_dangerous_shell_rules(),
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
    """Get the default policy for an autonomy tier.

    Every tier starts with the guardrails (engine/guardrails.py): commands
    that are never run, whatever the tier allows.
    """
    from .guardrails import policy_rules as guardrail_rules

    policies = {
        "suggest": default_suggest_policy,
        "ask": default_ask_policy,
        "auto-edit": default_auto_edit_policy,
        "full-auto": default_full_auto_policy,
    }
    factory = policies.get(tier, default_auto_edit_policy)
    return ExecutionPolicy(guardrail_rules() + factory().rules)


def project_execution_policy(
    tier: str, project_root: str, *, honor_allows: bool = True, policy_digest: Optional[str] = None,
) -> ExecutionPolicy:
    """The tier's built-in policy with the project's lumi-policy.json layered on.

    The project policy can tighten or refine the built-in rules; it cannot
    override built-in denies (see ExecutionPolicy.merge). In Auto-edit its
    ``allow`` rules also run the calls they match without asking
    (ExecutionPolicy.repository_allows), so they apply only while the user
    trusts the project and its policy hasn't changed since
    (gui/workspace_trust.py). ``policy_digest`` is the SHA-256 of the file
    that trust check read: allow rules apply only if the file read here is
    the same, so an edit in between can't slip past the check.
    Organization shell rules (lumi/policy.py) come next: neither a repository
    nor a tier can loosen them. The guardrails (engine/guardrails.py) come
    before everything, so an organization's ``allow`` can't reach them either.
    """
    policy = policy_for_tier(tier)
    # lumi-policy.json; repositories from before the rebrand keep resonant-policy.json.
    project_policy = None
    for name in ("lumi-policy.json", "resonant-policy.json"):
        candidate = os.path.join(project_root, name)
        if os.path.isfile(candidate):
            project_policy = ExecutionPolicy.from_file(candidate, source=REPOSITORY)
            break
    if project_policy and policy_digest is not None and project_policy.digest != policy_digest:
        honor_allows = False
    if project_policy and not honor_allows:
        project_policy = ExecutionPolicy(
            [rule for rule in project_policy.rules if rule.action != PolicyAction.ALLOW.value]
        )
    merged = policy.merge(project_policy) if project_policy else policy
    from ..policy import current as current_policy

    org_policy = current_policy()
    if org_policy and org_policy.shell_rules:
        org_rules = ExecutionPolicy.from_rules(list(org_policy.shell_rules)).rules
        from .guardrails import policy_rules as guardrail_rules

        first = guardrail_rules()
        merged = ExecutionPolicy(first + org_rules + [rule for rule in merged.rules if rule not in first])
    return merged

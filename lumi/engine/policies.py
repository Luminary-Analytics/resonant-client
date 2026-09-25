"""
Declarative Execution Policies for Lumi Sessions.

Defines rules that control which tools are allowed, prompted, or denied.
Inspired by Codex CLI's Starlark rule system, simplified to JSON.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
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
    arg_globs: dict[str, str | list[str]] = field(default_factory=dict)
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
    def from_dict(cls, data: dict) -> "PolicyRule":
        """A rule from JSON: a repository's lumi-policy.json or organization policy.

        Raises ValueError, saying what's wrong, for a rule that can't be
        applied as written, rather than returning one that fails later, when
        a tool call is checked against it. Keys Lumi doesn't use are ignored.
        """
        if not isinstance(data, dict):
            raise ValueError("a rule must be an object")
        tool_pattern = data.get("tool_pattern", "*")
        if not isinstance(tool_pattern, str):
            raise ValueError("tool_pattern must be a tool name or pattern")
        action = data.get("action", PolicyAction.ALLOW.value)
        if not isinstance(action, str) or action not in {item.value for item in PolicyAction}:
            raise ValueError('action must be "allow", "prompt" or "deny"')
        arg_patterns = data.get("arg_patterns", {})
        if not isinstance(arg_patterns, dict) or not all(
            isinstance(key, str) and isinstance(pattern, str) for key, pattern in arg_patterns.items()
        ):
            raise ValueError("arg_patterns must map argument names to regular expressions")
        for key, pattern in arg_patterns.items():
            try:
                re.compile(pattern)
            except re.error as exc:
                # matches() would treat it as never matching, silently.
                raise ValueError(f"arg_patterns.{key} isn't a valid regular expression ({exc})") from exc
        arg_globs = data.get("arg_globs", {})
        if not isinstance(arg_globs, dict) or not all(
            isinstance(key, str) and (
                isinstance(globs, str)
                or (isinstance(globs, list) and all(isinstance(glob, str) for glob in globs))
            )
            for key, globs in arg_globs.items()
        ):
            raise ValueError("arg_globs must map argument names to a glob or a list of globs")
        reason = data.get("reason", "")
        return cls(
            tool_pattern=tool_pattern,
            action=action,
            arg_patterns=dict(arg_patterns),
            arg_globs={key: list(globs) if isinstance(globs, list) else globs for key, globs in arg_globs.items()},
            reason="" if reason is None else str(reason),
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
        """Rules that must all be valid, such as an organization's; raises ValueError otherwise."""
        if not isinstance(rules, (list, tuple)):
            raise ValueError("rules must be a list of rule objects")
        parsed = []
        for number, rule in enumerate(rules, 1):
            try:
                parsed.append(PolicyRule.from_dict(rule))
            except ValueError as exc:
                raise ValueError(f"rule {number}: {exc}") from exc
        return cls(parsed)

    @classmethod
    def from_file(cls, path: str | Path) -> Optional["ExecutionPolicy"]:
        """Load a repository's lumi-policy.json (or legacy resonant-policy.json).

        The file comes with the repository, so a mistake in it must not cost
        more than its own rules (see repository_rules); a warning says what's
        wrong. None when there is no file or it isn't readable JSON.
        """
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError) as e:
            # ValueError: not JSON, or not UTF-8. RecursionError: nested too deeply.
            logger.warning("Ignoring %s, which isn't readable JSON: %s", path, e)
            return None
        rules, problems = repository_rules(data)
        if problems:
            logger.warning(
                "%s has mistakes: %s. Until the file is fixed, Lumi uses only its valid deny and prompt rules.",
                path, "; ".join(problems),
            )
        return cls(rules)

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


def repository_rules(data: object) -> tuple[list[PolicyRule], list[str]]:
    """The rules Lumi uses from a repository's parsed lumi-policy.json, and its mistakes.

    A file that isn't an object with a ``rules`` list contributes nothing. A
    rule that can't be applied as written is dropped, and so are the file's
    ``allow`` rules: rules apply in order and the first match wins, so without
    the broken rule a later ``allow`` could let through what that rule was
    meant to refuse or ask about. The file's valid ``deny`` and ``prompt``
    rules still apply, since they only make Lumi more careful. Fixing the
    file brings its allow rules back.
    """
    if not isinstance(data, dict):
        return [], ['the file must be a JSON object with a "rules" list']
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        return [], ['"rules" must be a list of rule objects']
    rules: list[PolicyRule] = []
    problems: list[str] = []
    for number, raw_rule in enumerate(raw_rules, 1):
        try:
            rules.append(PolicyRule.from_dict(raw_rule))
        except ValueError as exc:
            problems.append(f"rule {number}: {exc}")
    if problems:
        rules = [rule for rule in rules if rule.action != PolicyAction.ALLOW.value]
    return rules, problems


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


def project_execution_policy(tier: str, project_root: str, *, honor_allows: bool = True) -> ExecutionPolicy:
    """The tier's built-in policy with the project's lumi-policy.json layered on.

    The project policy can tighten or refine the built-in rules; it cannot
    override built-in denies (see ExecutionPolicy.merge). Its ``allow`` rules
    skip approval prompts, so they apply only while the user trusts the
    project and its policy hasn't changed since (gui/workspace_trust.py).
    Organization shell rules (lumi/policy.py) come next: neither a repository
    nor a tier can loosen them. The guardrails (engine/guardrails.py) come
    before everything, so an organization's ``allow`` can't reach them either.

    A lumi-policy.json with mistakes keeps only its valid deny and prompt
    rules (repository_rules), and can't stop the organization's rules applying.
    """
    policy = policy_for_tier(tier)
    # lumi-policy.json; repositories from before the rebrand keep resonant-policy.json.
    project_policy = None
    for name in ("lumi-policy.json", "resonant-policy.json"):
        candidate = os.path.join(project_root, name)
        if os.path.isfile(candidate):
            project_policy = ExecutionPolicy.from_file(candidate)
            break
    if project_policy and not honor_allows:
        project_policy = ExecutionPolicy(
            [rule for rule in project_policy.rules if rule.action != PolicyAction.ALLOW.value]
        )
    merged = policy.merge(project_policy) if project_policy else policy
    return with_organization_rules(merged)


def with_organization_rules(policy: ExecutionPolicy) -> ExecutionPolicy:
    """``policy`` with the organization's shell rules (lumi/policy.py) checked first.

    Only the guardrails (engine/guardrails.py) come before them. The app's
    fallback, when a project's policy can't be built, uses this too
    (gui/app.py). The rules were validated when the organization policy
    loaded, so this doesn't fail on them.
    """
    from ..policy import current as current_policy

    org_policy = current_policy()
    if not org_policy or not org_policy.shell_rules:
        return policy
    org_rules = ExecutionPolicy.from_rules(list(org_policy.shell_rules)).rules
    from .guardrails import policy_rules as guardrail_rules

    first = guardrail_rules()
    return ExecutionPolicy(first + org_rules + [rule for rule in policy.rules if rule not in first])

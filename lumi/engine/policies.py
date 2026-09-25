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
        """A rule from JSON: a repository's lumi-policy.json or organization policy.

        Raises ValueError, saying what's wrong, for a rule that can't be
        applied as written, rather than returning one that fails later, when
        a tool call is checked against it. Keys Lumi doesn't use are ignored,
        including ``source``, which only the caller sets.
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
        """Rules that must all be valid, such as an organization's; raises ValueError otherwise."""
        if not isinstance(rules, (list, tuple)):
            raise ValueError("rules must be a list of rule objects")
        parsed = []
        for number, rule in enumerate(rules, 1):
            try:
                parsed.append(PolicyRule.from_dict(rule, source=source))
            except ValueError as exc:
                raise ValueError(f"rule {number}: {exc}") from exc
        return cls(parsed)

    @classmethod
    def from_file(cls, path: str | Path, *, source: str = "") -> Optional["ExecutionPolicy"]:
        """Load a repository's lumi-policy.json (or legacy resonant-policy.json).

        The file is read once, and ``digest`` is the SHA-256 of exactly the
        bytes the rules came from. The file comes with the repository, so a
        mistake in it must not cost more than its own rules (see
        repository_rules); a warning says what's wrong. None when there is no
        file or it isn't readable JSON.
        """
        try:
            raw = Path(path).read_bytes()
            data = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError) as e:
            # ValueError: not JSON, or not UTF-8. RecursionError: nested too deeply.
            logger.warning("Ignoring %s, which isn't readable JSON: %s", path, e)
            return None
        rules, problems = repository_rules(data, source=source)
        if problems:
            logger.warning(
                "%s has mistakes: %s. Until the file is fixed, Lumi uses only its valid deny and prompt rules.",
                path, "; ".join(problems),
            )
        policy = cls(rules)
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


def repository_rules(data: object, *, source: str = "") -> tuple[list[PolicyRule], list[str]]:
    """The rules Lumi uses from a repository's parsed lumi-policy.json, and its mistakes.

    A file that isn't an object with a ``rules`` list contributes nothing. A
    rule that can't be applied as written is dropped, and so are the file's
    ``allow`` rules: rules apply in order and the first match wins, so without
    the broken rule a later ``allow`` could let through what that rule was
    meant to refuse or ask about. The file's valid ``deny`` and ``prompt``
    rules still apply, since they only make Lumi more careful. Fixing the
    file brings its allow rules back. ``source`` tags the rules
    (PolicyRule.source).
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
            rules.append(PolicyRule.from_dict(raw_rule, source=source))
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

    A lumi-policy.json with mistakes keeps only its valid deny and prompt
    rules (repository_rules), and can't stop the organization's rules applying.
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

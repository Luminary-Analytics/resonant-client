"""Security flags: what in a turn an organization's security team may want to see.

Organization oversight (lumi/oversight.py) raises these when the policy turns
``security_flags`` on. Detection runs on this computer and is deterministic:
the engine's own verdicts on tool calls and fixed patterns, never a model.

========================  ========  ==============================================
Kind                      Severity  When
========================  ========  ==============================================
``destructive_command``   high      a command the guardrails never run
                                    (engine/guardrails.py)
``dangerous_command``     medium    a command the permission mode refuses (a
                                    download piped to a shell, a recursive delete)
``policy_denied``         medium    the organization's shell rules refused a call;
                          low       the project's lumi-policy.json, a hook or the
                                    review gate did
``excluded_file``         medium    the agent tried to read, write or search a
                                    file Lumi never reads (engine/exclusions.py)
``outside_project``       low       a path outside the project (not named)
``approval_denied``       low       the person or their permission hook said no;
                          medium    a second person's approval was declined or
                                    never came (engine/second_approval.py)
``secret_redacted``       medium    a secret was removed from what went to the
                          high      model or from what was shared (high for private
                                    keys and cloud secrets)
``prompt_injection``      medium    tool output that tries to instruct the agent:
                          low       medium from web pages, MCP servers, issues and
                                    pull requests, low from local files and commands
========================  ========  ==============================================

A flag's ``rule`` is a label from ``RULES``, a closed set, never free text:
Lumi Cloud stores it in the clear, shows it to everyone who sees oversight
and sends it to SIEM, webhook, email and Slack destinations. A hook's reason,
a policy rule's words and an exclusion pattern (which can be a file's name)
never go in it. What a person or a program wrote goes only in the excerpt:
short, secrets removed and the organization's DLP rules applied (lumi/dlp.py
``shareable``, on the whole text before a window is cut), shared only when
the organization also receives messages (``oversight.messages`` not
``off``), and never for excluded files or paths outside the project.
"""

from __future__ import annotations

import bisect
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

SEVERITIES = ("low", "medium", "high")
KINDS = ("destructive_command", "dangerous_command", "policy_denied", "excluded_file", "outside_project",
         "approval_denied", "secret_redacted", "prompt_injection")
# Every label a flag's ``rule`` can be, and what it says. Lumi Cloud keeps the
# same list (lumi_cloud/oversight.py RULES) and stores any other value as "other".
RULES: dict[str, str] = {
    # destructive_command: the guardrails (engine/guardrails.py)
    "delete_everything": "Deleting the whole file system, a drive or the home folder",
    "permissions_everything": "Changing permissions on the whole file system",
    "disk_format": "Formatting or partitioning a disk",
    "disk_overwrite": "Writing raw data over a disk",
    "fork_bomb": "A fork bomb",
    "shutdown": "Shutting down or restarting the computer",
    "guardrail": "A command Lumi never runs",
    # dangerous_command: the permission mode's refusals (engine/policies._dangerous_shell_rules)
    "recursive_delete": "A recursive delete",
    "system_permissions": "Changing system permissions",
    "remote_script": "A download piped to a shell",
    "risky_command": "A command the permission mode refuses",
    # policy_denied
    "organization_rule": "The organization's shell rules",
    "project_rule": "The project's lumi-policy.json",
    "review_gate": "Agent changes wait for review",
    "hook_denied": "A hook refused it",
    # excluded_file (the file and the pattern are never named)
    "excluded_by_organization": "A file the organization's policy excludes",
    "excluded_file": "A file excluded from Lumi",
    # outside_project
    "outside_project": "A path outside the project",
    # approval_denied
    "declined_by_person": "Declined by the person",
    "declined_by_hook": "Declined by a permission hook",
    "second_approval_declined": "A second person declined",
    "second_approval_expired": "No second person answered in time",
    "second_approval_unavailable": "Lumi Cloud couldn't ask for a second approval",
    "second_approval_stopped": "Stopped while waiting for a second approval",
    # secret_redacted
    "secret_removed": "Secrets were removed",
    # prompt_injection
    "ignore_instructions": "Asks to ignore earlier instructions",
    "new_identity": "Tells the agent it is someone else now",
    "new_instructions": "Gives the agent new instructions",
    "fake_role_marker": "Imitates a system or chat role marker",
    "hide_from_person": "Asks to hide something from the person",
    "send_credentials": "Asks to send credentials somewhere",
    "html_comment_instructions": "Instructions hidden in an HTML comment",
    "unicode_tags": "Hidden Unicode tag characters",
    "invisible_characters": "Invisible or direction-changing characters",
}
# The label a flag of each kind falls back to.
_KIND_RULES = {"destructive_command": "guardrail", "dangerous_command": "risky_command",
               "policy_denied": "organization_rule", "excluded_file": "excluded_file",
               "outside_project": "outside_project", "approval_denied": "declined_by_person",
               "secret_redacted": "secret_removed", "prompt_injection": "ignore_instructions"}
EXCERPT_LIMIT = 200
# Tool output past this is not searched for injection (a long log or page is
# still covered where instructions usually sit: its start).
SCAN_LIMIT = 200_000
# How many characters an excerpt keeps on each side of what matched.
_CONTEXT = 60
# The person's own "no" (Session._resolve_tool_permission); the app and the
# terminal UI match the same text.
USER_DENIAL_OUTPUT = "Tool execution denied by user."
# Secret formats whose exposure is worst: whoever holds them has the account.
HIGH_SECRETS = frozenset({"private key", "AWS secret key", "Stripe key", "Azure storage key"})
# The guardrails' reasons (engine/guardrails.GUARDRAILS) and the permission
# modes' (engine/policies._dangerous_shell_rules), as labels.
_GUARDRAIL_RULES = {
    "Deleting the whole file system or your home folder": "delete_everything",
    "Deleting a whole drive": "delete_everything",
    "Deleting a whole drive or your home folder": "delete_everything",
    "Changing permissions on the whole file system": "permissions_everything",
    "Formatting a disk": "disk_format",
    "Formatting a drive": "disk_format",
    "Changing disk partitions": "disk_format",
    "Writing raw data over a disk": "disk_overwrite",
    "A fork bomb": "fork_bomb",
    "Shutting down or restarting the computer": "shutdown",
}
_DANGEROUS_RULES = {
    "Recursive delete blocked — use a safer alternative": "recursive_delete",
    "System permission changes blocked": "system_permissions",
    "Piping remote scripts to shell blocked": "remote_script",
}
_SECOND_APPROVAL_RULES = {"denied": "second_approval_declined", "expired": "second_approval_expired",
                          "unavailable": "second_approval_unavailable", "cancelled": "second_approval_stopped"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rule_text(rule: str) -> str:
    """What a rule label says, for people (Settings, and Lumi Cloud's pages)."""
    return RULES.get(str(rule or ""), "Another rule")


def label(kind: str, rule: str) -> str:
    """``rule`` if it is a label from RULES, else the kind's own: never free text."""
    return rule if rule in RULES else _KIND_RULES.get(kind, "guardrail")


@dataclass
class Flag:
    """One security flag, as the member sees it and Lumi Cloud receives it."""

    kind: str
    severity: str
    rule: str
    tool: str = ""
    excerpt: str = ""
    worker: bool = False
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    at: str = field(default_factory=_now)

    def key(self) -> tuple[str, str, str]:
        """Flags with the same key in one turn are one flag."""
        return (self.kind, self.rule, self.tool)

    def to_dict(self) -> dict:
        return asdict(self)


def clip(text: Any, limit: int = EXCERPT_LIMIT) -> str:
    """One line, secrets removed (secret_scan.redact_for_sharing), then the organization's DLP rules
    applied (dlp.shareable: every rule; '' when they withhold it), invisible characters shown, at most
    ``limit``."""
    from . import dlp
    from .secret_scan import redact_for_sharing

    value, _ = redact_for_sharing(str(text or ""))
    value = dlp.shareable(value) or ""
    value = _INVISIBLE.sub(lambda match: f"<U+{ord(match.group(0)):04X}>", value)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _flag(kind: str, severity: str, rule: str, *, tool: str = "", excerpt: str = "", worker: bool = False) -> Flag:
    # Only a label from RULES: anything else would carry free text to Lumi Cloud.
    return Flag(kind=kind, severity=severity, rule=label(kind, rule), tool=str(tool or "")[:80],
                excerpt=clip(excerpt), worker=bool(worker))


# ── Refused tool calls ──────────────────────────────────────────────────────


def _command_text(arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return ""
    command = arguments.get("command")
    if isinstance(command, list):
        return " ".join(str(word) for word in command)
    return str(command or "")


def _after(prefix: str, text: str) -> str:
    return text[len(prefix):].strip() if text.startswith(prefix) else ""


def for_denial(name: str, arguments: Any, event: dict, *, worker: bool = False) -> Flag | None:
    """The flag for a refused tool call (a ``tool.result`` with ``denied``), or None.

    ``denied_by`` on the event says which layer refused it (the engine sets
    it where it refuses); older events are read by their output. The rule is
    a label: the reason a hook, a policy rule or an exclusion gave stays on
    this computer, and the excerpt is the command the agent tried.
    """
    output = str(event.get("output") or "")
    source = str(event.get("denied_by") or "")
    reason = str(event.get("denied_rule") or "")
    command = _command_text(arguments)
    if not source:
        from .engine.guardrails import blocked_call

        if output.startswith("Blocked by policy:") and blocked_call(name, arguments if isinstance(arguments, dict)
                                                                     else {}):
            source = "guardrail"
        elif output.startswith("Blocked by policy:"):
            source = "rule"
        elif output.startswith("Blocked by hook:"):
            source = "hook"
        elif output == USER_DENIAL_OUTPUT:
            source = "user"
        elif "Lumi does not read, list or send excluded files" in output:
            source = "exclusion"
        else:
            return None
    policy_reason = reason or _after("Blocked by policy:", output)
    if source == "guardrail":
        from .engine.guardrails import blocked_call

        found = blocked_call(name, arguments if isinstance(arguments, dict) else {}) or policy_reason
        label = _GUARDRAIL_RULES.get(found.split(" is never allowed")[0], "guardrail")
        return _flag("destructive_command", "high", label, tool=name, excerpt=command, worker=worker)
    if source == "dangerous_command":
        return _flag("dangerous_command", "medium", _DANGEROUS_RULES.get(policy_reason, "risky_command"),
                     tool=name, excerpt=command, worker=worker)
    if source == "organization":
        return _flag("policy_denied", "medium", "organization_rule", tool=name, excerpt=command, worker=worker)
    if source in ("repository", "rule", "policy"):
        return _flag("policy_denied", "low", "project_rule", tool=name, excerpt=command, worker=worker)
    if source == "review_gate":
        return _flag("policy_denied", "low", "review_gate", tool=name, excerpt=command, worker=worker)
    if source == "hook":
        return _flag("policy_denied", "low", "hook_denied", tool=name, excerpt=command, worker=worker)
    if source == "exclusion":
        # Never the file or the pattern (a pattern can be a file's name): only whose rule it was.
        label = "excluded_by_organization" if reason == "organization policy" else "excluded_file"
        return _flag("excluded_file", "medium", label, tool=name, worker=worker)
    if source == "sandbox":
        # The path itself isn't named: it can be anywhere on the computer.
        return _flag("outside_project", "low", "outside_project", tool=name, worker=worker)
    if source == "user":
        return _flag("approval_denied", "low", "declined_by_person", tool=name, excerpt=command, worker=worker)
    if source == "permission_hook":
        return _flag("approval_denied", "low", "declined_by_hook", tool=name, excerpt=command, worker=worker)
    if source == "second_approval":
        state = reason.rsplit("(", 1)[-1].rstrip(")") if reason.endswith(")") else reason
        return _flag("approval_denied", "medium", _SECOND_APPROVAL_RULES.get(state, "second_approval_declined"),
                     tool=name, excerpt=command, worker=worker)
    return None  # the tier's own rules, an allowlist, arguments that didn't parse: not a security event


# ── Removed secrets ─────────────────────────────────────────────────────────


def for_redaction(kinds: dict, where: str) -> Flag | None:
    """A flag for secrets removed ({kind: count}); ``where`` says from what (only in the excerpt)."""
    counts = {str(kind): int(count) for kind, count in (kinds or {}).items() if int(count or 0) > 0}
    if not counts:
        return None
    total = sum(counts.values())
    names = ", ".join(sorted(counts))
    severity = "high" if set(counts) & HIGH_SECRETS else "medium"
    noun = "secret" if total == 1 else "secrets"
    return _flag("secret_redacted", severity, "secret_removed",
                 excerpt=f"Removed {total} {noun} ({names}) {where}.")


# ── Prompt injection in tool output ─────────────────────────────────────────

_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")

# (label, pattern). Each is a sign that text a tool returned is trying to give
# the agent instructions; none is proof. They are written narrowly, so
# ordinary prose ("you are now in the project root") doesn't match, and in
# linear time: tool output is text other people wrote, and a pattern that
# backtracks would hold the turn (tests/test_security_flags.py times them).
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_instructions", re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+|every\s+)?(?:of\s+)?"
        r"(?:the\s+|your\s+|my\s+|these\s+|those\s+)?(?:previous|prior|above|earlier|preceding|original|system)"
        r"\s+(?:instructions?|prompts?|directions?|rules|guidelines|messages?|context)\b")),
    ("new_identity", re.compile(
        r"(?i)\byou\s+are\s+now\s+(?:(?:an?|the|my)\s+)?(?:(?:unrestricted|unfiltered|uncensored|jailbroken|evil|"
        r"rogue|different|new|free)\b|DAN\b|in\s+(?:developer|god|jailbreak|unrestricted|debug)\s+mode\b|"
        r"acting\s+as\b|no\s+longer\b)")),
    ("new_instructions", re.compile(
        r"(?i)(?:\b(?:new|updated|real|actual|secret|hidden|additional)\s+(?:system\s+)?instructions?\s*[:\-]|"
        r"\bfrom\s+now\s+on,?\s+(?:you|the\s+(?:assistant|agent|ai|model))\s+(?:will|must|are|should)\b)")),
    # [ \t]* rather than \s*: after ^, \s crosses lines, and a run of blank
    # lines made every line start rescan the rest of the text.
    ("fake_role_marker", re.compile(
        r"(?im)(?:<\|(?:im_start|im_end|system|endoftext)\|>|^[ \t]*(?:\[[ \t]*system[ \t]*\]|<[ \t]*/?[ \t]*system"
        r"[ \t]*>|#{2,}[ \t]*system[ \t]*(?:prompt|message)?[ \t]*$))")),
    ("hide_from_person", re.compile(
        r"(?i)\b(?:do\s+not|don't|never)\s+(?:tell|inform|mention\s+(?:this\s+)?to|show\s+(?:this\s+)?to|"
        r"reveal\s+(?:this\s+)?to|alert|notify)\s+(?:the\s+)?(?:user|human|developer|operator|person)\b")),
    ("send_credentials", re.compile(
        r"(?i)\b(?:send|post|upload|exfiltrate|forward|leak|email|transmit)\s+(?:the\s+|all\s+|your\s+|any\s+|its\s+)?"
        r"(?:api[\s_-]?keys?|credentials|secrets?|tokens?|passwords?|ssh\s+keys?|private\s+keys?|\.env(?:\s+file)?|"
        r"environment\s+variables|cookies)\s+(?:to|at|via)\b")),
    ("unicode_tags", re.compile("[\U000e0000-\U000e007f]+")),
    ("invisible_characters", re.compile(
        "[‪-‮⁦-⁩]|[​-‏⁠-⁤﻿]{4,}")),
)
# An HTML comment that addresses an AI and tells it what to do. Found by
# scanning (see _html_comment_instructions), not one regex: the regex this
# replaces backtracked on "<!-- AI must" repeated without "-->" (2.4 s for
# 20 KB, 25 s for 200 KB) inside Session.run.
_AI_WORD = re.compile(r"(?i)\b(?:AI|assistant|LLM|agent|model|chatbot)s?\b")
_AI_COMMAND = re.compile(r"(?i)\b(?:must|should|need\s+to|are\s+to|ignore|instead|always|never)\b")
# How far after the word for an AI its instruction may start.
_AI_COMMAND_WITHIN = 120
# Comments examined per text; each is examined once, in order.
_MAX_COMMENTS = 10_000
# Tools whose output comes from outside the person's project: web pages,
# MCP servers, issue trackers, code hosts, the screen and the clipboard.
_EXTERNAL_PREFIXES = ("browser_", "mcp_", "github_", "issue_")
_EXTERNAL_TOOLS = frozenset({"screen_ocr", "accessibility_tree", "clipboard_read", "window_list"})


def external_tool(name: str, *, external: bool = False) -> bool:
    return external or str(name).startswith(_EXTERNAL_PREFIXES) or name in _EXTERNAL_TOOLS


def _addresses_ai(body: str) -> tuple[int, int] | None:
    """(start, end) in ``body`` of a word for an AI followed closely by an instruction, or None."""
    commands = [match.start() for match in _AI_COMMAND.finditer(body)]
    if not commands:
        return None
    for word in _AI_WORD.finditer(body):
        index = bisect.bisect_left(commands, word.end())
        if index < len(commands) and commands[index] - word.end() <= _AI_COMMAND_WITHIN:
            return word.start(), commands[index]
    return None


def _html_comment_instructions(text: str) -> tuple[int, int] | None:
    """(start, end) of an HTML comment that gives an AI instructions, or None; linear in ``text``."""
    start = text.find("<!--")
    examined = 0
    while start != -1 and examined < _MAX_COMMENTS:
        examined += 1
        close = text.find("-->", start + 4)
        if close == -1:
            return None  # nothing after here closes a comment
        if _addresses_ai(text[start + 4:close]) is not None:
            return start, close + 3
        # What follows is outside this comment; an "<!--" inside it was just text.
        start = text.find("<!--", close + 3)
    return None


def _regex_finder(pattern: re.Pattern[str]) -> Callable[[str], tuple[int, int] | None]:
    def find(text: str) -> tuple[int, int] | None:
        match = pattern.search(text)
        return match.span() if match is not None else None

    return find


# Every sign, in order: (label, find(text) -> (start, end) or None).
_FINDERS: tuple[tuple[str, Callable[[str], tuple[int, int] | None]], ...] = (
    *((label, _regex_finder(pattern)) for label, pattern in INJECTION_PATTERNS[:6]),
    ("html_comment_instructions", _html_comment_instructions),
    *((label, _regex_finder(pattern)) for label, pattern in INJECTION_PATTERNS[6:]),
)


def _signs(text: str) -> dict[str, tuple[int, int]]:
    found = {}
    for label, find in _FINDERS:
        span = find(text)
        if span is not None:
            found[label] = span
    return found


def _bounded(text: Any) -> str:
    """At most SCAN_LIMIT characters, without a token cut in half at the end.

    A token cut there (the first half of a key) no longer matches its pattern
    or value, so it would reach an excerpt as it is.
    """
    value = str(text or "")
    if len(value) <= SCAN_LIMIT:
        return value
    value = value[:SCAN_LIMIT]
    cut = max(value.rfind(" "), value.rfind("\n"), value.rfind("\t"))
    return value[:cut] if cut > 0 else ""


def injection_indicators(text: Any) -> list[tuple[str, str]]:
    """(label, excerpt) for each kind of injection sign in ``text``, once each.

    Secrets are removed from the whole of what is searched before any excerpt
    is cut from it: a window cut first could end inside a token or a saved
    key, and the part left inside would match neither its pattern nor its
    value. Most output has no sign, so it is searched as it is first.
    """
    value = _bounded(text)
    raw = _signs(value)
    if not raw:
        return []
    from . import dlp
    from .secret_scan import redact_for_sharing

    redacted, _ = redact_for_sharing(value)
    # The organization's DLP rules for tool results apply to all of it too,
    # before a window is cut; text they withhold gives no excerpt at all.
    shared = dlp.shareable(redacted, "tool_result")
    redacted = shared if shared is not None else ""
    spans = _signs(redacted)
    found = []
    for label, _find in _FINDERS:
        if label not in raw and label not in spans:
            continue
        span = spans.get(label)
        # A sign that only matched across a secret has no excerpt rather than one around the secret.
        excerpt = "" if span is None else redacted[max(0, span[0] - _CONTEXT):min(len(redacted), span[1] + _CONTEXT)]
        found.append((label, excerpt))
    return found


def for_tool_output(name: str, output: Any, *, external: bool = False, worker: bool = False) -> list[Flag]:
    """Flags for injection signs in what a tool returned."""
    severity = "medium" if external_tool(name, external=external) else "low"
    return [_flag("prompt_injection", severity, label, tool=name, excerpt=excerpt, worker=worker)
            for label, excerpt in injection_indicators(output)]

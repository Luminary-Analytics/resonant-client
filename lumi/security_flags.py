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

A flag names its rule (the guardrail, the policy rule's reason, the
exclusion pattern, the indicator), never a file's contents. Its excerpt is
short, has secrets removed, and is shared only when the organization also
receives messages (``oversight.messages`` not ``off``).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

SEVERITIES = ("low", "medium", "high")
KINDS = ("destructive_command", "dangerous_command", "policy_denied", "excluded_file", "outside_project",
         "approval_denied", "secret_redacted", "prompt_injection")
EXCERPT_LIMIT = 200
RULE_LIMIT = 200
# Tool output past this is not searched for injection (a long log or page is
# still covered where instructions usually sit: its start).
SCAN_LIMIT = 200_000
# The person's own "no" (Session._resolve_tool_permission); the app and the
# terminal UI match the same text.
USER_DENIAL_OUTPUT = "Tool execution denied by user."
# Secret formats whose exposure is worst: whoever holds them has the account.
HIGH_SECRETS = frozenset({"private key", "AWS secret key", "Stripe key", "Azure storage key"})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


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
    """One line, secrets removed, invisible characters shown, at most ``limit`` characters."""
    from .secret_scan import redact_text

    value, _ = redact_text(str(text or ""), patterns=True)
    value = _INVISIBLE.sub(lambda match: f"<U+{ord(match.group(0)):04X}>", value)
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _flag(kind: str, severity: str, rule: str, *, tool: str = "", excerpt: str = "", worker: bool = False) -> Flag:
    return Flag(kind=kind, severity=severity, rule=clip(rule, RULE_LIMIT) or kind.replace("_", " "),
                tool=str(tool or "")[:80], excerpt=clip(excerpt), worker=bool(worker))


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
    it where it refuses); older events are read by their output.
    """
    output = str(event.get("output") or "")
    source = str(event.get("denied_by") or "")
    rule = str(event.get("denied_rule") or "")
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
    policy_reason = rule or _after("Blocked by policy:", output)
    if source == "guardrail":
        from .engine.guardrails import blocked_call

        reason = blocked_call(name, arguments if isinstance(arguments, dict) else {}) or policy_reason
        return _flag("destructive_command", "high", reason.split(" is never allowed")[0], tool=name,
                     excerpt=command, worker=worker)
    if source == "dangerous_command":
        return _flag("dangerous_command", "medium", policy_reason, tool=name, excerpt=command, worker=worker)
    if source == "organization":
        return _flag("policy_denied", "medium", policy_reason or "The organization's shell rules", tool=name,
                     excerpt=command, worker=worker)
    if source in ("repository", "rule", "policy"):
        return _flag("policy_denied", "low", policy_reason or "The project's lumi-policy.json", tool=name,
                     excerpt=command, worker=worker)
    if source == "review_gate":
        return _flag("policy_denied", "low", policy_reason or "Agent changes wait for review", tool=name,
                     excerpt=command, worker=worker)
    if source == "hook":
        return _flag("policy_denied", "low", f"A hook: {_after('Blocked by hook:', output) or 'refused'}",
                     tool=name, excerpt=command, worker=worker)
    if source == "exclusion":
        return _flag("excluded_file", "medium", rule or "A file Lumi never reads", tool=name, worker=worker)
    if source == "sandbox":
        # The path itself isn't named: it can be anywhere on the computer.
        return _flag("outside_project", "low", "A path outside the project", tool=name, worker=worker)
    if source == "user":
        return _flag("approval_denied", "low", "Declined by the person", tool=name, excerpt=command, worker=worker)
    if source == "permission_hook":
        return _flag("approval_denied", "low", "Declined by a permission hook", tool=name, excerpt=command,
                     worker=worker)
    if source == "second_approval":
        return _flag("approval_denied", "medium", f"Second approval not given: {rule or 'refused'}", tool=name,
                     excerpt=command, worker=worker)
    return None  # the tier's own rules, an allowlist, arguments that didn't parse: not a security event


# ── Removed secrets ─────────────────────────────────────────────────────────


def for_redaction(kinds: dict, where: str) -> Flag | None:
    """A flag for secrets removed ({kind: count}); ``where`` says from what."""
    counts = {str(kind): int(count) for kind, count in (kinds or {}).items() if int(count or 0) > 0}
    if not counts:
        return None
    total = sum(counts.values())
    names = ", ".join(sorted(counts))
    severity = "high" if set(counts) & HIGH_SECRETS else "medium"
    noun = "secret" if total == 1 else "secrets"
    return _flag("secret_redacted", severity, f"{names} ({where})",
                 excerpt=f"Removed {total} {noun} ({names}) {where}.")


# ── Prompt injection in tool output ─────────────────────────────────────────

_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]")

# (rule, pattern). Each is a sign that text a tool returned is trying to give
# the agent instructions; none is proof. They are written narrowly, so
# ordinary prose ("you are now in the project root") doesn't match.
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Asks to ignore earlier instructions", re.compile(
        r"(?i)\b(?:ignore|disregard|forget|override|bypass)\s+(?:all\s+|any\s+|every\s+)?(?:of\s+)?"
        r"(?:the\s+|your\s+|my\s+|these\s+|those\s+)?(?:previous|prior|above|earlier|preceding|original|system)"
        r"\s+(?:instructions?|prompts?|directions?|rules|guidelines|messages?|context)\b")),
    ("Tells the agent it is someone else now", re.compile(
        r"(?i)\byou\s+are\s+now\s+(?:(?:an?|the|my)\s+)?(?:(?:unrestricted|unfiltered|uncensored|jailbroken|evil|"
        r"rogue|different|new|free)\b|DAN\b|in\s+(?:developer|god|jailbreak|unrestricted|debug)\s+mode\b|"
        r"acting\s+as\b|no\s+longer\b)")),
    ("Gives the agent new instructions", re.compile(
        r"(?i)(?:\b(?:new|updated|real|actual|secret|hidden|additional)\s+(?:system\s+)?instructions?\s*[:\-]|"
        r"\bfrom\s+now\s+on,?\s+(?:you|the\s+(?:assistant|agent|ai|model))\s+(?:will|must|are|should)\b)")),
    ("Imitates a system or chat role marker", re.compile(
        r"(?im)(?:<\|(?:im_start|im_end|system|endoftext)\|>|^\s*(?:\[\s*system\s*\]|<\s*/?\s*system\s*>|"
        r"#{2,}\s*system\s*(?:prompt|message)?\s*$))")),
    ("Asks to hide something from the person", re.compile(
        r"(?i)\b(?:do\s+not|don't|never)\s+(?:tell|inform|mention\s+(?:this\s+)?to|show\s+(?:this\s+)?to|"
        r"reveal\s+(?:this\s+)?to|alert|notify)\s+(?:the\s+)?(?:user|human|developer|operator|person)\b")),
    ("Asks to send credentials somewhere", re.compile(
        r"(?i)\b(?:send|post|upload|exfiltrate|forward|leak|email|transmit)\s+(?:the\s+|all\s+|your\s+|any\s+|its\s+)?"
        r"(?:api[\s_-]?keys?|credentials|secrets?|tokens?|passwords?|ssh\s+keys?|private\s+keys?|\.env(?:\s+file)?|"
        r"environment\s+variables|cookies)\s+(?:to|at|via)\b")),
    ("Instructions hidden in an HTML comment", re.compile(
        r"(?is)<!--(?:(?!-->).){0,400}?\b(?:AI|assistant|LLM|agent|model|chatbot)s?\b(?:(?!-->).){0,120}?"
        r"\b(?:must|should|need\s+to|are\s+to|ignore|instead|always|never)\b(?:(?!-->).){0,400}?-->")),
    ("Hidden Unicode tag characters", re.compile("[\U000e0000-\U000e007f]+")),
    ("Invisible or direction-changing characters", re.compile(
        "[\u202a-\u202e\u2066-\u2069]|[\u200b-\u200f\u2060-\u2064\ufeff]{4,}")),
)
# Tools whose output comes from outside the person's project: web pages,
# MCP servers, issue trackers, code hosts, the screen and the clipboard.
_EXTERNAL_PREFIXES = ("browser_", "mcp_", "github_", "issue_")
_EXTERNAL_TOOLS = frozenset({"screen_ocr", "accessibility_tree", "clipboard_read", "window_list"})


def external_tool(name: str, *, external: bool = False) -> bool:
    return external or str(name).startswith(_EXTERNAL_PREFIXES) or name in _EXTERNAL_TOOLS


def injection_indicators(text: Any) -> list[tuple[str, str]]:
    """(rule, excerpt) for each kind of injection sign in ``text``, once each."""
    value = str(text or "")[:SCAN_LIMIT]
    found: list[tuple[str, str]] = []
    for rule, pattern in INJECTION_PATTERNS:
        match = pattern.search(value)
        if match is None:
            continue
        start, end = max(0, match.start() - 60), min(len(value), match.end() + 60)
        found.append((rule, value[start:end]))
    return found


def for_tool_output(name: str, output: Any, *, external: bool = False, worker: bool = False) -> list[Flag]:
    """Flags for injection signs in what a tool returned."""
    severity = "medium" if external_tool(name, external=external) else "low"
    return [_flag("prompt_injection", severity, rule, tool=name, excerpt=excerpt, worker=worker)
            for rule, excerpt in injection_indicators(output)]

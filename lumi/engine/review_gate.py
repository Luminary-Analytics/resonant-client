"""Agent changes wait for a named reviewer (Settings > Code review; an organization can lock it).

With ``review.agent_changes`` on:

- **The agent doesn't merge or push to a default branch.** Deny rules for
  ``gh pr merge``, ``glab mr merge``, completing an Azure DevOps pull request
  and ``git push`` naming main, master, trunk or production come right after
  the guardrails in every execution policy (engine/policies.py), so no tier,
  repository or organization ``allow`` reaches past them. Like the
  guardrails, they catch the command as written, not one built to hide.
- **A pull request the agent opens names its reviewers.** Its description
  says an AI agent made the change and who must review it; on GitHub, Lumi
  also requests review from ``review.reviewers`` (people, or ``org/team``).
- **It joins the organization's review queue** in Lumi Cloud when the person
  is signed in (``set_registrar``); ``github_pr_view`` reports its state
  there as reviews come in.

Merging stays with people: the forge's branch protection is what requires
the approval before a merge.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable

from ..paths import state_home

_settings: Any = None
_registrar: Any = None
_lock = threading.Lock()
REVIEWS_FILE = "reviews.json"

_START = r"(?:^|[;&|(]|\$\()\s*(?:sudo\s+)?"
_DEFAULT = r"(?:main|master|trunk|production)"
RULES: tuple[tuple[str, str], ...] = (
    (_START + r"gh\s+pr\s+merge\b", "Merging a pull request"),
    (_START + r"glab\s+mr\s+merge\b", "Merging a merge request"),
    (_START + r"az\s+repos\s+pr\s+update\b[^;&|]*--(status\s+completed|auto-complete\s+true)", "Completing a pull request"),
    (_START + r"git\s+push\b[^;&|]*\s(?:\S+:)?(?:refs/heads/)?" + _DEFAULT + r"['\"]?(?=$|[\s;&|)])",
     "Pushing to the default branch"),
)
_COMPILED = tuple((re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in RULES)


def configure(settings: Any) -> None:
    """Read the settings at use, so a change (or an organization's lock) applies at once."""
    global _settings
    _settings = settings


def set_registrar(registrar: Any) -> None:
    """Where agent pull requests are reported: ``register(pr) -> id`` and ``update(id, status)``, or None."""
    global _registrar
    _registrar = registrar


def enabled() -> bool:
    return bool(_settings is not None and _settings.get("review", "agent_changes", False))


def reviewers() -> list[str]:
    raw = _settings.get("review", "reviewers", []) if _settings is not None else []
    names = raw if isinstance(raw, list) else re.split(r"[,\s]+", str(raw or ""))
    return [name.strip().lstrip("@") for name in names if re.fullmatch(r"@?[A-Za-z0-9][\w.-]*(/[\w.-]+)?", str(name).strip())]


def refusal(reason: str) -> str:
    who = ", ".join(f"@{name}" for name in reviewers()) or "a named reviewer"
    return (f"{reason} is left to people: agent changes here wait for review by {who}. Open or update a pull "
            "request instead, and say what's ready for review.")


def blocked(command: str) -> str:
    """Why a shell command is refused while agent changes need review, or ""."""
    if not enabled():
        return ""
    for pattern, reason in _COMPILED:
        if pattern.search(str(command or "")):
            return reason
    return ""


def policy_rules() -> list:
    """The deny rules for an execution policy, while agent changes need review."""
    if not enabled():
        return []
    from .guardrails import SHELL_TOOLS
    from .policies import PolicyRule

    return [PolicyRule(tool_pattern=tool, action="deny", arg_patterns={"command": pattern}, reason=refusal(reason))
            for tool in SHELL_TOOLS for pattern, reason in RULES]


def pr_body(body: str) -> str:
    """The description, with a line saying an agent made the change and who reviews it."""
    if not enabled():
        return body
    who = ", ".join(f"@{name}" for name in reviewers()) or "a named reviewer"
    note = f"This change was made by an AI agent (Lumi). It needs review by {who} before it merges."
    return f"{body.rstrip()}\n\n---\n{note}" if body.strip() else note


# ── After opening a pull request ─────────────────────────────────────────────


def _remembered() -> dict:
    try:
        data = json.loads((state_home() / REVIEWS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _remember(url: str, review_id: str) -> None:
    with _lock:
        data = _remembered()
        data[url] = review_id
        path = state_home() / REVIEWS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def after_create(cwd: str, pr: dict, title: str, *, request: Callable[[list[str], list[str]], None] | None) -> str:
    """Request the reviewers (``request`` on GitHub) and report the pull request; a line for the model."""
    if not enabled():
        return ""
    names = reviewers()
    lines = []
    if names and request is not None:
        people = [name for name in names if "/" not in name]
        teams = [name.split("/", 1)[1] for name in names if "/" in name]
        try:
            request(people, teams)
            lines.append("Requested review from " + ", ".join(f"@{name}" for name in names) + ".")
        except Exception as exc:  # noqa: BLE001 - the pull request exists; say what didn't work
            lines.append(f"Couldn't request review from {', '.join(names)}: {exc}")
    elif names:
        lines.append("Ask " + ", ".join(names) + " to review it; Lumi requests reviewers only on GitHub.")
    registrar = _registrar
    if registrar is not None and pr.get("url"):
        from ..team_library import repository_of

        try:
            review_id = registrar.register({"repository": repository_of(cwd), "number": pr.get("number"),
                                            "url": pr["url"], "title": title, "reviewers": names})
            if review_id:
                _remember(pr["url"], str(review_id))
                lines.append("It's in your organization's review queue in Lumi Cloud.")
        except Exception:  # noqa: BLE001 - the queue is a convenience; the pull request stands
            pass
    lines.append("It waits for review before it merges: don't merge it or push to the default branch yourself.")
    return " ".join(lines)


def report(url: str, status: str) -> None:
    """Tell the review queue a pull request's state (waiting, approved, changes_requested, merged or closed)."""
    registrar, review_id = _registrar, _remembered().get(url)
    if registrar is None or not review_id:
        return
    try:
        registrar.update(review_id, status)
    except Exception:  # noqa: BLE001 - best effort, like registering
        pass

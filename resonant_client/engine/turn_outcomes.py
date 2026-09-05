"""Deterministic completion classification for interactive agent turns."""

from __future__ import annotations

import re
import os
import hashlib
from pathlib import Path
from typing import Iterable


WRITE_TOOL_NAMES = frozenset({"file_edit", "file_write", "file_replace"})
VALIDATION_TOOL_NAMES = frozenset({
    "check_run",
})


def normalized_changed_files(values, project_path=""):
    root = Path(project_path or os.getcwd()).resolve()
    result = {}
    for value in values:
        path = Path(value)
        path = (root / path).resolve() if not path.is_absolute() else path.resolve()
        key = os.path.normcase(str(path))
        try:
            label = path.relative_to(root).as_posix()
        except ValueError:
            label = path.as_posix()
        result.setdefault(key, label)
    return list(result.values())


def file_fingerprints(values, project_path=""):
    result = {}
    for value in normalized_changed_files(values, project_path):
        path = Path(project_path or os.getcwd()) / value
        try:
            result[value] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            result[value] = None
    return result


def current_checks(records, files):
    """Latest result of each named command; unrelated success never hides failure."""
    latest = {}
    for record in records:
        item = dict(record)
        if item.get("files") != files:
            item["status"] = "stale" if item.get("status") == "passed" else item.get("status")
        latest[(item.get("requirement"), item.get("command"))] = item
    return list(latest.values())

_CHANGE_REQUEST = re.compile(
    r"\b(?:add|build|change|clean\s*up|convert|create|delete|edit|fix|implement|"
    r"install|migrate|modify|move|patch|refactor|remove|rename|replace|rewrite|"
    r"ship|update|upgrade|wire)\b",
    re.IGNORECASE,
)
_WORKSPACE_CHANGE_PROHIBITION = re.compile(
    r"(?:\b(?:do\s+not|don't|never)\b"
    r"(?=[^.!?\n]{0,120}\b(?:configure|install|edit|modify|change|write|touch)\b)"
    r"[^.!?\n]{0,120}\b(?:anything|files?|the\s+(?:workspace|repo(?:sitory)?|project|codebase))\b|"
    r"\b(?:do\s+not|don't|never)\s+(?:edit|modify|change|write|touch)\s+"
    r"(?:any\s+)?(?:files?|anything|the\s+(?:workspace|repo(?:sitory)?|project|codebase))\b|"
    r"\bwithout\s+(?:editing|modifying|changing|writing|touching)\s+"
    r"(?:any\s+)?(?:files?|anything|the\s+(?:workspace|repo(?:sitory)?|project|codebase))\b)",
    re.IGNORECASE,
)
_ACTION_PROMISE = re.compile(
    r"\b(?:i(?:'ll|\s+will|\s+am\s+going\s+to)|let\s+me|next,?\s+i(?:'ll|\s+will)|"
    r"i\s+need\s+to)\b",
    re.IGNORECASE,
)
_PROMISED_ACTION = re.compile(
    r"\b(?:apply|build|change|check|clean|convert|create|edit|fix|implement|modify|"
    r"patch|refactor|remove|replace|rewrite|run|test|update|validate|verify)\b",
    re.IGNORECASE,
)
_NO_CHANGE_NEEDED = re.compile(
    r"\b(?:already\s+(?:fixed|implemented|converted|correct|up[- ]to[- ]date)|"
    r"no\s+changes?\s+(?:are\s+)?needed|nothing\s+to\s+change|"
    r"does\s+not\s+require\s+(?:a\s+)?change)\b",
    re.IGNORECASE,
)


def request_requires_workspace_change(text: str) -> bool:
    """Return whether the request asks the agent to mutate the workspace."""
    value = str(text or "")
    if _WORKSPACE_CHANGE_PROHIBITION.search(value):
        return False
    return bool(_CHANGE_REQUEST.search(value))


def response_promises_future_action(text: str) -> bool:
    """Detect a response that promises work but does not itself prove action."""
    value = str(text or "").strip()
    return bool(value and _ACTION_PROMISE.search(value) and _PROMISED_ACTION.search(value))


def response_says_no_change_needed(text: str) -> bool:
    return bool(_NO_CHANGE_NEEDED.search(str(text or "")))


def unique_strings(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value) for value in values if str(value or "").strip()))


def classify_turn_outcome(
    *,
    user_request: str,
    assistant_text: str,
    changed_files: Iterable[str] = (),
    validation_tools: Iterable[str] = (),
    checks: Iterable[dict] = (),
    successful_tools: Iterable[str] = (),
    terminal_error: str = "",
    needs_input: bool = False,
) -> str:
    """Return a stable outcome id consumed by every client surface."""
    changed = unique_strings(changed_files)
    validations = list(checks)
    successes = unique_strings(successful_tools)
    visible_text = bool(str(assistant_text or "").strip())

    if terminal_error:
        return "failed"
    if needs_input:
        return "needs_input"
    if changed:
        return "changed_verified" if validations and all(c.get("status") == "passed" for c in validations) else "changed_unverified"
    if request_requires_workspace_change(user_request):
        if visible_text and response_says_no_change_needed(assistant_text):
            return "no_changes_needed"
        return "incomplete"
    if visible_text:
        return "answered"
    if successes:
        return "incomplete"
    return "failed"

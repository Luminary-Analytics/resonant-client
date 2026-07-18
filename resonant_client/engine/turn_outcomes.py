"""Deterministic completion classification for interactive agent turns."""

from __future__ import annotations

import re
from typing import Iterable


WRITE_TOOL_NAMES = frozenset({"file_edit", "file_write", "file_replace"})
VALIDATION_TOOL_NAMES = frozenset({
    "bash",
    "batch",
    "computer_screenshot",
})

_CHANGE_REQUEST = re.compile(
    r"\b(?:add|build|change|clean\s*up|convert|create|delete|edit|fix|implement|"
    r"install|migrate|modify|move|patch|refactor|remove|rename|replace|rewrite|"
    r"ship|update|upgrade|wire)\b",
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
    return bool(_CHANGE_REQUEST.search(str(text or "")))


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
    successful_tools: Iterable[str] = (),
    terminal_error: str = "",
    needs_input: bool = False,
) -> str:
    """Return a stable outcome id consumed by every client surface."""
    changed = unique_strings(changed_files)
    validations = unique_strings(validation_tools)
    successes = unique_strings(successful_tools)
    visible_text = bool(str(assistant_text or "").strip())

    if terminal_error:
        return "failed"
    if needs_input:
        return "needs_input"
    if changed:
        return "changed_verified" if validations else "changed_unverified"
    if request_requires_workspace_change(user_request):
        if visible_text and response_says_no_change_needed(assistant_text):
            return "no_changes_needed"
        return "incomplete"
    if visible_text:
        return "answered"
    if successes:
        return "incomplete"
    return "failed"

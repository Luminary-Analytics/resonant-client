"""Reliable text-edit matching for model-generated file edits."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass


class EditMatchError(ValueError):
    """Raised when an edit cannot be applied safely and unambiguously."""


@dataclass(frozen=True)
class EditApplication:
    content: str
    strategy: str
    replacements: int
    line: int


def _all_spans(haystack: str, needle: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = 0
    while needle:
        index = haystack.find(needle, start)
        if index < 0:
            break
        spans.append((index, index + len(needle)))
        start = index + len(needle)
    return spans


def _normalized_with_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace while retaining source offsets for replacements."""
    output: list[str] = []
    offsets: list[int] = []
    in_whitespace = False
    for index, char in enumerate(text):
        if char.isspace():
            if not in_whitespace:
                output.append(" ")
                offsets.append(index)
            in_whitespace = True
        else:
            output.append(char)
            offsets.append(index)
            in_whitespace = False
    return "".join(output), offsets


def _whitespace_spans(content: str, old_text: str) -> list[tuple[int, int]]:
    normalized_content, offsets = _normalized_with_map(content)
    normalized_old = re.sub(r"\s+", " ", old_text).strip()
    if not normalized_old:
        return []
    spans = _all_spans(normalized_content, normalized_old)
    resolved: list[tuple[int, int]] = []
    for start, end in spans:
        source_start = offsets[start]
        source_end = offsets[end - 1] + 1
        resolved.append((source_start, source_end))
    return resolved


def _line_candidates(content: str, old_text: str) -> list[tuple[float, int, int, str]]:
    lines = content.splitlines(keepends=True)
    old_lines = old_text.splitlines() or [old_text]
    target = re.sub(r"\s+", " ", old_text).strip()
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    candidates: list[tuple[float, int, int, str]] = []
    base_size = max(1, len(old_lines))
    for size in sorted({max(1, base_size - 1), base_size, base_size + 1}):
        for start_line in range(0, max(0, len(lines) - size + 1)):
            end_line = start_line + size
            candidate = "".join(lines[start_line:end_line])
            normalized = re.sub(r"\s+", " ", candidate).strip()
            ratio = difflib.SequenceMatcher(None, target, normalized).ratio()
            candidates.append((
                ratio,
                offsets[start_line],
                offsets[end_line],
                candidate,
            ))
    return sorted(candidates, key=lambda item: item[0], reverse=True)


def _replace_spans(content: str, spans: list[tuple[int, int]], new_text: str) -> str:
    updated = content
    for start, end in reversed(spans):
        updated = updated[:start] + new_text + updated[end:]
    return updated


def apply_text_edit(
    content: str,
    old_text: str,
    new_text: str,
    *,
    replace_all: bool = False,
) -> EditApplication:
    """Apply a model edit with ambiguity guards and conservative recovery."""
    if not old_text:
        raise EditMatchError("old_text must not be empty.")

    exact = _all_spans(content, old_text)
    if exact:
        if len(exact) > 1 and not replace_all:
            raise EditMatchError(
                f"old_text matched {len(exact)} locations. Include more surrounding "
                "context to make the edit unique, or set replace_all=true intentionally."
            )
        spans = exact if replace_all else exact[:1]
        return EditApplication(
            _replace_spans(content, spans, new_text),
            "exact",
            len(spans),
            content.count("\n", 0, spans[0][0]) + 1,
        )

    whitespace = _whitespace_spans(content, old_text)
    if whitespace:
        if len(whitespace) > 1 and not replace_all:
            raise EditMatchError(
                f"Whitespace-normalized old_text matched {len(whitespace)} locations. "
                "Include more surrounding context to make the edit unique."
            )
        spans = whitespace if replace_all else whitespace[:1]
        return EditApplication(
            _replace_spans(content, spans, new_text),
            "whitespace",
            len(spans),
            content.count("\n", 0, spans[0][0]) + 1,
        )

    candidates = _line_candidates(content, old_text)
    if candidates:
        best = candidates[0]
        competing_locations = [
            item for item in candidates[1:]
            if item[2] <= best[1] or item[1] >= best[2]
        ]
        second_ratio = competing_locations[0][0] if competing_locations else 0.0
        # High threshold + separation prevents a near-match in repeated code
        # from silently editing the wrong block.
        if len(old_text.strip()) >= 20 and best[0] >= 0.92 and best[0] - second_ratio >= 0.03:
            span = (best[1], best[2])
            return EditApplication(
                _replace_spans(content, [span], new_text),
                "fuzzy",
                1,
                content.count("\n", 0, span[0]) + 1,
            )

        line = content.count("\n", 0, best[1]) + 1
        preview = best[3].strip()
        if len(preview) > 800:
            preview = preview[:800] + "\n...[preview truncated]"
        raise EditMatchError(
            "old_text was not found. "
            f"Closest candidate starts at line {line} ({best[0]:.0%} similar):\n{preview}\n"
            "Re-read that range and retry with the exact surrounding text."
        )

    raise EditMatchError("old_text was not found and the file has no comparable text.")

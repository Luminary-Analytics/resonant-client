"""
Tool-output truncation utilities.

Each tool (file_read, bash, grep, etc.) used to truncate its output ad-hoc with
hard char-count slices, which (a) routinely cut lines mid-token, (b) gave the
LLM no signal about what was lost, and (c) varied in policy across tools. This
module centralizes the policy.

Two-axis truncation: lines OR bytes — whichever limit is hit first wins. Never
returns partial lines (except in one tail edge case where a single trailing
line itself exceeds the byte budget; we keep its end so error context isn't
lost).

Direction matters:

- `truncate_head` — keep the *first* N lines/bytes. Use for file reads (the
  start of a file is what the model needs to navigate / understand it).
- `truncate_tail` — keep the *last* N lines/bytes. Use for bash output (errors
  and final results land at the end; the head is usually warm-up noise).
- `truncate_line` — bound a single line's character count. Use for grep
  matches where a 100KB line would dominate a result list.

Ported from pi-coding-agent's truncate.ts (MIT, Mario Zechner). Behavior is
intentionally compatible so output shapes are predictable.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_BYTES = 50 * 1024  # 50KB
GREP_MAX_LINE_LENGTH = 500     # max chars per single grep match line


@dataclass
class TruncationResult:
    """Outcome of a truncation pass — the truncated content plus metadata.

    The metadata fields let callers render an honest "showing X of Y" footer
    without re-measuring; the LLM can also reason about what was elided.
    """

    content: str
    truncated: bool
    truncated_by: Optional[str]      # "lines", "bytes", or None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool          # tail-only edge case
    first_line_exceeds_limit: bool   # head-only edge case (single huge line)
    max_lines: int
    max_bytes: int


def format_size(num_bytes: int) -> str:
    """Render a byte count as a human-friendly size string ("3.2KB", "1.5MB")."""
    if num_bytes < 1024:
        return f"{num_bytes}B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f}KB"
    return f"{num_bytes / (1024 * 1024):.1f}MB"


def _byte_len(s: str) -> int:
    return len(s.encode("utf-8"))


def truncate_head(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the first N lines/bytes of `content`.

    Suitable for file reads. Never returns a partial line. If the very first
    line exceeds the byte limit we return empty content with
    `first_line_exceeds_limit=True` so the caller can surface a clear error
    rather than silently dropping the file.
    """
    total_bytes = _byte_len(content)
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    # Pathological case: the first line on its own busts the byte budget.
    # Returning a half-line here would lie about line integrity.
    first_line_bytes = _byte_len(lines[0])
    if first_line_bytes > max_bytes:
        return TruncationResult(
            content="",
            truncated=True,
            truncated_by="bytes",
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=0,
            output_bytes=0,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes_count = 0
    truncated_by = "lines"

    for i, line in enumerate(lines):
        if i >= max_lines:
            break
        line_bytes = _byte_len(line) + (1 if i > 0 else 0)  # +1 for the joining newline
        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            break
        output_lines.append(line)
        output_bytes_count += line_bytes

    if len(output_lines) >= max_lines and output_bytes_count <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines)
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=_byte_len(output_content),
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    content: str,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the last N lines/bytes of `content`.

    Suitable for bash / shell output where the failure mode and final result
    are at the bottom and the head is warm-up noise. May return a partial
    *first* line in one edge case: when the last line of the original content
    already exceeds the byte budget, we keep its tail (error messages of last
    resort beat returning empty).
    """
    total_bytes = _byte_len(content)
    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncationResult(
            content=content,
            truncated=False,
            truncated_by=None,
            total_lines=total_lines,
            total_bytes=total_bytes,
            output_lines=total_lines,
            output_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )

    output_lines: list[str] = []
    output_bytes_count = 0
    truncated_by = "lines"
    last_line_partial = False

    for i in range(len(lines) - 1, -1, -1):
        if len(output_lines) >= max_lines:
            break
        line = lines[i]
        line_bytes = _byte_len(line) + (1 if output_lines else 0)  # +1 for newline join

        if output_bytes_count + line_bytes > max_bytes:
            truncated_by = "bytes"
            # Single trailing line bigger than the entire byte budget — keep its tail.
            if not output_lines:
                truncated_line = _truncate_string_to_bytes_from_end(line, max_bytes)
                output_lines.insert(0, truncated_line)
                output_bytes_count = _byte_len(truncated_line)
                last_line_partial = True
            break

        output_lines.insert(0, line)
        output_bytes_count += line_bytes

    if len(output_lines) >= max_lines and output_bytes_count <= max_bytes:
        truncated_by = "lines"

    output_content = "\n".join(output_lines)
    return TruncationResult(
        content=output_content,
        truncated=True,
        truncated_by=truncated_by,
        total_lines=total_lines,
        total_bytes=total_bytes,
        output_lines=len(output_lines),
        output_bytes=_byte_len(output_content),
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def _truncate_string_to_bytes_from_end(s: str, max_bytes: int) -> str:
    """Return the last `max_bytes` bytes of `s` aligned to a UTF-8 char boundary."""
    buf = s.encode("utf-8")
    if len(buf) <= max_bytes:
        return s
    start = len(buf) - max_bytes
    # A continuation byte starts with 0b10xxxxxx (mask 0xC0 == 0x80) — skip
    # forward until we land on a leading byte so we don't slice mid-codepoint.
    while start < len(buf) and (buf[start] & 0xC0) == 0x80:
        start += 1
    return buf[start:].decode("utf-8", errors="replace")


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    """Cap a single line at `max_chars` characters, appending a `[truncated]` marker.

    Used for grep match lines so a 50KB minified-JS hit doesn't dominate the
    result list. Returns `(text, was_truncated)`.
    """
    if len(line) <= max_chars:
        return line, False
    return f"{line[:max_chars]}... [truncated]", True


def render_truncation_footer(result: TruncationResult) -> str:
    """One-line summary suitable for appending to truncated tool output.

    Empty string when nothing was truncated, so callers can unconditionally
    concat without worrying about double-newlines.
    """
    if not result.truncated:
        return ""
    if result.first_line_exceeds_limit:
        return (
            f"\n\n[truncated: first line is {format_size(result.total_bytes)}, "
            f"exceeds {format_size(result.max_bytes)} budget]"
        )
    showing = (
        f"showing {result.output_lines}/{result.total_lines} lines, "
        f"{format_size(result.output_bytes)}/{format_size(result.total_bytes)}"
    )
    by = result.truncated_by or "lines"
    return f"\n\n[truncated by {by}: {showing}]"

"""
Tests for resonant_client.engine.truncation.

Behavior under test:
  - truncate_head — file-style: keep the start, line-aligned.
  - truncate_tail — bash-style: keep the end, line-aligned (with one edge case).
  - truncate_line — single-line cap with [truncated] suffix (grep matches).
  - render_truncation_footer — readable summary string for tool output.

Plus a couple of UTF-8 boundary checks since the byte-budget tail truncator
slices into raw UTF-8 buffers.
"""
from __future__ import annotations

from resonant_client.engine.truncation import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_LINES,
    format_size,
    render_truncation_footer,
    truncate_head,
    truncate_line,
    truncate_tail,
)


# ---------------------------------------------------------------------------
# truncate_head
# ---------------------------------------------------------------------------

class TestTruncateHead:
    def test_short_content_passes_through_untouched(self):
        r = truncate_head("hello\nworld")
        assert r.truncated is False
        assert r.truncated_by is None
        assert r.content == "hello\nworld"
        assert r.total_lines == 2
        assert r.output_lines == 2

    def test_line_limit_caps_at_max_lines(self):
        content = "\n".join(f"line {i}" for i in range(100))
        r = truncate_head(content, max_lines=10, max_bytes=1_000_000)
        assert r.truncated is True
        assert r.truncated_by == "lines"
        assert r.output_lines == 10
        assert r.content == "\n".join(f"line {i}" for i in range(10))

    def test_byte_limit_keeps_only_complete_lines(self):
        # Each "line N\n" is ~7-8 bytes. Cap at 20 bytes → ~2-3 complete lines.
        content = "line 0\nline 1\nline 2\nline 3\nline 4"
        r = truncate_head(content, max_lines=1000, max_bytes=20)
        assert r.truncated is True
        assert r.truncated_by == "bytes"
        assert "\n" in r.content
        # Must end on a clean line — never partial.
        assert not r.content.endswith("lin")
        assert not r.content.endswith("line ")

    def test_first_line_exceeds_limit_returns_empty_with_flag(self):
        # A single 1KB line with a 100-byte budget — pathological case.
        content = "x" * 1024 + "\nshort"
        r = truncate_head(content, max_lines=100, max_bytes=100)
        assert r.truncated is True
        assert r.first_line_exceeds_limit is True
        assert r.content == ""

    def test_line_count_metadata_reflects_original(self):
        content = "\n".join(f"l{i}" for i in range(50))
        r = truncate_head(content, max_lines=5, max_bytes=1_000_000)
        assert r.total_lines == 50
        assert r.output_lines == 5


# ---------------------------------------------------------------------------
# truncate_tail
# ---------------------------------------------------------------------------

class TestTruncateTail:
    def test_short_content_passes_through_untouched(self):
        r = truncate_tail("hello\nworld")
        assert r.truncated is False
        assert r.content == "hello\nworld"

    def test_keeps_last_n_lines(self):
        content = "\n".join(f"line {i}" for i in range(20))
        r = truncate_tail(content, max_lines=5, max_bytes=1_000_000)
        assert r.truncated is True
        assert r.output_lines == 5
        # Tail truncation = the END is what matters (errors live there).
        assert r.content == "\n".join(f"line {i}" for i in range(15, 20))

    def test_byte_limit_drops_from_the_head(self):
        content = "line 0\nline 1\nline 2\nline 3\nline 4"
        r = truncate_tail(content, max_lines=1000, max_bytes=20)
        assert r.truncated is True
        assert r.truncated_by == "bytes"
        # Last line ("line 4") should be present.
        assert r.content.endswith("line 4")

    def test_single_huge_trailing_line_returns_partial_tail(self):
        # The one edge case where tail truncation emits a partial line:
        # the last (only) line itself busts the budget, so we keep its end.
        content = "x" * 5000
        r = truncate_tail(content, max_lines=1000, max_bytes=100)
        assert r.truncated is True
        assert r.last_line_partial is True
        assert len(r.content.encode("utf-8")) <= 100
        # And what we kept is the END of the original (error-context preservation).
        assert r.content.endswith("x" * 50)


# ---------------------------------------------------------------------------
# UTF-8 boundary preservation in tail
# ---------------------------------------------------------------------------

class TestUtf8Boundaries:
    def test_tail_does_not_split_multibyte_codepoint(self):
        # 4-byte emoji repeated. With a budget that lands in the middle of
        # one, the truncator should walk forward to a clean codepoint start.
        emoji = "🎉"  # U+1F389, 4 bytes UTF-8
        content = emoji * 50  # 200 bytes
        r = truncate_tail(content, max_lines=1000, max_bytes=42)
        # Result must be valid UTF-8 — re-encoding/decoding should roundtrip.
        roundtripped = r.content.encode("utf-8").decode("utf-8")
        assert roundtripped == r.content
        # And every char in the result should be the emoji (no half-codepoints).
        assert all(c == emoji for c in r.content)


# ---------------------------------------------------------------------------
# truncate_line
# ---------------------------------------------------------------------------

class TestTruncateLine:
    def test_short_line_unchanged(self):
        text, was = truncate_line("hello", max_chars=100)
        assert was is False
        assert text == "hello"

    def test_long_line_gets_suffix(self):
        text, was = truncate_line("x" * 1000, max_chars=10)
        assert was is True
        assert text.startswith("xxxxxxxxxx")
        assert text.endswith("[truncated]")

    def test_at_exact_limit_unchanged(self):
        text, was = truncate_line("a" * 500, max_chars=500)
        assert was is False
        assert text == "a" * 500


# ---------------------------------------------------------------------------
# render_truncation_footer
# ---------------------------------------------------------------------------

class TestFooter:
    def test_empty_when_not_truncated(self):
        r = truncate_head("short")
        assert render_truncation_footer(r) == ""

    def test_includes_size_summary_when_truncated(self):
        content = "\n".join(f"l{i}" for i in range(100))
        r = truncate_head(content, max_lines=5, max_bytes=1_000_000)
        footer = render_truncation_footer(r)
        assert footer.startswith("\n\n[truncated")
        assert "5/100" in footer  # output_lines/total_lines

    def test_special_message_for_first_line_exceeds(self):
        content = "x" * 1024
        r = truncate_head(content, max_lines=100, max_bytes=100)
        footer = render_truncation_footer(r)
        assert "first line is" in footer
        assert "exceeds" in footer


# ---------------------------------------------------------------------------
# format_size
# ---------------------------------------------------------------------------

class TestFormatSize:
    def test_bytes(self):
        assert format_size(0) == "0B"
        assert format_size(512) == "512B"

    def test_kb(self):
        assert format_size(1024) == "1.0KB"
        assert format_size(2048) == "2.0KB"

    def test_mb(self):
        assert format_size(1024 * 1024) == "1.0MB"
        assert format_size(int(1.5 * 1024 * 1024)) == "1.5MB"


# ---------------------------------------------------------------------------
# Defaults sanity-check — guards against accidental tightening
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_default_max_lines_reasonable(self):
        # 2000 lines is enough to read most source files end-to-end. Below
        # 1000 we'd start cutting off long single files mid-read.
        assert DEFAULT_MAX_LINES >= 1000

    def test_default_max_bytes_reasonable(self):
        # 50KB is large enough for typical files, small enough that the LLM
        # can still hold the rest of the conversation.
        assert DEFAULT_MAX_BYTES >= 30 * 1024
        assert DEFAULT_MAX_BYTES <= 200 * 1024

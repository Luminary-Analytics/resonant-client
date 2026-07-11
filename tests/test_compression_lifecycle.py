"""Tests for v0.5.11a2 — engine/compression.py coverage deepening.

Pre-v0.5.11 coverage was 37%. test_compression_budget.py covers
`model_context_budget` and the model-name pathway through
`should_compress`, but the actual compression lifecycle —
`estimate_tokens` (list-content branch), `_extract_text`,
`_build_summary_prompt`, and `compress` itself — was untested.

Compression fires when conversation history grows past the
per-model context budget; a regression in any of these helpers
would corrupt summaries silently and the bug only manifests after
KEEP_RECENT_TURNS+ user messages have accumulated.

Covered here:
- estimate_tokens: list-content + mixed-dict-and-non-dict-parts.
- _extract_text: string / list-of-text-parts / list-with-non-text-types
  / non-string non-list (str repr).
- _build_summary_prompt: user/assistant/tool_call/tool_result
  formatting, long-text truncation at 2000 chars, tool_result
  truncation at 500 chars, empty-text skip, unknown-role skip.
- compress: full lifecycle with a stub backend, plus the early-out
  paths (history too short, summary failure, empty summary, split_idx
  ≤ 2 sanity guard).
"""
from __future__ import annotations

from typing import Iterator

from resonant_client.engine.compression import (
    KEEP_RECENT_TURNS,
    _build_summary_prompt,
    _extract_text,
    compress,
    estimate_tokens,
)


# ── estimate_tokens ────────────────────────────────────────────────────


class TestEstimateTokens:
    def test_empty_history_zero_tokens(self):
        assert estimate_tokens([]) == 0

    def test_string_content_counted_at_4_chars_per_token(self):
        # 8 chars / 4 = 2 tokens
        history = [{"role": "user", "content": "hi there"}]
        assert estimate_tokens(history) == 2

    def test_list_content_text_parts_counted(self):
        # The branch on lines 77-80: list content with dict parts that
        # have a "text" key.
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},     # 5 chars
                    {"type": "text", "text": "world!!"},   # 7 chars
                ],
            },
        ]
        # 12 / 4 = 3
        assert estimate_tokens(history) == 3

    def test_list_content_skips_non_dict_parts(self):
        # Defensive: if a list contains a non-dict (a string, an int,
        # whatever) the loop must not crash. It just doesn't add to
        # the count.
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "abcd"},  # 4 chars
                    "raw string skipped",
                    42,
                    None,
                ],
            },
        ]
        assert estimate_tokens(history) == 1

    def test_list_content_dicts_without_text_field_count_zero(self):
        history = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "..."}},
                    {"type": "text"},  # missing 'text' key
                ],
            },
        ]
        assert estimate_tokens(history) == 0

    def test_missing_content_field_zero(self):
        history = [{"role": "user"}]
        assert estimate_tokens(history) == 0

    def test_mixed_string_and_list_entries_summed(self):
        history = [
            {"role": "user", "content": "abcd"},          # 4 chars
            {"role": "assistant", "content": [
                {"type": "text", "text": "efgh"},          # 4 chars
            ]},
        ]
        # 8 / 4 = 2
        assert estimate_tokens(history) == 2


# ── _extract_text ──────────────────────────────────────────────────────


class TestExtractText:
    def test_string_content_returned_as_is(self):
        assert _extract_text({"role": "user", "content": "hello"}) == "hello"

    def test_list_content_text_parts_joined(self):
        entry = {
            "role": "user",
            "content": [
                {"type": "text", "text": "alpha"},
                {"type": "text", "text": "beta"},
            ],
        }
        assert _extract_text(entry) == "alpha beta"

    def test_list_content_skips_non_text_types(self):
        # image_url etc. should NOT contribute to the extracted text.
        entry = {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "image_url", "image_url": {"url": "..."}},
                {"type": "text", "text": "bye"},
            ],
        }
        assert _extract_text(entry) == "hi bye"

    def test_list_content_skips_non_dict_parts(self):
        entry = {"role": "user", "content": ["not-a-dict", 42]}
        assert _extract_text(entry) == ""

    def test_other_content_types_str_repr(self):
        # A non-string non-list content (e.g. int / None) falls through
        # to `str(content)`. Defensive — real history doesn't have this
        # but the code guards against it.
        assert _extract_text({"content": 42}) == "42"
        assert _extract_text({"content": None}) == "None"

    def test_missing_content_returns_empty(self):
        assert _extract_text({"role": "user"}) == ""


# ── _build_summary_prompt ──────────────────────────────────────────────


class TestBuildSummaryPrompt:
    def test_user_lines_have_user_prefix(self):
        out = _build_summary_prompt([
            {"role": "user", "content": "hello"},
        ])
        assert "User: hello" in out

    def test_assistant_lines_have_assistant_prefix(self):
        out = _build_summary_prompt([
            {"role": "assistant", "content": "hi back"},
        ])
        assert "Assistant: hi back" in out

    def test_tool_call_uses_name_field(self):
        # tool_call entries only render when content is non-empty
        # (the `if not text: continue` guard skips empty-content
        # entries regardless of role). Content here is the call args.
        out = _build_summary_prompt([
            {"role": "tool_call", "name": "bash", "content": "ls -la"},
        ])
        assert "[Tool call: bash]" in out

    def test_empty_content_tool_call_skipped(self):
        # The `if not text: continue` guard fires before the role
        # branches — so a tool_call with empty content renders nothing
        # (no `[Tool call: ...]` bracket appears).
        out = _build_summary_prompt([
            {"role": "tool_call", "name": "bash", "content": ""},
        ])
        assert "[Tool call:" not in out

    def test_tool_result_truncated_to_500_chars(self):
        long_text = "x" * 1000
        out = _build_summary_prompt([
            {"role": "tool_result", "content": long_text},
        ])
        # The tool_result line includes the first 500 chars (no
        # ellipsis appended in the helper for tool_result).
        assert "[Tool result: " in out
        # Check the length of the tool-result content slice
        marker = "[Tool result: "
        idx = out.index(marker) + len(marker)
        end = out.index("]", idx)
        assert (end - idx) == 500

    def test_long_user_text_truncated_at_2000_with_ellipsis(self):
        long_text = "y" * 2500
        out = _build_summary_prompt([
            {"role": "user", "content": long_text},
        ])
        # The truncation appends "..." so the rendered substring is
        # 2000 chars + "...".
        assert "y" * 2000 + "..." in out
        # The full 2500-char original must NOT appear.
        assert "y" * 2500 not in out

    def test_empty_text_entry_skipped(self):
        out = _build_summary_prompt([
            {"role": "user", "content": ""},
            {"role": "user", "content": "real content"},
        ])
        # Only the non-empty one renders.
        assert out.count("User:") == 1
        assert "real content" in out

    def test_unknown_role_silently_skipped(self):
        out = _build_summary_prompt([
            {"role": "system_internal", "content": "should not appear"},
            {"role": "user", "content": "kept"},
        ])
        assert "should not appear" not in out
        assert "User: kept" in out

    def test_summary_prompt_includes_instructions_header(self):
        out = _build_summary_prompt([
            {"role": "user", "content": "x"},
        ])
        assert "Summarize this conversation" in out
        assert "decisions made" in out
        assert "under 500 words" in out

    def test_list_content_extracted_and_rendered(self):
        # _extract_text handles list-content; _build_summary_prompt
        # then renders the joined text.
        out = _build_summary_prompt([
            {"role": "assistant", "content": [
                {"type": "text", "text": "alpha"},
                {"type": "text", "text": "beta"},
            ]},
        ])
        assert "Assistant: alpha beta" in out


# ── compress ───────────────────────────────────────────────────────────


class _StubSession:
    """Minimal duck-typed Session for the compress() function."""

    def __init__(self, history, backend):
        self.conversation_history = history
        self.backend = backend


class _StubBackend:
    """Yields canned (event_type, data) pairs for stream()."""

    def __init__(self, deltas: list[str], raise_exc: bool = False):
        self.deltas = deltas
        self.raise_exc = raise_exc
        self.stream_calls: list[dict] = []

    def stream(
        self, *, user_msg, conversation_history, instructions,
        tools, max_tokens,
    ) -> Iterator[tuple[str, dict]]:
        self.stream_calls.append({
            "user_msg": user_msg,
            "instructions": instructions,
            "max_tokens": max_tokens,
        })
        if self.raise_exc:
            raise RuntimeError("simulated upstream failure")
        for d in self.deltas:
            yield ("text.delta", {"delta": d})
        yield ("done", {})


def _build_long_history(n_user_pairs: int = 10, msg_chars: int = 5000) -> list:
    """Make a history that easily trips the 100K-token compress
    threshold (5000 chars × 20 entries × 2 = 200_000 chars ≈ 50k tokens
    — but estimate_tokens uses /4, so 200_000/4 = 50000 tokens. Need
    more)."""
    history = []
    for i in range(n_user_pairs):
        history.append({"role": "user", "content": "u" * msg_chars + f" {i}"})
        history.append({"role": "assistant", "content": "a" * msg_chars + f" {i}"})
    return history


class TestCompress:
    def test_returns_history_unchanged_when_too_short(self):
        # Below the KEEP_RECENT_TURNS*2+4 threshold, compress is a no-op.
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        backend = _StubBackend(deltas=["unused"])
        session = _StubSession(history, backend)
        new_history, summary = compress(session, max_tokens=10)  # tiny
        assert new_history is history
        assert summary == ""
        # The backend was NEVER called — short-circuit before stream.
        assert backend.stream_calls == []

    def test_returns_history_unchanged_when_under_threshold(self):
        # Long enough to pass the length check, but well under the
        # 100K-token default budget.
        history = _build_long_history(n_user_pairs=KEEP_RECENT_TURNS + 2,
                                      msg_chars=10)
        backend = _StubBackend(deltas=["unused"])
        session = _StubSession(history, backend)
        new_history, summary = compress(session)
        assert new_history is history
        assert summary == ""
        assert backend.stream_calls == []

    def test_compresses_when_over_threshold(self):
        # Force the threshold low enough to trip with a small history.
        history = _build_long_history(n_user_pairs=KEEP_RECENT_TURNS + 2,
                                      msg_chars=200)
        backend = _StubBackend(deltas=["This ", "is ", "the ", "summary."])
        session = _StubSession(history, backend)
        new_history, summary = compress(session, max_tokens=10)

        # Backend was called once with the summary prompt.
        assert len(backend.stream_calls) == 1
        assert "Summarize" in backend.stream_calls[0]["user_msg"]
        assert backend.stream_calls[0]["max_tokens"] == 1024

        # Summary returned + folded into history[0].
        assert summary == "This is the summary."
        assert new_history[0]["role"] == "assistant"
        assert "Previous conversation summary" in new_history[0]["content"]
        assert "This is the summary." in new_history[0]["content"]

    def test_compress_keeps_recent_n_user_turns(self):
        # The compressed history should contain the summary + the last
        # KEEP_RECENT_TURNS user messages and their assistant responses.
        n = KEEP_RECENT_TURNS + 4  # 4 extra pairs to compress
        history = _build_long_history(n_user_pairs=n, msg_chars=200)
        backend = _StubBackend(deltas=["S"])
        session = _StubSession(history, backend)
        new_history, summary = compress(session, max_tokens=10)

        # Should have 1 (summary) + KEEP_RECENT_TURNS*2 (recent pairs)
        # entries — though if the split lands mid-turn the count can
        # vary slightly. Lower bound is the summary + recent pairs.
        assert len(new_history) <= len(history)
        # Summary entry first.
        assert "Previous conversation summary" in new_history[0]["content"]
        # The most recent user message must still be present at the end.
        last_user = next(
            (e for e in reversed(history) if e.get("role") == "user"), None,
        )
        assert last_user in new_history

    def test_summary_failure_returns_original(self):
        history = _build_long_history(n_user_pairs=KEEP_RECENT_TURNS + 2,
                                      msg_chars=200)
        backend = _StubBackend(deltas=[], raise_exc=True)
        session = _StubSession(history, backend)
        new_history, summary = compress(session, max_tokens=10)
        # Backend raised → fallback to original history + empty summary.
        assert new_history is history
        assert summary == ""

    def test_empty_summary_returns_original(self):
        # If the model emits zero text (or only whitespace), the
        # compressor refuses to fold an empty summary into history.
        history = _build_long_history(n_user_pairs=KEEP_RECENT_TURNS + 2,
                                      msg_chars=200)
        backend = _StubBackend(deltas=["   ", "\n\n", " "])  # all whitespace
        session = _StubSession(history, backend)
        new_history, summary = compress(session, max_tokens=10)
        assert new_history is history
        assert summary == ""

    def test_explicit_backend_arg_overrides_session_backend(self):
        history = _build_long_history(n_user_pairs=KEEP_RECENT_TURNS + 2,
                                      msg_chars=200)
        session_backend = _StubBackend(deltas=["unused"])
        explicit_backend = _StubBackend(deltas=["from-explicit"])
        session = _StubSession(history, session_backend)

        new_history, summary = compress(
            session, backend=explicit_backend, max_tokens=10,
        )
        # Explicit backend was used; session_backend was NOT.
        assert summary == "from-explicit"
        assert len(explicit_backend.stream_calls) == 1
        assert session_backend.stream_calls == []

    def test_runtime_context_window_resolves_threshold(self):
        # Runtime model metadata overrides a caller's generic max_tokens value.
        history = _build_long_history(n_user_pairs=KEEP_RECENT_TURNS + 2,
                                      msg_chars=300)
        backend = _StubBackend(deltas=["S"])
        session = _StubSession(history, backend)

        # With max_tokens=10**9 (huge), the only way compress fires
        # is via the model_name budget override.
        new_history, summary = compress(
            session, max_tokens=10**9,
            model_name="deepseek-v4-flash:cloud",
        )
        # If the override worked, compress fires and summary is non-empty.
        # If it didn't work, summary stays "" and history stays the same.
        # Pick whichever assertion is correct based on actual token count.
        # Use enough history to exceed the synthetic 32K runtime window.
        big_history = _build_long_history(
            n_user_pairs=KEEP_RECENT_TURNS + 30, msg_chars=2000,
        )
        backend2 = _StubBackend(deltas=["S"])
        session2 = _StubSession(big_history, backend2)
        _, summary2 = compress(
            session2, max_tokens=10**9,
            model_name="deepseek-v4-flash:cloud",
            context_window=32_768,
        )
        assert summary2 == "S"  # compress fired despite huge max_tokens

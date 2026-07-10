"""
Tests for the v0.4.6 (T2.1) per-tier context budget.

Pre-T2.1 every model used `DEFAULT_MAX_CONTEXT_TOKENS = 100_000` —
flash with its smaller effective context would OOM before the
compressor ever fired; pro was over-conservative. The new
`model_context_budget()` helper resolves a per-model threshold and
`should_compress` / `compress` accept a `model_name=...` kwarg that
overrides the explicit `max_tokens`.
"""

from __future__ import annotations

from resonant_client.engine.compression import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    evict_old_tool_outputs,
    model_context_budget,
    should_compress,
)


# ── model_context_budget ────────────────────────────────────────────────


class TestModelContextBudgetExact:
    def test_flash_exact(self):
        assert model_context_budget("deepseek-v4-flash:cloud") == 24_000

    def test_pro_exact(self):
        assert model_context_budget("deepseek-v4-pro:cloud") == 96_000

    def test_generic_v4(self):
        assert model_context_budget("deepseek-v4:cloud") == 48_000

    def test_glm_5_2_flagship_exact(self):
        # v0.6.5 — the flagship gets an explicit pro-tier budget rather
        # than falling through to the generic 100K default.
        assert model_context_budget("glm-5.2:cloud") == 96_000

    def test_case_insensitive(self):
        # The Ollama model selector tends to lowercase but tools could
        # pass mixed case; the lookup must not care.
        assert model_context_budget("DeepSeek-V4-Flash:cloud") == 24_000
        assert model_context_budget("DEEPSEEK-V4-PRO:CLOUD") == 96_000


class TestModelContextBudgetFamily:
    def test_unknown_deepseek_flash_falls_back_to_flash_tier(self):
        # A future variant like "deepseek-v5-flash:cloud" should still
        # be treated as a flash-class model (small budget).
        assert model_context_budget("deepseek-v5-flash:cloud") == 24_000

    def test_unknown_deepseek_pro_falls_back_to_pro_tier(self):
        assert model_context_budget("deepseek-v5-pro:cloud") == 96_000

    def test_bare_deepseek_uses_mid_tier(self):
        # "deepseek" without flash/pro suffix → mid (48K)
        assert model_context_budget("deepseek-coder:33b") == 48_000

    def test_any_glm_uses_flagship_budget(self):
        # v0.6.5 — all GLM cloud tiers carry large (≥200K) windows, so
        # any "glm" resolves to the flagship budget, not the default.
        assert model_context_budget("glm-5.1:cloud") == 96_000
        assert model_context_budget("glm-4.7:cloud") == 96_000

    def test_unknown_model_uses_default(self):
        assert model_context_budget("llama3:70b") == DEFAULT_MAX_CONTEXT_TOKENS
        assert model_context_budget("qwen2.5-coder:32b") == DEFAULT_MAX_CONTEXT_TOKENS


class TestModelContextBudgetEdgeCases:
    def test_empty_string_uses_default(self):
        assert model_context_budget("") == DEFAULT_MAX_CONTEXT_TOKENS

    def test_none_uses_default(self):
        assert model_context_budget(None) == DEFAULT_MAX_CONTEXT_TOKENS

    def test_runtime_context_window_overrides_stale_model_table(self):
        assert model_context_budget("glm-5.2:cloud", context_window=32_768) == 24_576
        assert model_context_budget("unknown", context_window=131_072) == 98_304


# ── should_compress with model_name ─────────────────────────────────────


def _make_long_history(approx_chars: int) -> list:
    """Build a synthetic history that roughly hits `approx_chars` total
    payload characters. The compressor estimates 4 chars/token so this
    is a rough way to hit specific token thresholds in tests.
    """
    # Create enough turns to clear the KEEP_RECENT_TURNS gate first.
    # Each entry contributes its `content` length to estimate_tokens.
    entry_payload = "x" * 1000  # ~250 tokens each
    needed = approx_chars // len(entry_payload) + 1
    history = []
    for i in range(needed):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": entry_payload})
    return history


class TestShouldCompressWithModel:
    def test_flash_trips_at_lower_threshold(self):
        # ~30K tokens of payload (well above flash's 24K threshold,
        # well below the 100K default).
        history = _make_long_history(approx_chars=30_000 * 4)
        assert should_compress(history, model_name="deepseek-v4-flash:cloud") is True
        # With the default threshold (100K), it should NOT trip.
        assert should_compress(history) is False

    def test_pro_does_not_trip_at_flash_threshold(self):
        # ~30K tokens — above flash's 24K, well below pro's 96K.
        history = _make_long_history(approx_chars=30_000 * 4)
        assert should_compress(history, model_name="deepseek-v4-pro:cloud") is False

    def test_pro_trips_at_higher_threshold(self):
        # ~100K tokens — above pro's 96K.
        history = _make_long_history(approx_chars=100_000 * 4)
        assert should_compress(history, model_name="deepseek-v4-pro:cloud") is True

    def test_short_history_never_compresses_regardless_of_model(self):
        # KEEP_RECENT_TURNS gate — under the minimum entry count, no
        # model can trigger compression. (Defends the legacy floor.)
        short = [{"role": "user", "content": "hi"}] * 4
        assert should_compress(short, model_name="deepseek-v4-flash:cloud") is False

    def test_model_name_overrides_explicit_max_tokens(self):
        # When BOTH model_name and max_tokens are passed, model_name
        # wins. This is intentional — callers should be able to pass
        # model_name=self.backend.model without juggling tier numbers.
        history = _make_long_history(approx_chars=30_000 * 4)
        # max_tokens=1_000_000 would normally suppress compression…
        # …but flash's budget (24K) should still trip on this history.
        assert should_compress(
            history,
            max_tokens=1_000_000,
            model_name="deepseek-v4-flash:cloud",
        ) is True

    def test_no_model_name_falls_back_to_max_tokens_arg(self):
        # Backwards-compat: callers that don't know about model_name
        # see the pre-T2.1 behavior.
        history = _make_long_history(approx_chars=30_000 * 4)
        assert should_compress(history, max_tokens=20_000) is True
        assert should_compress(history, max_tokens=50_000) is False


class TestToolOutputEviction:
    def test_evicts_only_old_oversized_results(self):
        history = []
        for index in range(12):
            history.append({
                "role": "tool_result",
                "name": "file_read",
                "content": f"result-{index}:" + ("x" * 2_000),
            })

        pruned, count = evict_old_tool_outputs(history)

        assert count == 4
        assert "Earlier file_read result evicted" in pruned[0]["content"]
        assert pruned[4]["content"] == history[4]["content"]
        assert history[0]["content"].startswith("result-0:")

    def test_keeps_small_old_results(self):
        history = [
            {"role": "tool_result", "name": "bash", "content": "ok"}
            for _ in range(10)
        ]
        pruned, count = evict_old_tool_outputs(history)
        assert count == 0
        assert pruned == history

"""Tests for v0.6.2a2 — better skill name generation.

The v0.6.1 field run (docs/field-observations/2026-05-06-…) surfaced
that auto-extracted skill names are unreadable: mid-word truncations
("tsc-a", "the-fil") and redundant verb-article prefixes that cluster
similar skills under the same alphabetical head. v0.6.2a2 fixes
`slugify()` to do word-boundary truncation + drop common prefixes.

Tests cover:
- Each real bad example from the field run produces a readable slug
- Each common prefix is stripped
- Word-boundary truncation never leaves mid-word debris
- Pathological inputs (empty, all-punct, very-short, prefix-only)
  still produce a sane fallback
- The `drop_prefixes=False` opt-out preserves the legacy behavior
"""
from __future__ import annotations

import pytest

from resonant_client.orchestration.skill_extraction import (
    _NOISE_PREFIXES,
    _strip_noise_prefix,
    slugify,
)


# ── Real bad examples from the field run ──────────────────────────────


@pytest.mark.parametrize("intent,expected_max_len,must_not_end_with", [
    # Field-run example #1 — "actually-" hedge + "create-a-" verb
    (
        "Actually create wordcount.py with word-counting logic and the file-handling boilerplate",
        30,
        ("the-fil",),  # the bad tail from the v0.6.1 run
    ),
    # Field-run example #2 — "add-a-" prefix
    (
        "Add a CONTRIBUTING.md to this project with three sections setup-conventions-pr",
        30,
        ("set",),  # mid-word "set"
    ),
    # Field-run example #3 — "bootstrap-a-" prefix + bad "-tsc-a" tail
    (
        "Bootstrap a TypeScript roguelite skeleton with strict tsc and ESLint",
        30,
        ("tsc-a",),
    ),
    # Field-run example #4 — "build-a-" prefix
    (
        "Build a Python CLI utility wordcount.py at the project root",
        30,
        ("util", "-b"),
    ),
    # Field-run example #5 — "create-a-" prefix + "-hello-…" duplicate
    (
        "Create a file called hello.txt at the project root",
        30,
        ("hellotxt",),  # the bad smush "hellotxt-at"
    ),
])
def test_field_run_bad_examples_become_readable(intent, expected_max_len, must_not_end_with):
    slug = slugify(intent)
    # Length cap
    assert len(slug) <= expected_max_len, f"slug {slug!r} exceeds {expected_max_len}"
    # No mid-word tail debris
    for bad_tail in must_not_end_with:
        assert not slug.endswith(bad_tail), (
            f"slug {slug!r} ends with bad mid-word tail {bad_tail!r}"
        )
    # Slug should be non-empty and well-formed kebab
    assert slug
    assert not slug.startswith("-")
    assert not slug.endswith("-")
    assert "--" not in slug


# ── Prefix stripping ──────────────────────────────────────────────────


@pytest.mark.parametrize("prefix", list(_NOISE_PREFIXES))
def test_each_noise_prefix_is_stripped(prefix):
    """Every entry in _NOISE_PREFIXES actually gets stripped."""
    # Synthesize a slug starting with the prefix; the rest is a known
    # word so we can assert the prefix is gone but the body remains.
    slug = prefix + "foobar-thing"
    out = _strip_noise_prefix(slug)
    assert not out.startswith(prefix), f"{prefix!r} not stripped"
    assert "foobar-thing" in out


def test_no_recursive_prefix_stripping():
    """Only one prefix is removed, even if a second one appears.

    `_strip_noise_prefix` is intentionally single-pass to avoid
    over-aggressive removal when two prefixes accidentally chain
    (e.g. "actually-create-a-foo"). We strip "actually-" only,
    leaving "create-a-foo". The second pass through `slugify()`
    via the slugify() public surface DOES strip again, but the
    helper itself is single-pass.
    """
    out = _strip_noise_prefix("actually-create-a-foo")
    assert out == "create-a-foo"


def test_slugify_strips_only_one_prefix_layer():
    """slugify() also single-passes via its single _strip_noise_prefix call."""
    # "actually-build-a-rocket" → strip "actually-" → "build-a-rocket"
    out = slugify("actually build a rocket", max_len=50)
    assert out == "build-a-rocket"


def test_no_prefix_means_no_strip():
    """If the slug doesn't start with a noise prefix, it's untouched."""
    out = slugify("explore-codebase-deeply", max_len=50)
    assert out == "explore-codebase-deeply"


# ── Word-boundary truncation ──────────────────────────────────────────


def test_truncation_cuts_at_last_dash():
    out = slugify("alpha bravo charlie delta echo", max_len=20)
    # 'alpha-bravo-charlie-delta-echo' is 30; cut to 20 → 'alpha-bravo-charlie-' → strip dash
    # Last dash <=20 is at position 17 (after "charlie")
    assert out == "alpha-bravo-charlie"
    assert not out.endswith("-")


def test_truncation_preserves_short_slugs_under_cap():
    out = slugify("short slug", max_len=30)
    assert out == "short-slug"


def test_truncation_falls_back_to_hard_slice_for_one_giant_word():
    """If the only word is longer than max_len, hard-slice rather than
    return empty — but the slice must not start/end with junk."""
    out = slugify("supercalifragilisticexpialidocious", max_len=15)
    # No dashes anywhere, so the half-rule kicks in: last_dash = -1, no truncation logic, falls through to cut[:15].strip('-')
    assert len(out) == 15
    assert "-" not in out  # was a single word
    assert out == "supercalifragil"


def test_truncation_when_dash_is_too_early():
    """If the only dash is in the first half of the slug, hard-slice
    (don't accept a pathologically short cut)."""
    # Dash at position 2 ("a-bcdefghij…"). max_len=15. Dash is at 2,
    # which is <= 15//2 = 7, so we DON'T cut at it; hard-slice instead.
    out = slugify("a-verylongwordthatrunsforever", max_len=15)
    # Slug is "a-verylongwordthatrunsforever"; cut[:15] = "a-verylongwordt"
    # last_dash in cut = 1, 1 <= 15//2 = 7 → keep hard slice
    assert len(out) == 15
    assert out == "a-verylongwordt"


# ── Edge cases ────────────────────────────────────────────────────────


def test_empty_input_returns_skill_fallback():
    assert slugify("") == "skill"
    assert slugify("   ") == "skill"
    assert slugify("---") == "skill"


def test_only_punctuation_returns_skill_fallback():
    assert slugify("!!!@@@###$$$%%%") == "skill"


def test_input_that_is_only_a_noise_prefix_returns_skill_fallback():
    """If stripping a prefix leaves the empty string, return 'skill'."""
    assert slugify("create a") == "skill"
    assert slugify("build an") == "skill"


def test_unicode_input_is_dropped_to_ascii():
    out = slugify("café résumé naïve", max_len=30)
    # All non-ascii letters dropped, then re-collapsed
    # "café résumé naïve" → "caf rsum nave" → "caf-rsum-nave"
    assert out == "caf-rsum-nave"


def test_drop_prefixes_false_preserves_legacy_behavior():
    """The opt-out flag exists for callers that want the raw slug."""
    out = slugify("create a foo", drop_prefixes=False, max_len=30)
    assert out == "create-a-foo"


def test_max_len_default_is_30():
    """The default max_len changed from 60 → 30 in v0.6.2a2."""
    long_intent = "alpha bravo charlie delta echo foxtrot golf hotel india"
    out = slugify(long_intent)
    assert len(out) <= 30


# ── Regression: integration with extract_skill ───────────────────────


def test_derive_skill_id_uses_new_slugify():
    """`extract_skill::_derive_skill_id` calls `slugify(graph.intent)`
    with the default. After v0.6.2a2 the default produces a 30-char
    cap, so a graph with a long intent must yield a short slug."""
    from resonant_client.orchestration.plan_graph import PlanGraph
    from resonant_client.orchestration.skill_extraction import _derive_skill_id

    g = PlanGraph.new("Build a Python CLI utility wordcount.py at the project root")
    sid = _derive_skill_id(g)
    assert len(sid) <= 30
    assert not sid.startswith("build-a-")
    # The body still carries the topic.
    assert "python" in sid or "cli" in sid or "utility" in sid

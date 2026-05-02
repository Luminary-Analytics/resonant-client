"""Tests for the specialist registry and prompt assembly."""

from __future__ import annotations

import pytest

from resonant_client.orchestration import (
    NodeSpecialization,
    SPECIALISTS,
    assemble_system_prompt,
    filter_tools_for_specialist,
    get_specialist,
)


def test_every_node_specialization_has_a_profile():
    for spec in NodeSpecialization.ALL:
        assert spec in SPECIALISTS, f"{spec} missing from SPECIALISTS registry"


def test_get_specialist_raises_on_unknown():
    with pytest.raises(KeyError):
        get_specialist("bogus")


def test_explore_cannot_edit_files():
    profile = get_specialist(NodeSpecialization.EXPLORE)
    assert "file_write" not in profile.tool_allowlist
    assert "file_edit" not in profile.tool_allowlist
    assert "bash" not in profile.tool_allowlist
    assert "file_read" in profile.tool_allowlist


def test_verify_can_run_bash_for_tests_but_not_edit():
    profile = get_specialist(NodeSpecialization.VERIFY)
    assert "bash" in profile.tool_allowlist
    assert "file_write" not in profile.tool_allowlist
    assert "file_edit" not in profile.tool_allowlist


def test_research_cannot_edit_or_run_shell():
    profile = get_specialist(NodeSpecialization.RESEARCH)
    assert "file_write" not in profile.tool_allowlist
    assert "bash" not in profile.tool_allowlist
    # But CAN browse
    assert "browser_navigate" in profile.tool_allowlist


def test_implement_has_full_edit_powers():
    profile = get_specialist(NodeSpecialization.IMPLEMENT)
    for tool in ("file_write", "file_edit", "file_read", "bash"):
        assert tool in profile.tool_allowlist


def test_plan_specialist_is_read_only():
    """Planner decomposes — never edits."""
    profile = get_specialist(NodeSpecialization.PLAN)
    assert "file_write" not in profile.tool_allowlist
    assert "file_edit" not in profile.tool_allowlist
    assert "bash" not in profile.tool_allowlist


def test_confidence_threshold_is_per_specialization():
    """Different specializations have different confidence thresholds."""
    assert get_specialist(NodeSpecialization.VERIFY).confidence_threshold > \
           get_specialist(NodeSpecialization.IMPLEMENT).confidence_threshold


def test_max_steps_reasonable():
    # implement gets the most steps, explore the fewest
    impl = get_specialist(NodeSpecialization.IMPLEMENT).max_steps
    expl = get_specialist(NodeSpecialization.EXPLORE).max_steps
    assert impl >= expl


# ── Prompt assembly ─────────────────────────────────────────────────────


def test_assemble_system_prompt_includes_all_layers():
    prompt = assemble_system_prompt(
        specialization=NodeSpecialization.IMPLEMENT,
        node_goal="add dark mode toggle",
        intent="ship dark mode",
        project_conventions="# Use Tailwind\n",
        extra_context="prerequisite found prefers-color-scheme works",
    )
    assert "PROJECT CONVENTIONS" in prompt
    assert "Use Tailwind" in prompt
    assert "SPECIALIZATION: IMPLEMENT" in prompt
    assert "Goal:   add dark mode toggle" in prompt
    assert "Intent: ship dark mode" in prompt
    assert "CONTEXT FROM PRIOR NODES" in prompt
    assert "prerequisite found" in prompt


def test_prompt_omits_empty_sections():
    prompt = assemble_system_prompt(
        specialization=NodeSpecialization.EXPLORE,
        node_goal="read settings.py",
        intent="understand the codebase",
    )
    assert "PROJECT CONVENTIONS" not in prompt
    assert "CONTEXT FROM PRIOR NODES" not in prompt
    assert "SPECIALIZATION: EXPLORE" in prompt


# ── Tool filtering ──────────────────────────────────────────────────────


def test_filter_tools_drops_disallowed():
    fake_tools = [
        {"function": {"name": "file_read", "description": "..."}},
        {"function": {"name": "file_write", "description": "..."}},
        {"function": {"name": "bash", "description": "..."}},
    ]
    explore_tools = filter_tools_for_specialist(NodeSpecialization.EXPLORE, fake_tools)
    names = {t["function"]["name"] for t in explore_tools}
    assert names == {"file_read"}

    impl_tools = filter_tools_for_specialist(NodeSpecialization.IMPLEMENT, fake_tools)
    impl_names = {t["function"]["name"] for t in impl_tools}
    assert impl_names == {"file_read", "file_write", "bash"}


def test_filter_handles_malformed_tool_entries():
    fake_tools = [
        {"function": {"name": "file_read"}},
        {"no_function_key": True},
        {"function": {}},  # no name
    ]
    out = filter_tools_for_specialist(NodeSpecialization.EXPLORE, fake_tools)
    assert len(out) == 1
    assert out[0]["function"]["name"] == "file_read"


# ── v0.4.8 (T2.3) — DeepSeek prompt-tuning invariants ─────────────────
#
# These tests pin the FORMAT REMINDER blocks added to the planner /
# verifier prompts. DeepSeek (especially flash) tends to drop the
# JSON envelope unless heavily reinforced; we put the schema reminder
# at the END of the prompt where the model attends most. If a future
# refactor moves it back to the middle or drops it entirely, these
# tests fail.


class TestPlanPromptFormatReminder:
    def test_plan_prompt_has_format_reminder_block(self):
        profile = get_specialist(NodeSpecialization.PLAN)
        assert "FORMAT REMINDER" in profile.system_block

    def test_plan_format_reminder_is_near_the_end(self):
        # FORMAT REMINDER should appear in the LAST third of the prompt.
        # DeepSeek attends to recent tokens; burying the reminder mid-prompt
        # defeats the purpose.
        block = get_specialist(NodeSpecialization.PLAN).system_block
        idx = block.find("FORMAT REMINDER")
        assert idx > len(block) * 0.5, "FORMAT REMINDER must be in the last half of the prompt"

    def test_plan_prompt_forbids_drift_modes(self):
        # The reminder must explicitly forbid the common JSON drift
        # modes seen in cross-model testing: trailing commas, single
        # quotes, comments inside JSON.
        block = get_specialist(NodeSpecialization.PLAN).system_block.lower()
        assert "trailing comma" in block
        assert "single quote" in block
        assert "comment" in block

    def test_plan_prompt_says_json_goes_last(self):
        # Hard-pin the "nothing important after the JSON" instruction —
        # DeepSeek tends to add follow-up prose that wastes tokens.
        block = get_specialist(NodeSpecialization.PLAN).system_block.lower()
        assert "goes last" in block or "wasted tokens" in block


class TestVerifyPromptStructured:
    def test_verify_prompt_has_json_envelope_spec(self):
        # Pre-T2.3 the verify prompt had NO JSON spec; the runner
        # relied on a heuristic prose-fallback. Verify the explicit
        # envelope is now in place.
        block = get_specialist(NodeSpecialization.VERIFY).system_block
        assert '"verdict"' in block
        assert '"findings"' in block
        assert "```json" in block

    def test_verify_prompt_lists_allowed_verdicts(self):
        block = get_specialist(NodeSpecialization.VERIFY).system_block.lower()
        # All three verdicts the parser recognizes must be documented.
        assert "pass" in block
        assert "revise" in block
        assert "blocked" in block

    def test_verify_prompt_has_format_reminder(self):
        profile = get_specialist(NodeSpecialization.VERIFY)
        assert "FORMAT REMINDER" in profile.system_block

    def test_verify_format_reminder_is_near_the_end(self):
        block = get_specialist(NodeSpecialization.VERIFY).system_block
        idx = block.find("FORMAT REMINDER")
        assert idx > len(block) * 0.5, "FORMAT REMINDER must be in the last half"

    def test_verify_still_forbids_edits(self):
        # The original "you may NOT edit files" invariant must survive
        # the prompt restructure — verify is read-only by design.
        block = get_specialist(NodeSpecialization.VERIFY).system_block
        assert "may NOT edit" in block or "may not edit" in block.lower()


# ── v0.4.10 (T2.5) — await_user discoverability invariants ───────────
#
# `await_user` is universally allowed but rarely called by specialists
# unless the prompt teaches them when. T2.5 added an "ESCAPE HATCH"
# block to explore / implement / plan prompts. These tests pin the
# block's presence + the good/bad-use guidance so a future prompt
# refactor can't silently drop it.


class TestAwaitUserDiscoverability:
    def test_explore_mentions_await_user_escape_hatch(self):
        block = get_specialist(NodeSpecialization.EXPLORE).system_block
        assert "ESCAPE HATCH" in block
        assert "await_user" in block

    def test_implement_mentions_await_user_escape_hatch(self):
        block = get_specialist(NodeSpecialization.IMPLEMENT).system_block
        assert "ESCAPE HATCH" in block
        assert "await_user" in block

    def test_plan_mentions_await_user_escape_hatch(self):
        block = get_specialist(NodeSpecialization.PLAN).system_block
        assert "ESCAPE HATCH" in block
        assert "await_user" in block

    def test_explore_gives_concrete_trigger(self):
        # The block must give a quantitative trigger condition. Without
        # it, the model has no signal for WHEN to escape.
        block = get_specialist(NodeSpecialization.EXPLORE).system_block.lower()
        # Trigger phrases that say "after N tool calls" or similar.
        assert "5+ tool calls" in block or "5 tool calls" in block

    def test_explore_distinguishes_good_vs_bad_use(self):
        # Concrete examples are what makes the difference between "tool
        # exists" and "tool gets called." Both good and bad examples
        # should be present.
        block = get_specialist(NodeSpecialization.EXPLORE).system_block.lower()
        assert "good:" in block and "bad:" in block

    def test_implement_distinguishes_good_vs_bad_use(self):
        block = get_specialist(NodeSpecialization.IMPLEMENT).system_block.lower()
        assert "good:" in block and "bad:" in block

    def test_implement_clarifies_user_vs_code_questions(self):
        # The key insight: ask user when the USER cares; read code when
        # the CODE will tell you. Pin this distinction.
        block = get_specialist(NodeSpecialization.IMPLEMENT).system_block.lower()
        assert "user cares" in block or "code will tell" in block

    def test_plan_recommends_asking_before_decomposing(self):
        # For the planner the right time to ask is BEFORE the JSON plan
        # gets generated — guesses at scope produce bad plans.
        block = get_specialist(NodeSpecialization.PLAN).system_block.lower()
        assert "before decomposing" in block

    def test_plan_escape_hatch_lands_before_format_reminder(self):
        # The FORMAT REMINDER must stay LAST (DeepSeek attends to recent
        # tokens for structured output). The ESCAPE HATCH should be
        # before it but still in the last half of the prompt.
        block = get_specialist(NodeSpecialization.PLAN).system_block
        escape_idx = block.find("ESCAPE HATCH")
        format_idx = block.find("FORMAT REMINDER")
        assert escape_idx > 0 and format_idx > 0
        assert escape_idx < format_idx, "ESCAPE HATCH must come before FORMAT REMINDER"

    def test_verify_does_not_get_escape_hatch(self):
        # Verify is a check role; its escape mechanism is the `blocked`
        # verdict, not await_user. Adding the escape-hatch block here
        # would dilute the verify-as-check semantics.
        block = get_specialist(NodeSpecialization.VERIFY).system_block
        assert "ESCAPE HATCH" not in block

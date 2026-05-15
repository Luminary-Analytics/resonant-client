"""Tests for the rigorous-grill (Autonomous Mission) extensions to
``orchestration.grill_me``.

Phase 2 (Autonomous Mission, v0.5.0) extends the standard grill prompt
with extra rules — more questions, binary type-tagged acceptance
criteria, time budget, vision-availability gate. These tests pin the
prompt invariants so a casual prompt edit can't silently degrade the
autonomous loop's convergence-ground-truth contract, and they cover
the parsers that pull `**Time budget:**` and typed
`[bash]/[chrome]/[vision]/[manual]` criteria out of the emitted spec.

The tests are deliberately scoped to invariants (must-have substrings,
must-have parsing behavior) rather than exact prose — small wording
tweaks should not break the suite.
"""
from __future__ import annotations

import pytest

from resonant_client.gui.roadmap import AcceptanceCriterion
from resonant_client.orchestration.grill_me import (
    ExtractedSpec,
    extract_acceptance_criteria,
    extract_spec,
    extract_time_budget,
    format_grill_first_message,
)


# ── Fixtures ────────────────────────────────────────────────────────────


_RIGOROUS_SPEC = """\
## Final spec

**Refined intent:** Add a dark-mode toggle to Settings, themed via a
single CSS variable so existing components don't need per-component
overrides.

**Key assumptions:**
- Pywebview app, single-window
- Existing styles live in static/styles.css

**In scope:**
- Toggle in Settings
- CSS variable theming
- Persistence across restart

**Out of scope:**
- System-theme follow
- Per-page overrides

**Time budget:** 4h

**Technical constraints:**
- No new runtime deps
- Must not regress existing CSS perf budget

**Acceptance criteria:**
- `[bash]` `pytest tests/test_settings.py` exits 0
- `[bash]` `grep -c 'data-theme' static/styles.css` output > 5
- `[chrome]` Settings page shows a "Theme" dropdown with at least Light and Dark
- `[vision]` After selecting Dark, page background renders near-black, not just an inverted icon
- `[manual]` Verify ANSI colours in TUI mode haven't regressed

**Open risks:**
- WebKit theming on Windows
- Persistence interaction with new-user flow
"""


_LEGACY_FREEFORM_SPEC = """\
## Final spec

**Refined intent:** A standard interactive spec without rigorous-mode
extras.

**Key assumptions:**
- Whatever

**In scope:**
- The thing

**Out of scope:**
- Other things

**Acceptance criteria:**
- The thing should work
- Performance should be good
"""


# ── Prompt invariants for rigorous mode ─────────────────────────────────


class TestRigorousPromptInvariants:
    """Guard rails on the rigorous-mode prompt addendum.

    These do NOT pin exact wording — only that the key behavioral
    promises (more questions, binary criteria, all four type tags,
    minimum count, time-budget question, format additions) survive
    future prompt edits.
    """

    def test_autonomous_flag_changes_prompt(self):
        std = format_grill_first_message("build a thing", autonomous=False)
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert std != rigorous
        assert len(rigorous) > len(std)

    def test_rigorous_pushes_higher_question_count(self):
        # The addendum overrides "5–15" with "10–25"
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert "10" in rigorous
        assert "25" in rigorous

    def test_rigorous_demands_binary_criteria(self):
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert "binary" in rigorous.lower()

    def test_rigorous_lists_all_four_type_tags(self):
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert "[bash]" in rigorous
        assert "[chrome]" in rigorous
        assert "[vision]" in rigorous
        assert "[manual]" in rigorous

    def test_rigorous_demands_minimum_four_criteria(self):
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        # The instruction text specifies "4" criteria; pin the digit
        assert "4" in rigorous
        # …and pins the relationship as a floor (minimum / at least)
        lower = rigorous.lower()
        assert "minimum" in lower or "at least" in lower

    def test_rigorous_asks_for_time_budget(self):
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert "Time budget" in rigorous
        # All preset options the design document committed to should
        # appear so the model can present them verbatim.
        for option in ("1h", "4h", "6h", "8h", "12h", "24h", "48h"):
            assert option in rigorous, f"missing budget option {option}"
        assert "full auto" in rigorous

    def test_rigorous_includes_time_budget_subsection_marker(self):
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert "**Time budget:**" in rigorous

    def test_rigorous_calls_out_measure_twice(self):
        # The design principle should survive prompt edits.
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert "Measure twice" in rigorous or "measure twice" in rigorous

    def test_standard_mode_omits_rigorous_addendum(self):
        std = format_grill_first_message("build a thing", autonomous=False)
        # Hallmarks of the rigorous block must not leak into the
        # standard flow — interactive Mission users shouldn't see the
        # autonomous-only rules.
        assert "Autonomous Mission" not in std
        assert "Time budget" not in std
        assert "10–25" not in std


class TestRule0InterviewerNotImplementer:
    """v0.6.3a1 — F1 fix. The v0.6.1 field run found that the grill
    agent, given a concrete one-shot instruction, would recognize it
    as "not a feature" and just self-execute the work in the chat
    session — leaving the mission stuck in `drafting` forever and
    bypassing the build/verify/skill-extraction loop entirely.

    Rule 0 forbids self-execution and routes one-shot tasks through a
    minimal spec instead. These tests pin the rule's invariants in
    BOTH modes (it lives in the shared base prompt).
    """

    def test_rule0_present_in_standard_mode(self):
        std = format_grill_first_message("build a thing", autonomous=False)
        assert "Rule 0" in std
        assert "INTERVIEWER" in std
        assert "IMPLEMENTER" in std

    def test_rule0_present_in_rigorous_mode(self):
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        assert "Rule 0" in rigorous
        assert "INTERVIEWER" in rigorous

    def test_rule0_forbids_mutating_tools(self):
        std = format_grill_first_message("build a thing", autonomous=False)
        # The rule must explicitly name the tools the agent must not use
        # to do the work itself.
        assert "file_edit" in std
        assert "file_write" in std
        assert "bash" in std

    def test_rule0_allows_readonly_research_tools(self):
        std = format_grill_first_message("build a thing", autonomous=False)
        # Codebase research must still be permitted — the rule should
        # name the read-only tools as allowed.
        assert "glob" in std
        assert "file_read" in std
        assert "grep" in std

    def test_rule0_covers_concrete_one_shot_path(self):
        std = format_grill_first_message("build a thing", autonomous=False)
        # The one-shot path: don't refuse, don't self-execute, emit a
        # minimal spec.
        assert "one-shot" in std.lower()
        assert "minimal" in std.lower()

    def test_rule0_names_the_stuck_drafting_failure_mode(self):
        std = format_grill_first_message("build a thing", autonomous=False)
        # The rule should explain WHY self-execution is bad — it dead-
        # ends the mission in drafting.
        assert "drafting" in std.lower()

    def test_rigorous_r3_has_one_shot_exception(self):
        rigorous = format_grill_first_message("build a thing", autonomous=True)
        # R3's 4-criterion floor must carve out one-shot tasks so it
        # doesn't contradict Rule 0.
        assert "Exception" in rigorous
        # The exception points back at Rule 0.
        assert rigorous.count("Rule 0") >= 2

    def test_standard_mode_has_no_four_criterion_floor(self):
        # Sanity: the 4-criterion floor is rigorous-only, so a one-shot
        # task in standard mode was never blocked by it. Rule 0's
        # minimal-spec guidance still applies.
        std = format_grill_first_message("build a thing", autonomous=False)
        assert "Minimum of 4 binary acceptance criteria" not in std


class TestRigorousPromptSharpening:
    """v0.5.2 — pin the sharpening rules added after the v0.5.1 GA
    smoke. The wordcount mission revealed that criteria were
    silently testing existence ("does it run?") instead of behavior
    ("does it produce the right output?"), so both flash and pro
    "passed" with non-equivalent outputs that wouldn't have
    satisfied a rigorous user. The sharpened prompt:
    - Explicitly demands behavior-testing criteria over existence
    - Requires concrete input/output examples for any output-producing
      feature
    - Splits the 4-criterion floor into 4 DISTINCT aspect categories
    - Adds an edge-case-probing minimum
    - Asks for greenfield-vs-refactor distinction up front
    - Caps question style to 1-2 sentences
    """

    def _rigorous(self) -> str:
        return format_grill_first_message("build a thing", autonomous=True)

    def test_demands_behavior_over_existence(self):
        # The headline sharpening rule.
        rigorous = self._rigorous()
        lower = rigorous.lower()
        assert "behavior" in lower
        # Plus contrasted with existence
        assert "existence" in lower

    def test_includes_good_vs_bad_criterion_examples(self):
        # The prompt needs to show concrete examples of weak criteria
        # (passes-by-existence) vs strong criteria (tests output).
        rigorous = self._rigorous()
        # `output ==` is the key form for behavior tests
        assert "output ==" in rigorous

    def test_splits_four_criteria_into_distinct_aspects(self):
        rigorous = self._rigorous()
        lower = rigorous.lower()
        # The 4 aspects from R3 (sharpened):
        # 1. Happy path with concrete output
        # 2. Error / edge-case behavior
        # 3. Code-quality or constraint check
        # 4. Regression guard or integration check
        assert "happy path" in lower
        assert "edge" in lower or "error" in lower
        assert "constraint" in lower or "code-quality" in lower or \
               "regression" in lower

    def test_edge_case_probing_minimum(self):
        # R1 (sharpened) — at least 3 questions probe edge cases / failure modes
        rigorous = self._rigorous()
        lower = rigorous.lower()
        assert "edge case" in lower or "failure mode" in lower
        # Concrete examples — what should happen when input is empty,
        # invalid, etc.
        assert "empty" in lower or "invalid" in lower

    def test_greenfield_vs_refactor_question_required(self):
        # R5 (NEW) — affects planner-selection downstream
        rigorous = self._rigorous()
        lower = rigorous.lower()
        assert "greenfield" in lower
        assert "refactor" in lower or "extension" in lower or "extends" in lower

    def test_concrete_output_example_probing(self):
        # R4 (NEW) — for any feature that produces output
        rigorous = self._rigorous()
        # The pattern: "input X → output Y" — ask for concrete examples
        lower = rigorous.lower()
        assert "concrete" in lower or "example" in lower
        # And mentions the specific INPUT → OUTPUT pattern
        assert "input" in lower

    def test_question_style_1_to_2_sentences(self):
        # R8 (NEW) — keep questions tight
        rigorous = self._rigorous()
        lower = rigorous.lower()
        assert "1-2 sentences" in lower or "tight" in lower or \
               "one sitting" in lower

    def test_question_count_target_increased_to_three_edge_case_minimum(self):
        # The "at least 3 of your questions must probe edge cases /
        # failure modes" rule
        rigorous = self._rigorous()
        # Pin the digit; allow either form
        assert "at least 3" in rigorous.lower() or "3 of your" in rigorous.lower()


class TestRigorousQuestionExemplar:
    """v0.5.7a5 — pin the question-format exemplar codified from
    linux-bridge field-observation #12. The 5-beat pattern
    (acknowledge → bridge → options → recommend → invite override)
    drove all 27 grill questions in the linux-bridge run to a
    consistent 5/5 rating; we want it to survive prompt edits."""

    def _rigorous(self):
        return format_grill_first_message("build a thing", autonomous=True)

    def test_exemplar_section_present(self):
        rigorous = self._rigorous()
        assert "EXEMPLAR" in rigorous

    def test_acknowledges_5_beat_pattern(self):
        rigorous = self._rigorous().lower()
        # All five beats by name
        for beat in ("acknowledge", "bridge", "options", "recommend", "override"):
            assert beat in rigorous, f"missing 5-beat keyword: {beat}"

    def test_concrete_example_present(self):
        # The exemplar must have an actual rendered example so the
        # model can pattern-match. A bare bullet list of beats isn't
        # enough — there's a concrete (a/b/c) example with a
        # recommendation.
        rigorous = self._rigorous()
        # Look for the recommendation phrasing the example uses.
        assert "Recommendation:" in rigorous or "recommendation:" in rigorous.lower()

    def test_active_scope_narrowing_called_out(self):
        # The synthesis-confirmation pattern (linux-bridge Q5b) is
        # explicitly named in the exemplar.
        rigorous = self._rigorous().lower()
        assert "synthesis-confirmation" in rigorous or "scope-narrowing" in rigorous

    def test_no_filler_rule_present(self):
        # The exemplar's "skip 'Great question!'" guidance is part
        # of why the linux-bridge run scored consistently — pin it.
        rigorous = self._rigorous()
        assert "filler" in rigorous.lower() or "Great question" in rigorous


# ── Vision-availability gate ────────────────────────────────────────────


class TestVisionAvailabilityGate:
    """When the host doesn't have a vision model, `[vision]` criteria
    can't be validated, so the prompt must steer the model away."""

    def test_vision_available_does_not_warn(self):
        rigorous = format_grill_first_message(
            "build a thing", autonomous=True, vision_available=True
        )
        assert "Vision model unavailable" not in rigorous

    def test_vision_unavailable_emits_warning(self):
        rigorous = format_grill_first_message(
            "build a thing", autonomous=True, vision_available=False
        )
        assert "Vision model unavailable" in rigorous
        # The note tells the model to NOT emit [vision] criteria.
        assert "Do NOT emit" in rigorous or "do not emit" in rigorous.lower()

    def test_vision_flag_only_applies_in_autonomous_mode(self):
        # vision_available=False with autonomous=False is a no-op —
        # the unavailable note never fires for standard interactive
        # specs (which don't use typed criteria anyway).
        std = format_grill_first_message(
            "build a thing", autonomous=False, vision_available=False
        )
        assert "Vision model unavailable" not in std
        assert "Autonomous Mission" not in std

    def test_vision_available_default_is_true(self):
        # If a caller forgets to pass vision_available, we should
        # default to "available" (the warning is opt-in, not opt-out).
        rigorous = format_grill_first_message(
            "build a thing", autonomous=True
        )
        assert "Vision model unavailable" not in rigorous


# ── Acceptance-criteria parser ──────────────────────────────────────────


class TestExtractAcceptanceCriteria:
    def test_extracts_all_four_types(self):
        criteria = extract_acceptance_criteria(_RIGOROUS_SPEC)
        types = [c.type for c in criteria]
        assert types.count("bash") == 2
        assert types.count("chrome") == 1
        assert types.count("vision") == 1
        assert types.count("manual") == 1
        assert len(criteria) == 5

    def test_returns_list_of_acceptance_criterion_objects(self):
        criteria = extract_acceptance_criteria(_RIGOROUS_SPEC)
        for c in criteria:
            assert isinstance(c, AcceptanceCriterion)
            assert c.passed is None  # Not yet validated
            assert c.evidence == ""  # No evidence yet

    def test_preserves_full_text_after_type_tag(self):
        criteria = extract_acceptance_criteria(_RIGOROUS_SPEC)
        bash_criteria = [c for c in criteria if c.type == "bash"]
        # First bash criterion: preserve the inline command and assertion
        assert any("pytest" in c.text for c in bash_criteria)
        assert any("exits 0" in c.text for c in bash_criteria)
        # Second bash criterion: preserve "output > 5"
        assert any("output > 5" in c.text for c in bash_criteria)

    def test_preserves_quoted_strings_in_chrome_text(self):
        criteria = extract_acceptance_criteria(_RIGOROUS_SPEC)
        chrome = [c for c in criteria if c.type == "chrome"]
        assert len(chrome) == 1
        assert "Theme" in chrome[0].text
        assert "Light" in chrome[0].text
        assert "Dark" in chrome[0].text

    def test_returns_empty_list_for_legacy_freeform_criteria(self):
        # Standard-mode specs don't use type tags. Parser should not
        # half-match — it should return [] cleanly so callers can tell
        # rigorous-mode handoff failed vs. happened.
        criteria = extract_acceptance_criteria(_LEGACY_FREEFORM_SPEC)
        assert criteria == []

    def test_returns_empty_list_when_no_acceptance_section(self):
        spec = "## Final spec\n\n**Refined intent:** Something.\n"
        criteria = extract_acceptance_criteria(spec)
        assert criteria == []

    def test_returns_empty_list_when_section_present_but_empty(self):
        spec = (
            "**Acceptance criteria:**\n\n"
            "**Open risks:**\n- something\n"
        )
        criteria = extract_acceptance_criteria(spec)
        assert criteria == []

    def test_does_not_bleed_into_next_subsection(self):
        # A criterion-shaped line in **Open risks:** must NOT be
        # picked up — the slice should stop at the next bold header.
        spec = (
            "**Acceptance criteria:**\n"
            "- `[bash]` `pytest` exits 0\n"
            "\n"
            "**Open risks:**\n"
            "- `[bash]` This looks like a criterion but is a risk note\n"
        )
        criteria = extract_acceptance_criteria(spec)
        assert len(criteria) == 1
        assert "pytest" in criteria[0].text

    def test_ignores_indented_or_malformed_bullets(self):
        # Sub-bullets and non-`-` markers should be ignored — we keep
        # the parser strict to surface format drift loudly.
        spec = (
            "**Acceptance criteria:**\n"
            "- `[bash]` `pytest` exits 0\n"
            "  - `[bash]` indented sub-bullet — should be ignored\n"
            "* `[bash]` star marker — should be ignored\n"
            "\n"
            "**Open risks:**\n"
        )
        criteria = extract_acceptance_criteria(spec)
        assert len(criteria) == 1

    def test_ignores_unknown_tag(self):
        # `[perf]` is not in CRITERION_TYPES, so the regex shouldn't
        # match — silently dropping rather than crashing keeps the
        # rest of the spec usable.
        spec = (
            "**Acceptance criteria:**\n"
            "- `[bash]` `pytest` exits 0\n"
            "- `[perf]` p95 < 100ms\n"
            "\n"
            "**Open risks:**\n"
        )
        criteria = extract_acceptance_criteria(spec)
        assert len(criteria) == 1
        assert criteria[0].type == "bash"


# ── Time-budget parser ──────────────────────────────────────────────────


class TestExtractTimeBudget:
    def test_extracts_simple_value(self):
        assert extract_time_budget("**Time budget:** 4h\n") == "4h"

    def test_extracts_full_auto(self):
        assert extract_time_budget("**Time budget:** full auto\n") == "full auto"

    def test_extracts_each_preset(self):
        for preset in ("1h", "4h", "6h", "8h", "12h", "24h", "48h", "full auto"):
            spec = f"**Time budget:** {preset}\n"
            assert extract_time_budget(spec) == preset

    def test_returns_empty_when_missing(self):
        assert extract_time_budget("**Refined intent:** No budget here.\n") == ""

    def test_returns_empty_for_empty_string(self):
        assert extract_time_budget("") == ""

    def test_case_insensitive_label(self):
        # The label is case-insensitive; the value is preserved as-is.
        assert extract_time_budget("**time budget:** 8h\n") == "8h"
        assert extract_time_budget("**TIME BUDGET:** 12h\n") == "12h"

    def test_strips_trailing_whitespace_from_value(self):
        assert extract_time_budget("**Time budget:** 4h   \n") == "4h"

    def test_finds_label_in_full_spec(self):
        # The parser must find the label even when surrounded by other
        # subsections (this is the realistic case).
        assert extract_time_budget(_RIGOROUS_SPEC) == "4h"


# ── Integrated extract_spec ─────────────────────────────────────────────


class TestExtractSpecRigorousFields:
    """Ensures `extract_spec` populates the rigorous-mode fields on
    ExtractedSpec when the model emits them, and leaves them empty when
    the spec is a legacy freeform one."""

    def test_populates_time_budget_and_criteria(self):
        msg = "Some preamble paragraph.\n\n" + _RIGOROUS_SPEC
        spec = extract_spec(msg)
        assert spec is not None
        assert spec.time_budget == "4h"
        assert len(spec.acceptance_criteria) == 5

    def test_refined_intent_still_extracted(self):
        # Adding rigorous fields must not regress refined-intent parsing.
        msg = _RIGOROUS_SPEC
        spec = extract_spec(msg)
        assert spec is not None
        assert "dark-mode" in spec.refined_intent.lower() or \
               "dark mode" in spec.refined_intent.lower()

    def test_legacy_spec_has_empty_rigorous_fields(self):
        spec = extract_spec(_LEGACY_FREEFORM_SPEC)
        assert spec is not None
        assert spec.time_budget == ""
        assert spec.acceptance_criteria == []

    def test_no_spec_returns_none(self):
        assert extract_spec("no spec here, just chat") is None
        assert extract_spec("") is None

    def test_extracted_spec_dataclass_default_values(self):
        # Construction with only the legacy required fields should
        # still work — proves the new fields are defaulted.
        s = ExtractedSpec(raw="x", refined_intent="y")
        assert s.time_budget == ""
        assert s.acceptance_criteria == []


class TestExtractSpecTruncationDetection:
    """v0.5.6a2 — the linux-bridge field-observation run had the model
    emit a Final-spec block that got cut off mid-sentence after the
    In-scope bullets, with no `**Acceptance criteria:**` section. The
    autonomous-mission dispatch card rendered anyway and clicking Build
    would have hit `extract_spec()` and raised ValueError.

    These tests pin the parser's behavior for truncated-spec cases so
    the frontend's spec-validity gate (mirror logic in JS) and the
    backend's `build_roadmap_from_spec` path can both rely on the same
    semantics: a spec without typed acceptance criteria is invalid."""

    _TRUNCATED_NO_CRITERIA_SECTION = """\
## Final spec

**Refined intent:** Build a thing.

**Key assumptions:**
- It's greenfield

**In scope:**
- Project scaffold
- Core CLI flags
- Recipe schema validation at load time: malformed recipes fail at app-load
"""

    _TRUNCATED_CRITERIA_HEADER_NO_CRITERIA = """\
## Final spec

**Refined intent:** Build a thing.

**In scope:**
- Project scaffold

**Acceptance criteria:**
"""

    _ONLY_HEADING_NO_BODY = """\
## Final spec

"""

    def test_truncated_no_criteria_section_parses_to_empty_list(self):
        # No Acceptance criteria heading at all → criteria list is empty.
        # Caller (build_roadmap_from_spec / frontend gate) is expected
        # to refuse to dispatch.
        spec = extract_spec(self._TRUNCATED_NO_CRITERIA_SECTION)
        assert spec is not None  # the `## Final spec` block is still detected
        assert spec.acceptance_criteria == []

    def test_criteria_header_but_no_criteria_lines_yields_empty(self):
        # Header is present, no `- [ ] \`[bash]\`...` lines below.
        spec = extract_spec(self._TRUNCATED_CRITERIA_HEADER_NO_CRITERIA)
        assert spec is not None
        assert spec.acceptance_criteria == []

    def test_only_heading_no_body_returns_spec_with_empty_fields(self):
        # Bare `## Final spec\n` with nothing else — defensive case.
        spec = extract_spec(self._ONLY_HEADING_NO_BODY)
        # Either parses to a spec with everything empty, or returns None.
        # Either is valid for the dispatch gate; pin whichever the
        # parser picks so a future refactor must update the gate too.
        if spec is not None:
            assert spec.acceptance_criteria == []
            assert spec.refined_intent == ""

    def test_build_roadmap_from_spec_refuses_truncated_spec(self, tmp_path):
        # Defensive end-to-end check: the backend's roadmap builder
        # should refuse a truncated spec with a clear ValueError. This
        # is the error the user would have seen if they'd clicked the
        # Build button on the un-gated dispatch card.
        from resonant_client.gui.autonomous_session import build_roadmap_from_spec
        with pytest.raises(ValueError, match="no typed acceptance criteria"):
            build_roadmap_from_spec(
                feature="x", intent_id="i",
                spec_markdown=self._TRUNCATED_NO_CRITERIA_SECTION,
                project_path=str(tmp_path),
            )

    def test_build_roadmap_from_spec_refuses_no_final_spec_block(self, tmp_path):
        # Sister case: no `## Final spec` block at all. Different
        # error message but same outcome (refusal).
        from resonant_client.gui.autonomous_session import build_roadmap_from_spec
        with pytest.raises(ValueError, match="Final spec"):
            build_roadmap_from_spec(
                feature="x", intent_id="i",
                spec_markdown="just chat, no spec here",
                project_path=str(tmp_path),
            )

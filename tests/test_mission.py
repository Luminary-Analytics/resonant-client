"""
Tests for the Mission feature (Phase 1 of long-running-agents).

Covers:
- The grill-me prompt (interviewer persona, spec sentinel format)
- Spec extraction (regex strictness, refined-intent paragraph, edge cases)
- Project-context injection into the grill prompt
- SessionRecord mission state lifecycle (start, advance, exit, persistence)
"""
from __future__ import annotations


from resonant_client.gui.sessions import SessionRecord
from resonant_client.orchestration.grill_me import (
    GRILL_ME_PROMPT,
    extract_spec,
    format_grill_first_message,
)


# ---------------------------------------------------------------------------
# format_grill_first_message
# ---------------------------------------------------------------------------

class TestFormatGrillFirstMessage:
    def test_includes_persona_prompt(self):
        msg = format_grill_first_message("add dark mode")
        assert "interview" in msg.lower()
        assert "## Final spec" in msg, "must teach the model the spec format"
        assert "one question at a time" in msg.lower()

    def test_seed_appended_at_end(self):
        msg = format_grill_first_message("add dark mode toggle")
        assert msg.endswith("add dark mode toggle")

    def test_empty_seed_falls_back_gracefully(self):
        # An empty seed shouldn't break the prompt — the model still gets
        # the persona, just told the user didn't describe anything.
        msg = format_grill_first_message("")
        assert GRILL_ME_PROMPT in msg
        assert "did not provide" in msg.lower()

    def test_whitespace_seed_treated_as_empty(self):
        msg = format_grill_first_message("   \n\t  ")
        assert "did not provide" in msg.lower()


# ---------------------------------------------------------------------------
# extract_spec — happy path
# ---------------------------------------------------------------------------

_FULL_SPEC = """
Some preamble from the model summarizing the conversation so far.

## Final spec

**Refined intent:** Add a dark-mode toggle to the Settings page that
respects system preference by default and persists the user's choice
across sessions.

**Key assumptions:**
- The current theme system uses CSS variables in `styles.css`
- No existing user-preference store; we'll reuse `settings.json`

**In scope:**
- Toggle UI in Settings
- CSS variable swap for dark palette
- Persistence to `~/.resonant/settings.json`

**Out of scope:**
- Per-project theme overrides
- Custom themes beyond light/dark

**Technical constraints:**
- Must not require pywebview to reload
- Existing CSS-variable conventions must be preserved

**Acceptance criteria:**
- Toggle persists across app restarts
- Default reflects OS-level prefers-color-scheme

**Open risks:**
- syntax-highlighting theme may not adapt cleanly
"""


class TestExtractSpec:
    def test_returns_spec_when_present(self):
        result = extract_spec(_FULL_SPEC)
        assert result is not None
        assert result.raw.startswith("## Final spec")
        assert "Add a dark-mode toggle" in result.refined_intent

    def test_refined_intent_is_just_the_paragraph(self):
        result = extract_spec(_FULL_SPEC)
        # Refined intent should be the **Refined intent** paragraph only —
        # nothing from the assumptions / scope / etc. blocks.
        assert "Key assumptions" not in result.refined_intent
        assert "In scope" not in result.refined_intent
        # And it should contain the actual intent text.
        assert "dark-mode toggle" in result.refined_intent.lower()
        assert "settings page" in result.refined_intent.lower()

    def test_returns_none_when_no_spec(self):
        text = "Here's my plan: I'll do the thing. Q1: what color? Q2: when?"
        assert extract_spec(text) is None

    def test_returns_none_for_empty_string(self):
        assert extract_spec("") is None

    def test_returns_none_for_partial_match(self):
        # A close-but-not-exact heading shouldn't fire — the regex requires
        # the exact wording "Final spec" as a top-level heading.
        text = "## Final specs (plural)\n\n**Refined intent:** ..."
        assert extract_spec(text) is None

    def test_does_not_fire_on_inline_mention(self):
        # The model talking ABOUT specs in normal prose shouldn't trigger.
        text = "I'd recommend writing a final spec when we're done. ## Plan\n..."
        assert extract_spec(text) is None

    def test_handles_carriage_returns(self):
        # Some backends emit \r\n line endings — should still match.
        windows_lines = _FULL_SPEC.replace("\n", "\r\n")
        result = extract_spec(windows_lines)
        # Note: our regex uses \n via re.MULTILINE, so \r\n input should
        # still match. If it doesn't, this test catches that regression.
        assert result is not None


# ---------------------------------------------------------------------------
# extract_spec — fallback behavior when format isn't perfect
# ---------------------------------------------------------------------------

class TestExtractSpecFallbacks:
    def test_no_refined_intent_field_uses_first_lines(self):
        # Model emits the heading but forgets the **Refined intent:** label.
        text = """
## Final spec

This is just a paragraph describing what's being built without using
the bold-label format we asked for.

Other content follows.
"""
        result = extract_spec(text)
        assert result is not None
        assert "paragraph describing" in result.refined_intent

    def test_refined_intent_with_multiline_paragraph(self):
        text = """
## Final spec

**Refined intent:** Build a system that does X.
It should also handle Y.
This is all one logical paragraph.

**Key assumptions:**
- ...
"""
        result = extract_spec(text)
        assert result is not None
        # Multi-line refined intent should be captured up to the next
        # **bold** field, not just the first line.
        assert "X" in result.refined_intent
        assert "Y" in result.refined_intent
        assert "Key assumptions" not in result.refined_intent

    def test_spec_at_end_of_message(self):
        # Common case: model writes a transitional sentence then the spec.
        text = "Got it. Here's what I think we're building:\n\n## Final spec\n\n**Refined intent:** Foo."
        result = extract_spec(text)
        assert result is not None
        assert "Foo" in result.refined_intent

    def test_refined_intent_uppercase_label_matches(self):
        # v0.3.2 — Qwen3 was emitting `**REFINED INTENT:**` (all caps).
        # Regex was case-sensitive, so we fell back to "first paragraph"
        # heuristic. Now case-insensitive, the explicit label wins.
        text = (
            "## Final spec\n\n"
            "**REFINED INTENT:** Build a thing that handles Z.\n\n"
            "**KEY ASSUMPTIONS:**\n- foo\n"
        )
        result = extract_spec(text)
        assert result is not None
        assert "Build a thing that handles Z" in result.refined_intent
        assert "KEY ASSUMPTIONS" not in result.refined_intent

    def test_refined_intent_titlecase_label_matches(self):
        text = (
            "## Final spec\n\n"
            "**Refined Intent:** Add a slash command.\n\n"
            "**Key assumptions:**\n- foo\n"
        )
        result = extract_spec(text)
        assert result is not None
        assert "Add a slash command" in result.refined_intent


# ---------------------------------------------------------------------------
# Prompt content — v0.3.2 reminders & rules
# ---------------------------------------------------------------------------

class TestGrillPromptContent:
    def test_format_reminder_present(self):
        # Cross-model testing showed Qwen3 drifting to UPPERCASE labels and
        # stopping the spec mid-message. The format reminder block at the
        # end teaches the model the parser is strict about heading + labels.
        msg = format_grill_first_message("anything")
        assert "Format reminders" in msg
        assert "## Final spec" in msg
        assert "**bold:**" in msg or "bold" in msg.lower()

    def test_partial_existence_rule_present(self):
        # Qwen3 abandoned an interview when it discovered the feature was
        # partially shipped. Rule #9 says "describe the delta, don't bail."
        msg = format_grill_first_message("anything")
        assert "partially exists" in msg or "partly built" in msg


# ---------------------------------------------------------------------------
# Project-context injection — Tier-1 fix #4
# ---------------------------------------------------------------------------

class TestProjectContextInjection:
    def test_resonant_md_content_included_when_present(self, tmp_path):
        # Drop a RESONANT.md in the fake project root and confirm the
        # interviewer prompt picks it up so the model doesn't claim
        # "this is a CLI app" or other wrong assumptions.
        (tmp_path / "RESONANT.md").write_text(
            "# My desktop GUI app\n\nBuilt with pywebview + Starlette.\n",
            encoding="utf-8",
        )
        msg = format_grill_first_message("add foo", project_path=str(tmp_path))
        assert "desktop GUI app" in msg
        assert "pywebview" in msg
        assert "Project context" in msg

    def test_falls_back_to_agents_md(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text(
            "Use Python 3.11+. Tests run via pytest.\n",
            encoding="utf-8",
        )
        msg = format_grill_first_message("add foo", project_path=str(tmp_path))
        assert "Python 3.11+" in msg

    def test_no_context_file_still_produces_prompt(self, tmp_path):
        msg = format_grill_first_message("add foo", project_path=str(tmp_path))
        # Persona is still there even without context.
        assert "interview" in msg.lower()
        assert "## Final spec" in msg
        # Mentions that no context file was found so the model checks itself.
        assert "No RESONANT.md" in msg or "AGENTS.md" in msg

    def test_invalid_project_path_does_not_crash(self):
        msg = format_grill_first_message("add foo", project_path="/nonexistent/path")
        assert "interview" in msg.lower()

    def test_project_path_optional(self):
        # Calling without project_path should still work (back-compat for
        # callers not yet wired through).
        msg = format_grill_first_message("add foo")
        assert "add foo" in msg


# ---------------------------------------------------------------------------
# Tightened grill rules — Tier-1 fix #3
# ---------------------------------------------------------------------------

class TestGrillPromptRules:
    def test_glob_first_rule_present(self):
        # The model is told to inspect with glob before grepping. Guards
        # against the regression we observed in the v0.3 smoke test where
        # the model wasted 7 grep calls hitting nothing.
        msg = format_grill_first_message("foo", project_path=None)
        assert "glob" in msg.lower()
        assert "grep" in msg.lower()

    def test_zero_grep_stop_rule_present(self):
        msg = format_grill_first_message("foo", project_path=None)
        # Specific phrasing that tells the model when to stop searching
        # and ask the user instead.
        assert "three consecutive" in msg.lower() or "stop searching" in msg.lower()

    def test_trust_project_context_rule_present(self):
        # Tier-1 fix #4 also added a "trust the project context" rule
        # to prevent the "this is a CLI app" hallucination.
        msg = format_grill_first_message("foo", project_path=None)
        assert "Project context" in msg


# ---------------------------------------------------------------------------
# SessionRecord mission_state lifecycle
# ---------------------------------------------------------------------------

class TestSessionRecordMissionState:
    def test_default_session_is_not_mission(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        assert rec.is_mission is False
        assert rec.mission_state is None
        assert rec.mission_phase == ""

    def test_start_mission_sets_drafting_phase(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.start_mission("add a /export command")
        assert rec.is_mission is True
        assert rec.mission_phase == "drafting"
        assert rec.mission_state["seed_feature"] == "add a /export command"
        assert "started_at" in rec.mission_state

    def test_advance_phase_keeps_seed_feature(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.start_mission("add foo")
        rec.advance_mission_phase(
            "planning_dispatched",
            spec_markdown="## Final spec\n...",
            intent_id="abc-123",
        )
        assert rec.mission_phase == "planning_dispatched"
        # Seed feature must survive phase transitions — the run-card and
        # sidebar both rely on it being stable across the lifecycle.
        assert rec.mission_state["seed_feature"] == "add foo"
        assert rec.mission_state["spec_markdown"].startswith("## Final spec")
        assert rec.mission_state["intent_id"] == "abc-123"

    def test_exit_mission_marks_exited(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.start_mission("add foo")
        rec.exit_mission()
        assert rec.mission_phase == "exited"
        assert "exited_at" in rec.mission_state
        # is_mission stays True after exit — the session is still a mission,
        # just an inactive one. Sidebar uses this to show under "Missions"
        # with a dim/inactive style.
        assert rec.is_mission is True

    def test_exit_on_non_mission_session_is_noop(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.exit_mission()  # should not raise
        assert rec.mission_state is None

    def test_round_trip_through_to_dict_from_dict(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.start_mission("add foo")
        rec.advance_mission_phase("planning_dispatched", spec_markdown="spec")
        data = rec.to_dict()

        rec2 = SessionRecord.from_dict(data)
        assert rec2.is_mission is True
        assert rec2.mission_phase == "planning_dispatched"
        assert rec2.mission_state["seed_feature"] == "add foo"
        assert rec2.mission_state["spec_markdown"] == "spec"

    def test_summary_includes_mission_state(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.start_mission("add foo")
        summary = rec.to_summary()
        # Sidebar pulls `mission_state` out of the summary to render the
        # Missions group + phase glyph. Without this field the row would
        # appear in the regular project tree, not under Missions.
        assert "mission_state" in summary
        assert summary["mission_state"]["phase"] == "drafting"


# ---------------------------------------------------------------------------
# Resume affordance (B4) — phase transitions out of exited
# ---------------------------------------------------------------------------

class TestResumeMission:
    def test_resume_with_no_spec_returns_to_drafting(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.start_mission("add foo")
        rec.exit_mission()
        assert rec.mission_phase == "exited"
        # Resume path: no intent_id captured, drop back to drafting.
        rec.advance_mission_phase("drafting")
        rec.mission_state.pop("exited_at", None)
        assert rec.mission_phase == "drafting"
        assert "exited_at" not in rec.mission_state
        assert rec.mission_state["seed_feature"] == "add foo"

    def test_resume_after_dispatch_returns_to_planning(self):
        rec = SessionRecord(session_id="x", project_path="/tmp/proj")
        rec.start_mission("add foo")
        rec.advance_mission_phase("planning_dispatched", intent_id="abc-123")
        rec.exit_mission()
        assert rec.mission_phase == "exited"
        # Resume path: intent already dispatched, return to planning.
        rec.advance_mission_phase("planning_dispatched")
        rec.mission_state.pop("exited_at", None)
        assert rec.mission_phase == "planning_dispatched"
        # Intent id is preserved across exit + resume so the planner
        # doesn't get re-dispatched.
        assert rec.mission_state["intent_id"] == "abc-123"
        assert "exited_at" not in rec.mission_state

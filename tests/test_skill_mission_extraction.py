"""Tests for v0.6.0a2 — autonomous-mission-iter skill extraction.

Uses the v0.5.17 streaming-stub harness to drive the extractor
deterministically without a real backend. Covers the full pipeline:
threshold heuristic → prompt build → backend call → response parse →
Skill construction + save.

The autonomous_loop integration (the call site in _run_full_reflect)
is verified separately via test_session_skill_extraction_integration
(future v0.6.0a2 follow-up if a deeper integration test is needed —
for now the unit tests + the deterministic extraction module are the
load-bearing coverage).
"""
from __future__ import annotations


import pytest

from resonant_client.orchestration.skill_mission_extraction import (
    DEFAULT_EXTRACTOR_MAX_TOKENS,
    NO_SKILL_SENTINEL,
    IterContext,
    _build_skill_from_extraction,
    build_extractor_user_prompt,
    extract_skill_from_iter,
    parse_extractor_response,
    should_extract_from_iter,
)
from resonant_client.orchestration.skills import load_skill
from tests.streaming_stub import StreamingBackend, done, error, text_delta


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "fakeproj"
    project.mkdir()
    return project


def _make_ctx(**overrides) -> IterContext:
    """Build a sensible default IterContext; override any field."""
    defaults = dict(
        roadmap_item_title="Scaffold Tauri+Svelte launcher",
        roadmap_item_description="Set up the project with Tauri v2 + Svelte 5.",
        iter_count=3,
        intent_id="test-intent",
        project_path="/tmp/proj",
        outcome_verdict="satisfied",
        outcome_summary="Created Tauri scaffold; resolved title-position quirk.",
        pass_result_bash_passed=4,
        pass_result_bash_failed=0,
        pass_result_vision_passed=0,
        pass_result_vision_failed=0,
        decision_request_resolved=False,
        verdict_overridden=False,
    )
    defaults.update(overrides)
    return IterContext(**defaults)


# ── Threshold heuristic ────────────────────────────────────────────────


class TestShouldExtractFromIter:
    def test_satisfied_with_passes_returns_true(self):
        assert should_extract_from_iter(_make_ctx()) is True

    def test_non_satisfied_verdict_returns_false(self):
        for verdict in ("continue", "blocked", "stuck"):
            ctx = _make_ctx(outcome_verdict=verdict)
            assert should_extract_from_iter(ctx) is False, verdict

    def test_overridden_verdict_returns_false(self):
        # Even verdict=satisfied: if the daemon overrode it, the iter
        # didn't actually succeed.
        ctx = _make_ctx(verdict_overridden=True)
        assert should_extract_from_iter(ctx) is False

    def test_trivial_iter_returns_false(self):
        # Only 1 criterion passed total — too trivial for a skill.
        ctx = _make_ctx(pass_result_bash_passed=1, pass_result_vision_passed=0)
        assert should_extract_from_iter(ctx) is False

    def test_zero_criteria_passed_returns_false(self):
        ctx = _make_ctx(pass_result_bash_passed=0, pass_result_vision_passed=0)
        assert should_extract_from_iter(ctx) is False

    def test_vision_criteria_count_toward_threshold(self):
        # 1 bash + 1 vision = 2 passed → above threshold.
        ctx = _make_ctx(pass_result_bash_passed=1, pass_result_vision_passed=1)
        assert should_extract_from_iter(ctx) is True


# ── Prompt builder ─────────────────────────────────────────────────────


class TestBuildExtractorUserPrompt:
    def test_includes_roadmap_item(self):
        ctx = _make_ctx(roadmap_item_title="Build the X")
        prompt = build_extractor_user_prompt(ctx)
        assert "Build the X" in prompt

    def test_includes_outcome_summary(self):
        ctx = _make_ctx(outcome_summary="something specific happened")
        prompt = build_extractor_user_prompt(ctx)
        assert "something specific happened" in prompt

    def test_includes_iter_count_and_intent(self):
        ctx = _make_ctx(iter_count=42, intent_id="my-intent")
        prompt = build_extractor_user_prompt(ctx)
        assert "42" in prompt
        assert "my-intent" in prompt

    def test_handles_empty_description(self):
        ctx = _make_ctx(roadmap_item_description="")
        prompt = build_extractor_user_prompt(ctx)
        assert "(no description)" in prompt

    def test_handles_empty_summary(self):
        ctx = _make_ctx(outcome_summary="")
        prompt = build_extractor_user_prompt(ctx)
        assert "(no summary)" in prompt


# ── Response parser ────────────────────────────────────────────────────


class TestParseExtractorResponse:
    def test_no_skill_sentinel_returns_none(self):
        assert parse_extractor_response(NO_SKILL_SENTINEL) is None

    def test_no_skill_sentinel_case_insensitive(self):
        assert parse_extractor_response("(NO SKILL)") is None
        assert parse_extractor_response("(no skill)") is None
        # Sentinel can have trailing text the model added; we anchor
        # on startswith.
        assert parse_extractor_response("(no skill) — nothing reusable") is None

    def test_empty_response_returns_none(self):
        assert parse_extractor_response("") is None
        assert parse_extractor_response("   \n   ") is None

    def test_well_formed_response_parses(self):
        response = """\
---
name: Tauri v2 quirks
description: Title is per-window in v2.
version: 1.0.0
---

# Tauri v2 quirks

Body content here.
"""
        result = parse_extractor_response(response)
        assert result is not None
        fm, body = result
        assert fm["name"] == "Tauri v2 quirks"
        assert fm["description"] == "Title is per-window in v2."
        assert "Body content here." in body

    def test_strips_outer_code_fence(self):
        # Some models wrap the entire response in ``` for quoting.
        response = """```
---
name: X
description: Y
---

Body
```"""
        result = parse_extractor_response(response)
        assert result is not None
        fm, body = result
        assert fm["name"] == "X"
        assert "Body" in body

    def test_missing_frontmatter_returns_none(self):
        # Just a markdown body, no frontmatter → not a valid skill.
        response = "# Just a heading\n\nSome body."
        assert parse_extractor_response(response) is None

    def test_frontmatter_with_empty_body_returns_none(self):
        response = "---\nname: X\n---\n   \n  "
        assert parse_extractor_response(response) is None


# ── _build_skill_from_extraction ───────────────────────────────────────


class TestBuildSkillFromExtraction:
    def test_complete_frontmatter_yields_skill(self):
        ctx = _make_ctx()
        fm = {
            "name": "Tauri v2 quirks",
            "description": "Title is per-window in v2.",
            "triggers": ["tauri", "window"],
        }
        body = "## Symptom\nThing\n\n## What to do\nStuff"
        skill = _build_skill_from_extraction(ctx, fm, body)
        assert skill is not None
        assert skill.name == "Tauri v2 quirks"
        assert skill.description == "Title is per-window in v2."
        assert skill.scope == "project"
        assert skill.created_by == "agent"
        assert skill.pinned is False
        assert skill.success_count == 1
        # Slug derived from name.
        assert skill.id == "tauri-v2-quirks"
        # Tokens populated for matching.
        assert "tauri" in skill.tokens

    def test_missing_name_returns_none(self):
        ctx = _make_ctx()
        skill = _build_skill_from_extraction(
            ctx, {"description": "no name"}, "body",
        )
        assert skill is None

    def test_missing_description_returns_none(self):
        ctx = _make_ctx()
        skill = _build_skill_from_extraction(
            ctx, {"name": "X"}, "body",
        )
        assert skill is None

    def test_string_triggers_normalized_to_list(self):
        # Defensive: model emits triggers as a string instead of a list.
        ctx = _make_ctx()
        skill = _build_skill_from_extraction(
            ctx, {"name": "X", "description": "Y", "triggers": "single"}, "body",
        )
        assert skill is not None
        assert skill.triggers == ["single"]

    def test_empty_triggers_list_ok(self):
        ctx = _make_ctx()
        skill = _build_skill_from_extraction(
            ctx, {"name": "X", "description": "Y", "triggers": []}, "body",
        )
        assert skill is not None
        assert skill.triggers == []

    def test_default_version_when_not_provided(self):
        ctx = _make_ctx()
        skill = _build_skill_from_extraction(
            ctx, {"name": "X", "description": "Y"}, "body",
        )
        assert skill.version == "1.0.0"


# ── End-to-end extract_skill_from_iter ──────────────────────────────────


class TestExtractSkillFromIterEndToEnd:
    def test_threshold_skip_returns_none_without_calling_backend(self, state_home):
        # Below-threshold ctx → no model call.
        backend = StreamingBackend()
        ctx = _make_ctx(outcome_verdict="continue")
        result = extract_skill_from_iter(ctx, backend=backend)
        assert result is None
        assert backend.stream_count == 0

    def test_no_skill_sentinel_returns_none_no_save(self, state_home):
        backend = StreamingBackend(events=[
            text_delta(NO_SKILL_SENTINEL),
            done(),
        ])
        ctx = _make_ctx(project_path=str(state_home))
        result = extract_skill_from_iter(ctx, backend=backend)
        assert result is None
        # Backend was called once.
        assert backend.stream_count == 1

    def test_well_formed_response_saves_skill(self, state_home, project_dir):
        skill_md = """\
---
name: Tauri v2 window config quirks
description: Tauri v2 differs from v1 — title is per-window not top-level.
version: 1.0.0
triggers: [tauri, window-config]
---

# Tauri v2 window config quirks

## Symptom
You set `title` at the top level of `tauri.conf.json` and Tauri
v2 fails to compile because the schema expects it per-window.

## What to do
Move `title` under `app.windows[0].title` instead.

## When NOT to apply
v1 still uses top-level title; check the version first.
"""
        backend = StreamingBackend(events=[
            text_delta(skill_md),
            done(),
        ])
        ctx = _make_ctx(project_path=str(project_dir))
        skill = extract_skill_from_iter(ctx, backend=backend)
        assert skill is not None
        assert skill.id == "tauri-v2-window-config-quirks"
        assert skill.created_by == "agent"
        assert skill.scope == "project"
        # Persisted to disk under the project scope.
        loaded = load_skill(
            "tauri-v2-window-config-quirks",
            scope="project",
            project_path=str(project_dir),
        )
        assert loaded is not None
        assert loaded.created_by == "agent"

    def test_backend_error_returns_none_does_not_raise(self, state_home):
        backend = StreamingBackend(events=[
            error("upstream backend failed"),
        ])
        ctx = _make_ctx(project_path=str(state_home))
        # Should NOT raise — extraction is best-effort.
        result = extract_skill_from_iter(ctx, backend=backend)
        assert result is None

    def test_backend_exception_returns_none_does_not_raise(self, state_home):
        # Backend stream() itself raising (network failure, etc.).
        backend = StreamingBackend(
            raise_on_stream=RuntimeError("network broken"),
        )
        ctx = _make_ctx(project_path=str(state_home))
        result = extract_skill_from_iter(ctx, backend=backend)
        assert result is None

    def test_malformed_response_returns_none(self, state_home):
        # Model emits a body without proper frontmatter → not saved.
        backend = StreamingBackend(events=[
            text_delta("This is just prose without frontmatter."),
            done(),
        ])
        ctx = _make_ctx(project_path=str(state_home))
        result = extract_skill_from_iter(ctx, backend=backend)
        assert result is None

    def test_max_tokens_threaded_to_backend(self, state_home):
        backend = StreamingBackend(events=[text_delta(NO_SKILL_SENTINEL), done()])
        ctx = _make_ctx(project_path=str(state_home))
        extract_skill_from_iter(ctx, backend=backend, max_tokens=2048)
        assert backend.stream_calls[0]["max_tokens"] == 2048

    def test_default_max_tokens(self, state_home):
        backend = StreamingBackend(events=[text_delta(NO_SKILL_SENTINEL), done()])
        ctx = _make_ctx(project_path=str(state_home))
        extract_skill_from_iter(ctx, backend=backend)
        assert backend.stream_calls[0]["max_tokens"] == DEFAULT_EXTRACTOR_MAX_TOKENS

    def test_extractor_uses_focused_system_prompt(self, state_home):
        # The system prompt has the SKILL_EXTRACTOR role + format
        # instructions — verify it's actually being passed through.
        backend = StreamingBackend(events=[text_delta(NO_SKILL_SENTINEL), done()])
        ctx = _make_ctx(project_path=str(state_home))
        extract_skill_from_iter(ctx, backend=backend)
        instructions = backend.stream_calls[0]["instructions_preview"]
        assert "SKILL_EXTRACTOR" in instructions

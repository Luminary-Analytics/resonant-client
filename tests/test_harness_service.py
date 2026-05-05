"""Tests for v0.5.14a1 — harness/service.py coverage.

The harness service was at 15% coverage in the v0.5.11 audit — by far
the lowest harness module. It hosts the shared static helpers that
client shells delegate to (status normalization, text/list/mapping
coercion, output-contract template, role-specific instruction blocks,
project summary, resume prompt).

The static methods are pure functions and trivially testable. The
build_* methods compose static helpers + workspace state. None of
this needed to wait — the gap was simply absence of test investment.

Coverage delta target on resonant_client/harness/service.py: 15% → ~85%.
"""
from __future__ import annotations

import json

import pytest

from resonant_client.harness.service import HarnessService
from resonant_client.harness.state import (
    HarnessWorkspace,
    ProductSpec,
    ProgressState,
    SprintContract,
    EvaluatorReport,
)


# ── Service fixture ────────────────────────────────────────────────────


@pytest.fixture
def service():
    """Service with identity normalize_session_mode/role callbacks so
    we can pass through whatever values tests provide directly."""
    return HarnessService(
        normalize_session_mode=lambda m: m or "chat",
        normalize_session_role=lambda mode, role: role or "planner",
    )


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


# ── normalize_contract_status — alias coverage + role fallbacks ────────


class TestNormalizeContractStatus:
    def test_empty_string_returns_empty(self):
        assert HarnessService.normalize_contract_status("", session_role="planner") == ""

    def test_none_returns_empty(self):
        assert HarnessService.normalize_contract_status(None, session_role="") == ""

    def test_canonical_status_passes_through(self):
        # Canonical statuses (proposed/approved/needs_revision/passed/failed)
        # exist in the alias dict mapping to themselves.
        for s in ("proposed", "approved", "needs_revision", "passed", "failed"):
            assert HarnessService.normalize_contract_status(s, session_role="") == s

    def test_alias_propose_normalizes_to_proposed(self):
        assert HarnessService.normalize_contract_status("propose", session_role="") == "proposed"

    def test_alias_planning_started_normalizes_to_proposed(self):
        assert HarnessService.normalize_contract_status(
            "planning_started", session_role="",
        ) == "proposed"

    def test_alias_active_normalizes_to_approved(self):
        assert HarnessService.normalize_contract_status(
            "active", session_role="",
        ) == "approved"

    def test_alias_repaired_normalizes_to_implemented(self):
        assert HarnessService.normalize_contract_status(
            "repaired", session_role="",
        ) == "implemented"

    def test_alias_block_normalizes_to_failed(self):
        assert HarnessService.normalize_contract_status(
            "block", session_role="",
        ) == "failed"

    def test_alias_uppercase_normalized_via_lower(self):
        # The first line lowercases input; PASSED and Passed both alias.
        assert HarnessService.normalize_contract_status("PASSED", session_role="") == "passed"
        assert HarnessService.normalize_contract_status("Passed", session_role="") == "passed"

    def test_planner_generator_ready_phrase_maps_to_approved(self):
        # Planner-only fallback: phrases like "ready_for_generator_X"
        # that didn't hit the explicit alias dict still normalize via
        # the generic "generator + ready" rule.
        assert HarnessService.normalize_contract_status(
            "spec_ready_for_generator_dispatch", session_role="planner",
        ) == "approved"

    def test_planner_planning_complete_phrase_maps_to_approved(self):
        assert HarnessService.normalize_contract_status(
            "planning_phase_completed_now_locked",
            session_role="planner",
        ) == "approved"

    def test_planner_starts_with_planning_maps_to_proposed(self):
        assert HarnessService.normalize_contract_status(
            "planning_in_progress_meeting_acceptance_checks",
            session_role="planner",
        ) == "proposed"

    def test_complete_token_planner_maps_to_approved(self):
        # The role-specific generic completion mapping.
        assert HarnessService.normalize_contract_status(
            "complete", session_role="planner",
        ) == "approved"

    def test_complete_token_generator_maps_to_implemented(self):
        assert HarnessService.normalize_contract_status(
            "complete", session_role="generator",
        ) == "implemented"

    def test_complete_token_other_role_maps_to_passed(self):
        # Default / evaluator / unknown role falls to "passed".
        assert HarnessService.normalize_contract_status(
            "completed", session_role="evaluator",
        ) == "passed"
        assert HarnessService.normalize_contract_status(
            "done", session_role="",
        ) == "passed"

    def test_unrecognized_status_passes_through(self):
        # If we can't normalize, the original (lowercased + stripped)
        # is returned so callers can decide what to do.
        assert HarnessService.normalize_contract_status(
            "weird_custom_status", session_role="",
        ) == "weird_custom_status"


# ── _truncate_text ─────────────────────────────────────────────────────


class TestTruncateText:
    def test_short_text_unchanged(self):
        assert HarnessService._truncate_text("hi", max_chars=10) == "hi"

    def test_exact_length_unchanged(self):
        assert HarnessService._truncate_text("abcde", max_chars=5) == "abcde"

    def test_long_text_truncated_with_ellipsis(self):
        result = HarnessService._truncate_text("a" * 20, max_chars=10)
        # max_chars = 10 → keep 9 chars + "…" = 10 visible chars total.
        assert result == "a" * 9 + "…"
        assert len(result) == 10

    def test_strips_surrounding_whitespace(self):
        assert HarnessService._truncate_text("  hello  ", max_chars=20) == "hello"

    def test_none_treated_as_empty(self):
        assert HarnessService._truncate_text(None, max_chars=10) == ""

    def test_max_chars_zero_returns_just_ellipsis_or_empty(self):
        # max(0, max_chars - 1) = 0 → empty slice + ellipsis.
        result = HarnessService._truncate_text("aaaa", max_chars=0)
        assert result.endswith("…")


# ── _normalize_string_list ─────────────────────────────────────────────


class TestNormalizeStringList:
    def test_none_returns_empty(self):
        assert HarnessService._normalize_string_list(None) == []

    def test_empty_string_returns_empty(self):
        assert HarnessService._normalize_string_list("") == []

    def test_single_string_wraps_in_list(self):
        assert HarnessService._normalize_string_list("hello") == ["hello"]

    def test_string_stripped(self):
        assert HarnessService._normalize_string_list("  spaced  ") == ["spaced"]

    def test_whitespace_only_string_returns_empty(self):
        assert HarnessService._normalize_string_list("   ") == []

    def test_list_strips_each_item(self):
        result = HarnessService._normalize_string_list(["  a ", "b", " c"])
        assert result == ["a", "b", "c"]

    def test_list_drops_empty_items(self):
        result = HarnessService._normalize_string_list(["x", "", "  ", "y"])
        assert result == ["x", "y"]

    def test_list_coerces_non_strings(self):
        # Numbers, bools coerced via str().
        result = HarnessService._normalize_string_list([1, 2.5, True, None])
        # None is filtered (whitespace-only after strip); others coerced.
        assert "1" in result
        assert "2.5" in result
        assert "True" in result

    def test_tuple_and_set_accepted(self):
        # Method accepts list/tuple/set.
        assert HarnessService._normalize_string_list(("a", "b")) == ["a", "b"]
        # set order isn't guaranteed; check membership.
        result = HarnessService._normalize_string_list({"x", "y"})
        assert sorted(result) == ["x", "y"]

    def test_other_value_falls_to_str_branch(self):
        # An int isn't a list/tuple/set, isn't None or "" — falls to
        # the `str(value).strip()` line.
        assert HarnessService._normalize_string_list(42) == ["42"]


# ── _normalize_string_mapping ──────────────────────────────────────────


class TestNormalizeStringMapping:
    def test_non_dict_returns_empty(self):
        assert HarnessService._normalize_string_mapping(None) == {}
        assert HarnessService._normalize_string_mapping("not a dict") == {}
        assert HarnessService._normalize_string_mapping([1, 2]) == {}

    def test_basic_string_mapping(self):
        result = HarnessService._normalize_string_mapping(
            {"check1": "evidence A", "check2": "evidence B"},
        )
        assert result == {"check1": "evidence A", "check2": "evidence B"}

    def test_strips_whitespace(self):
        result = HarnessService._normalize_string_mapping(
            {"  k  ": "  v  "},
        )
        assert result == {"k": "v"}

    def test_empty_keys_dropped(self):
        result = HarnessService._normalize_string_mapping(
            {"": "no key", "real": "yes"},
        )
        assert result == {"real": "yes"}

    def test_empty_values_dropped(self):
        # Empty stripped values are dropped (not coerced to "").
        result = HarnessService._normalize_string_mapping(
            {"a": "", "b": "  ", "c": "ok"},
        )
        assert result == {"c": "ok"}

    def test_bool_true_becomes_PASS(self):
        result = HarnessService._normalize_string_mapping(
            {"check": True},
        )
        assert result == {"check": "PASS"}

    def test_bool_false_dropped(self):
        # False → "" → dropped.
        result = HarnessService._normalize_string_mapping(
            {"check": False},
        )
        assert result == {}

    def test_non_string_values_coerced(self):
        result = HarnessService._normalize_string_mapping(
            {"count": 42, "ratio": 0.5},
        )
        assert result == {"count": "42", "ratio": "0.5"}


# ── build_output_contract ──────────────────────────────────────────────


class TestBuildOutputContract:
    def test_chat_mode_returns_empty(self, service):
        # The chat session mode short-circuits to empty contract.
        assert service.build_output_contract(
            session_mode="chat", session_role="planner",
        ) == ""

    def test_planner_emits_valid_json_template(self, service):
        out = service.build_output_contract(
            session_mode="sprint", session_role="planner",
        )
        assert "```resonant-harness" in out
        # Extract the JSON block + parse.
        start = out.index("```resonant-harness\n") + len("```resonant-harness\n")
        end = out.rindex("\n```")
        payload = json.loads(out[start:end])
        assert payload["action"] == "planner_update"
        assert "spec" in payload
        assert "sprint_contract" in payload
        assert payload["sprint_contract"]["status"] == "proposed"

    def test_generator_template_includes_handoff_markdown(self, service):
        out = service.build_output_contract(
            session_mode="sprint", session_role="generator",
        )
        start = out.index("```resonant-harness\n") + len("```resonant-harness\n")
        end = out.rindex("\n```")
        payload = json.loads(out[start:end])
        assert payload["action"] == "generator_update"
        assert "handoff_markdown" in payload
        assert payload["sprint_status"] == "implemented"

    def test_evaluator_template_has_verdict_default(self, service):
        out = service.build_output_contract(
            session_mode="sprint", session_role="evaluator",
        )
        start = out.index("```resonant-harness\n") + len("```resonant-harness\n")
        end = out.rindex("\n```")
        payload = json.loads(out[start:end])
        assert payload["action"] == "evaluator_verdict"
        assert payload["verdict"] == "revise"
        assert isinstance(payload["findings"], list)


# ── build_instructions ─────────────────────────────────────────────────


class TestBuildInstructions:
    def test_chat_mode_returns_empty(self, service):
        assert service.build_instructions(
            project_path="/p", session_mode="chat", session_role="any",
        ) == ""

    def test_planner_includes_role_block_and_contract(self, service):
        out = service.build_instructions(
            project_path="/p", session_mode="sprint", session_role="planner",
        )
        assert "SPRINT WORKFLOW" in out
        assert "Session role: planner" in out
        assert "planner session" in out
        # Planner gets the JSON output contract appended.
        assert "resonant-harness" in out

    def test_generator_role_block_no_contract(self, service):
        # Generator role drops the contract block (it's noise for a
        # mostly-edits session).
        out = service.build_instructions(
            project_path="/p", session_mode="sprint", session_role="generator",
        )
        assert "Session role: generator" in out
        assert "generator session" in out
        # No JSON output contract.
        assert "```resonant-harness" not in out

    def test_evaluator_role_includes_contract(self, service):
        out = service.build_instructions(
            project_path="/p", session_mode="sprint", session_role="evaluator",
        )
        assert "Session role: evaluator" in out
        assert "evaluator session" in out
        assert "```resonant-harness" in out


# ── get_summary — workspace integration ────────────────────────────────


class TestGetSummary:
    def test_returns_dict_for_fresh_project(self, service, state_home, project_dir):
        # Fresh project — get_summary returns a dict shape with the
        # expected keys (progress / contract / report / history).
        summary = service.get_summary(str(project_dir))
        assert isinstance(summary, dict)
        # Loose shape checks — we just want to confirm the call
        # exercises read_progress / read_sprint_contract / etc.
        assert "progress" in summary or "current_phase" in str(summary)

    def test_normalizes_contract_status_in_summary(self, service, state_home, project_dir):
        # Pre-populate workspace with a non-canonical status; verify
        # get_summary normalizes it.
        ws = HarnessWorkspace(project_dir)
        ws.ensure_layout()
        ws.write_sprint_contract(SprintContract(
            sprint_id="sp-1", status="ready_to_implement",
        ))
        # Default progress role is whatever ensure_layout writes.
        summary = service.get_summary(str(project_dir))
        # The normalized status should appear in the summary dict
        # somewhere — the exact key depends on implementation, but
        # "approved" (canonical for "ready_to_implement") should be
        # present.
        as_text = json.dumps(summary, default=str)
        assert "approved" in as_text

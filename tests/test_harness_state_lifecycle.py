"""Tests for v0.5.12a3 — harness/state.py lifecycle coverage.

Existing tests/test_harness_state.py covers path resolution + legacy
migration. The actual sprint lifecycle methods — set_active_sprint,
record_evaluator_verdict, set_contract_status, update_*, run-history
append/read, teacher-escalation logging — were untested. The harness
is opt-in (default off post v0.4.0 refocus), but anyone who DOES
opt in needs the lifecycle to work correctly.

Coverage delta target on resonant_client/harness/state.py: 61% → ~95%.

Mirrors the fixtures from test_harness_state.py for consistency.
"""
from __future__ import annotations

import json
import time

import pytest

from resonant_client.harness.state import (
    EvaluatorReport,
    HarnessWorkspace,
    ProductSpec,
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


@pytest.fixture
def ws(state_home, project_dir):
    """A workspace with the layout already created."""
    workspace = HarnessWorkspace(project_dir)
    workspace.ensure_layout()
    return workspace


# ── Round-trip read/write for each artifact ─────────────────────────────


class TestSpecRoundTrip:
    def test_round_trip(self, ws):
        spec = ProductSpec(
            title="Resonant",
            summary="agentic coder",
            user_stories=["as a dev I want X"],
            sprint_order=["sp-1", "sp-2"],
        )
        ws.write_spec(spec)
        loaded = ws.read_spec()
        assert loaded.title == "Resonant"
        assert loaded.summary == "agentic coder"
        assert loaded.user_stories == ["as a dev I want X"]
        assert loaded.sprint_order == ["sp-1", "sp-2"]

    def test_unknown_keys_in_payload_dropped(self, ws):
        # _coerce_dataclass_payload filters unknown keys silently —
        # important for forward-compat when the on-disk schema lags
        # the dataclass definition.
        ws.spec_path.write_text(
            json.dumps({"title": "x", "unknown_field": 42}),
            encoding="utf-8",
        )
        loaded = ws.read_spec()
        assert loaded.title == "x"

    def test_update_spec_partial(self, ws):
        ws.write_spec(ProductSpec(title="orig", summary="orig sum"))
        ws.update_spec(summary="new sum")
        loaded = ws.read_spec()
        assert loaded.title == "orig"  # untouched
        assert loaded.summary == "new sum"

    def test_update_spec_ignores_unknown_keys(self, ws):
        ws.write_spec(ProductSpec(title="x"))
        # update_spec uses hasattr() — unknown keys silently dropped.
        ws.update_spec(title="y", made_up="zzz")
        loaded = ws.read_spec()
        assert loaded.title == "y"
        assert not hasattr(loaded, "made_up")


class TestEvaluatorReportRoundTrip:
    def test_round_trip(self, ws):
        report = EvaluatorReport(
            sprint_id="sp-1", verdict="pass",
            score=0.95, findings=["good"], passed_checks=["c1"],
        )
        ws.write_evaluator_report(report)
        loaded = ws.read_evaluator_report()
        assert loaded.sprint_id == "sp-1"
        assert loaded.verdict == "pass"
        assert loaded.score == 0.95
        assert loaded.findings == ["good"]
        assert loaded.passed_checks == ["c1"]


class TestUpdateMethods:
    def test_update_progress(self, ws):
        # update_progress reads + mutates + writes; missing keys ignored.
        progress = ws.update_progress(
            current_phase="implementation",
            active_sprint_id="sp-7",
            unknown_attr="ignored",
        )
        assert progress.current_phase == "implementation"
        assert progress.active_sprint_id == "sp-7"
        # Persisted on disk too.
        loaded = ws.read_progress()
        assert loaded.current_phase == "implementation"
        assert loaded.active_sprint_id == "sp-7"

    def test_update_sprint_contract(self, ws):
        contract = ws.update_sprint_contract(
            sprint_id="sp-1", status="approved",
        )
        assert contract.status == "approved"
        loaded = ws.read_sprint_contract()
        assert loaded.sprint_id == "sp-1"
        assert loaded.status == "approved"

    def test_update_evaluator_report(self, ws):
        report = ws.update_evaluator_report(verdict="revise", score=0.4)
        assert report.verdict == "revise"
        assert report.score == 0.4


# ── Handoff text I/O ────────────────────────────────────────────────────


class TestHandoff:
    def test_read_when_missing_returns_empty(self, state_home, project_dir):
        ws = HarnessWorkspace(project_dir)
        # No ensure_layout() — handoff path doesn't exist yet.
        assert not ws.handoff_path.exists()
        assert ws.read_handoff() == ""

    def test_write_then_read(self, ws):
        ws.write_handoff("# Handoff\n\nSomething happened.")
        loaded = ws.read_handoff()
        assert "# Handoff" in loaded
        assert "Something happened." in loaded

    def test_write_strips_trailing_whitespace_appends_single_newline(self, ws):
        ws.write_handoff("hello\n\n\n")
        # write_handoff calls .rstrip() + adds one \n.
        assert ws.handoff_path.read_text(encoding="utf-8") == "hello\n"


# ── Run history JSONL append / read ─────────────────────────────────────


class TestRunHistory:
    def test_empty_when_no_file(self, state_home, project_dir):
        ws = HarnessWorkspace(project_dir)
        assert not ws.run_history_path.exists()
        assert ws.read_run_history() == []

    def test_append_then_read(self, ws):
        ws.append_run_event("started", {"foo": 1})
        ws.append_run_event("ended", {"foo": 2})
        events = ws.read_run_history()
        assert len(events) == 2
        assert events[0]["event"] == "started"
        assert events[0]["payload"] == {"foo": 1}
        assert events[1]["event"] == "ended"

    def test_read_with_limit_returns_most_recent(self, ws):
        for i in range(5):
            ws.append_run_event("e", {"i": i})
        events = ws.read_run_history(limit=2)
        assert len(events) == 2
        # The last 2 (i=3, i=4) — read_run_history slices from the end.
        assert events[0]["payload"]["i"] == 3
        assert events[1]["payload"]["i"] == 4

    def test_read_skips_invalid_json_lines(self, ws):
        ws.append_run_event("ok1", {"k": 1})
        # Manually corrupt the file with an invalid line.
        with ws.run_history_path.open("a", encoding="utf-8") as f:
            f.write("not-json\n")
        ws.append_run_event("ok2", {"k": 2})
        events = ws.read_run_history()
        # Bad line dropped; surrounding events still surface.
        assert len(events) == 2
        assert events[0]["event"] == "ok1"
        assert events[1]["event"] == "ok2"

    def test_skips_blank_lines(self, ws):
        with ws.run_history_path.open("a", encoding="utf-8") as f:
            f.write("\n   \n")
        ws.append_run_event("real", {})
        events = ws.read_run_history()
        assert len(events) == 1
        assert events[0]["event"] == "real"

    def test_event_records_include_timestamp(self, ws):
        before = time.time()
        ws.append_run_event("x", {})
        after = time.time()
        event = ws.read_run_history()[0]
        assert before <= event["timestamp"] <= after


# ── Teacher escalation log ──────────────────────────────────────────────


class TestTeacherEscalations:
    def test_empty_when_no_file(self, state_home, project_dir):
        ws = HarnessWorkspace(project_dir)
        assert ws.read_teacher_escalations() == []

    def test_append_then_read(self, ws):
        ws.append_teacher_escalation({"reason": "stuck"})
        ws.append_teacher_escalation({"reason": "ambiguous"})
        rows = ws.read_teacher_escalations()
        assert len(rows) == 2
        assert rows[0]["reason"] == "stuck"

    def test_limit_returns_most_recent(self, ws):
        for i in range(4):
            ws.append_teacher_escalation({"i": i})
        rows = ws.read_teacher_escalations(limit=2)
        assert [r["i"] for r in rows] == [2, 3]

    def test_read_skips_invalid_json(self, ws):
        ws.append_teacher_escalation({"good": 1})
        with ws.teacher_escalations_path.open("a", encoding="utf-8") as f:
            f.write("garbage\n")
        ws.append_teacher_escalation({"good": 2})
        rows = ws.read_teacher_escalations()
        assert len(rows) == 2


# ── set_active_sprint full lifecycle ────────────────────────────────────


class TestSetActiveSprint:
    def test_writes_contract_progress_and_resets_evaluator(self, ws):
        progress, contract = ws.set_active_sprint(
            sprint_id="sp-1",
            feature_name="grill",
            objective="refine spec",
            deliverables=["d1", "d2"],
            acceptance_checks=["c1"],
            evaluator_focus=["f1"],
            target_files=["a.py"],
            target_line_hints=["line 10"],
            validation_commands=["pytest -q"],
            edit_strategy="surgical",
            status="proposed",
            role="planner",
        )

        assert contract.sprint_id == "sp-1"
        assert contract.feature_name == "grill"
        assert contract.deliverables == ["d1", "d2"]

        # Progress reflects new active sprint.
        assert progress.active_sprint_id == "sp-1"
        assert progress.active_role == "planner"
        assert progress.current_phase == "planning"
        assert progress.summary == "refine spec"
        # Cleared from any prior state.
        assert progress.blockers == []
        assert progress.next_steps == []

        # Evaluator report wiped (no stale verdict bleeds through).
        report = ws.read_evaluator_report()
        assert report.sprint_id == "sp-1"
        assert report.verdict == "unknown"
        assert report.score is None
        assert report.findings == []

        # Run history records the transition.
        events = ws.read_run_history()
        assert any(e["event"] == "set_active_sprint" for e in events)

    def test_role_generator_sets_implementation_phase(self, ws):
        progress, _ = ws.set_active_sprint(
            sprint_id="sp-2",
            feature_name="impl",
            objective="ship",
            role="generator",
        )
        # Non-planner roles flip current_phase to implementation.
        assert progress.current_phase == "implementation"
        assert progress.active_role == "generator"

    def test_clears_acceptance_evidence(self, ws):
        # Pre-populate acceptance_evidence; new sprint must clear it.
        ws.update_progress(acceptance_evidence={"old-sprint": "stale"})
        progress, _ = ws.set_active_sprint(
            sprint_id="sp-3", feature_name="x", objective="y",
        )
        assert progress.acceptance_evidence == {}


# ── record_evaluator_verdict ────────────────────────────────────────────


class TestRecordEvaluatorVerdict:
    def _setup_active_sprint(self, ws, sprint_id="sp-42"):
        ws.set_active_sprint(
            sprint_id=sprint_id, feature_name="f", objective="o",
        )

    def test_pass_verdict_marks_contract_passed(self, ws):
        self._setup_active_sprint(ws, sprint_id="sp-42")
        progress, contract, report = ws.record_evaluator_verdict(
            sprint_id="sp-42", verdict="pass",
            findings=["all green"], passed_checks=["c1", "c2"],
            score=1.0,
        )
        assert report.verdict == "pass"
        assert report.score == 1.0
        assert report.findings == ["all green"]
        assert contract.status == "passed"
        assert progress.current_phase == "completed"
        assert progress.active_role == "evaluator"

    def test_revise_verdict_sets_revision_phase_and_next_steps(self, ws):
        self._setup_active_sprint(ws, sprint_id="sp-42")
        progress, contract, _ = ws.record_evaluator_verdict(
            sprint_id="sp-42", verdict="revise",
            required_revisions=["fix bug A", "add test B"],
        )
        assert contract.status == "needs_revision"
        assert progress.current_phase == "revision"
        assert progress.next_steps == ["fix bug A", "add test B"]

    def test_blocked_verdict_marks_failed(self, ws):
        self._setup_active_sprint(ws, sprint_id="sp-42")
        progress, contract, _ = ws.record_evaluator_verdict(
            sprint_id="sp-42", verdict="blocked",
            failed_checks=["c1"],
        )
        assert contract.status == "failed"
        assert progress.current_phase == "blocked"

    def test_verdict_for_different_sprint_does_not_touch_contract(self, ws):
        # If the report is for sprint X but the active contract is sprint Y,
        # the contract status should NOT update (defensive guard).
        self._setup_active_sprint(ws, sprint_id="sp-42")
        _, contract, _ = ws.record_evaluator_verdict(
            sprint_id="other-sprint", verdict="pass",
        )
        # Active contract still on sp-42 with original "proposed" status.
        assert contract.sprint_id == "sp-42"
        assert contract.status == "proposed"

    def test_unknown_verdict_leaves_phase_unchanged(self, ws):
        self._setup_active_sprint(ws, sprint_id="sp-42")
        # Capture current_phase before.
        original_phase = ws.read_progress().current_phase
        progress, _, _ = ws.record_evaluator_verdict(
            sprint_id="sp-42", verdict="weird",  # not in the verdict map
        )
        assert progress.current_phase == original_phase

    def test_records_run_event(self, ws):
        self._setup_active_sprint(ws, sprint_id="sp-42")
        ws.record_evaluator_verdict(
            sprint_id="sp-42", verdict="pass",
        )
        verdict_events = [
            e for e in ws.read_run_history()
            if e["event"] == "evaluator_verdict"
        ]
        assert len(verdict_events) == 1
        assert verdict_events[0]["payload"]["verdict"] == "pass"


# ── set_contract_status ─────────────────────────────────────────────────


class TestSetContractStatus:
    def _seed_contract(self, ws, sprint_id="sp-1"):
        ws.set_active_sprint(
            sprint_id=sprint_id, feature_name="x", objective="y",
        )

    def test_status_proposed_defaults_role_to_planner(self, ws):
        self._seed_contract(ws)
        progress, contract = ws.set_contract_status(status="proposed")
        assert contract.status == "proposed"
        assert progress.current_phase == "planning"
        assert progress.active_role == "planner"

    def test_status_approved_defaults_role_to_generator(self, ws):
        self._seed_contract(ws)
        progress, _ = ws.set_contract_status(status="approved")
        assert progress.current_phase == "implementation"
        assert progress.active_role == "generator"

    def test_status_implemented_routes_to_evaluator(self, ws):
        self._seed_contract(ws)
        progress, _ = ws.set_contract_status(status="implemented")
        assert progress.current_phase == "evaluation"
        assert progress.active_role == "evaluator"

    def test_explicit_role_overrides_default(self, ws):
        self._seed_contract(ws)
        progress, _ = ws.set_contract_status(
            status="approved", role="teacher",
        )
        # Explicit role wins over the default-role-map lookup.
        assert progress.active_role == "teacher"
        assert progress.current_phase == "implementation"

    def test_unknown_status_leaves_phase_unchanged(self, ws):
        self._seed_contract(ws)
        original_phase = ws.read_progress().current_phase
        progress, _ = ws.set_contract_status(status="weird-status")
        assert progress.current_phase == original_phase

    def test_records_run_event(self, ws):
        self._seed_contract(ws)
        ws.set_contract_status(status="approved")
        events = [
            e for e in ws.read_run_history()
            if e["event"] == "contract_status"
        ]
        assert len(events) == 1
        assert events[0]["payload"]["status"] == "approved"

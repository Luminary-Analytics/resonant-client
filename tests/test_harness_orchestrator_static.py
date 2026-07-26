"""Tests for v0.5.14a2 — harness/orchestrator.py static-method coverage.

orchestrator.py was at 28% coverage in the v0.5.11 audit. The static
classification helpers near the bottom of the file are pure functions
that drive WHICH role runs next in the cycle — a regression in
_choose_next_role or _is_generator_ready_contract would silently
misroute the planner/generator/evaluator pipeline.

The active background-cycle methods (_run_cycle, _attempt_role_retry,
_attempt_teacher_recovery) need a stub backend harness and are
deferred to a future alpha. The static helpers + dataclass to_dict
methods are pure and easy to cover here.

Coverage delta target on harness/orchestrator.py: 28% → ~50%.
"""
from __future__ import annotations

import json
from datetime import datetime


from resonant_client.harness.orchestrator import (
    HarnessCycleRun,
    HarnessCycleStep,
    HarnessOrchestrator,
)


# ── HarnessCycleStep.to_dict / to_full_dict ─────────────────────────────


class TestHarnessCycleStepDict:
    def _step(self, **overrides):
        defaults = dict(
            role="planner", backend_type="ollama", model="deepseek",
            status="completed", started_at="2026-05-05T10:00:00",
        )
        defaults.update(overrides)
        return HarnessCycleStep(**defaults)

    def test_to_dict_truncates_result_to_500_chars(self):
        step = self._step(result="x" * 1000)
        out = step.to_dict()
        assert out["result"] == "x" * 500
        assert len(out["result"]) == 500

    def test_to_dict_short_result_unchanged(self):
        step = self._step(result="short result")
        assert step.to_dict()["result"] == "short result"

    def test_to_dict_empty_result_stays_empty(self):
        step = self._step(result="")
        assert step.to_dict()["result"] == ""

    def test_to_dict_includes_all_fields(self):
        step = self._step(
            error="boom", auto_transition="proposed→approved",
            steps=3, role_mode="planner", prechecked=True,
            evaluation_mode="full",
        )
        out = step.to_dict()
        # All declared fields present.
        for key in (
            "role", "backend_type", "model", "status", "started_at",
            "completed_at", "result", "error", "summary_before",
            "summary_after", "auto_transition", "steps",
            "evaluation_mode", "role_mode", "prechecked",
        ):
            assert key in out

    def test_to_full_dict_returns_full_result(self):
        step = self._step(result="x" * 1000)
        out = step.to_full_dict()
        # Full result, NOT truncated.
        assert out["result"] == "x" * 1000


# ── HarnessCycleRun.__post_init__ ──────────────────────────────────────


class TestHarnessCycleRunInit:
    def test_post_init_sets_created_at_when_empty(self):
        run = HarnessCycleRun(
            id="run-1", name="t", project_path="/p",
            objective="o", max_loops=5,
        )
        # created_at should have been auto-populated.
        assert run.created_at != ""
        # Parses as ISO format.
        datetime.fromisoformat(run.created_at)

    def test_post_init_preserves_explicit_created_at(self):
        explicit = "2026-01-01T00:00:00"
        run = HarnessCycleRun(
            id="run-2", name="t", project_path="/p",
            objective="o", max_loops=5, created_at=explicit,
        )
        assert run.created_at == explicit


# ── _is_retryable_failure ──────────────────────────────────────────────


class TestIsRetryableFailure:
    def test_evaluator_with_timeout_token_retryable(self):
        assert HarnessOrchestrator._is_retryable_failure(
            role="evaluator", error="request timed out after 30s",
        ) is True

    def test_generator_with_doom_loop_retryable(self):
        assert HarnessOrchestrator._is_retryable_failure(
            role="generator", error="doom loop detected, aborting",
        ) is True

    def test_planner_role_never_retryable(self):
        # The static method only returns True for evaluator + generator.
        assert HarnessOrchestrator._is_retryable_failure(
            role="planner", error="timed out",
        ) is False

    def test_non_matching_error_not_retryable(self):
        assert HarnessOrchestrator._is_retryable_failure(
            role="evaluator", error="permission denied",
        ) is False

    def test_empty_error_not_retryable(self):
        assert HarnessOrchestrator._is_retryable_failure(
            role="evaluator", error="",
        ) is False

    def test_case_insensitive_match(self):
        # Token matching uses .lower().
        assert HarnessOrchestrator._is_retryable_failure(
            role="generator", error="TIMEOUT WHILE STREAMING",
        ) is True

    def test_syntax_gate_token_retryable(self):
        assert HarnessOrchestrator._is_retryable_failure(
            role="generator",
            error="syntax gate failed: invalid Python",
        ) is True


# ── _summary_signature ─────────────────────────────────────────────────


class TestSummarySignature:
    def test_returns_json_string(self):
        sig = HarnessOrchestrator._summary_signature(
            {"active_sprint_id": "sp-1"},
        )
        # Round-trip parse to confirm it's valid JSON.
        parsed = json.loads(sig)
        assert parsed["active_sprint_id"] == "sp-1"

    def test_signature_is_stable_for_equivalent_summaries(self):
        s1 = {"active_sprint_id": "x", "current_phase": "y", "summary": "z"}
        s2 = {"summary": "z", "current_phase": "y", "active_sprint_id": "x"}
        # sort_keys=True makes ordering identical regardless of dict order.
        assert HarnessOrchestrator._summary_signature(s1) == HarnessOrchestrator._summary_signature(s2)

    def test_signature_changes_with_value_change(self):
        s1 = {"active_sprint_id": "x"}
        s2 = {"active_sprint_id": "y"}
        assert HarnessOrchestrator._summary_signature(s1) != HarnessOrchestrator._summary_signature(s2)

    def test_extra_fields_not_in_stable_set_ignored(self):
        # Fields not in the stable subset don't affect the signature.
        s1 = {"active_sprint_id": "x"}
        s2 = {"active_sprint_id": "x", "random_field": "anything"}
        assert HarnessOrchestrator._summary_signature(s1) == HarnessOrchestrator._summary_signature(s2)

    def test_missing_fields_default_to_empty(self):
        # _summary_signature uses .get(default) so missing fields don't crash.
        sig = HarnessOrchestrator._summary_signature({})
        parsed = json.loads(sig)
        assert parsed["active_sprint_id"] == ""
        assert parsed["deliverables"] == []


# ── _is_generator_ready_contract ───────────────────────────────────────


class TestIsGeneratorReadyContract:
    def test_missing_required_fields_returns_false(self):
        # Without active_sprint_id, objective, or acceptance_checks → False.
        assert HarnessOrchestrator._is_generator_ready_contract({}) is False
        assert HarnessOrchestrator._is_generator_ready_contract(
            {"active_sprint_id": "sp-1"},
        ) is False

    def test_read_only_objective_token_short_circuits_to_true(self):
        # Read-only audits don't need target_files etc. — the token in
        # the objective alone is enough.
        for token in (
            "read-only audit",
            "READ ONLY review",
            "do not modify repository files",
            "audit only",
            "inspect only",
        ):
            assert HarnessOrchestrator._is_generator_ready_contract({
                "active_sprint_id": "sp-1",
                "contract_objective": token,
                "acceptance_checks": ["c1"],
            }) is True

    def test_target_files_with_line_hints_is_ready(self):
        assert HarnessOrchestrator._is_generator_ready_contract({
            "active_sprint_id": "sp-1",
            "contract_objective": "build a thing",
            "acceptance_checks": ["c1"],
            "target_files": ["a.py"],
            "target_line_hints": ["line 10"],
        }) is True

    def test_target_files_with_validation_commands_is_ready(self):
        assert HarnessOrchestrator._is_generator_ready_contract({
            "active_sprint_id": "sp-1",
            "contract_objective": "build a thing",
            "acceptance_checks": ["c1"],
            "target_files": ["a.py"],
            "validation_commands": ["pytest"],
        }) is True

    def test_target_files_with_edit_strategy_is_ready(self):
        assert HarnessOrchestrator._is_generator_ready_contract({
            "active_sprint_id": "sp-1",
            "contract_objective": "build a thing",
            "acceptance_checks": ["c1"],
            "target_files": ["a.py"],
            "edit_strategy": "surgical",
        }) is True

    def test_target_files_alone_is_not_ready(self):
        # Need at least one of: line_hints, validation_commands, edit_strategy.
        assert HarnessOrchestrator._is_generator_ready_contract({
            "active_sprint_id": "sp-1",
            "contract_objective": "build a thing",
            "acceptance_checks": ["c1"],
            "target_files": ["a.py"],
        }) is False

    def test_no_target_files_not_ready_without_read_only_token(self):
        # Without read-only token + without target_files → False.
        assert HarnessOrchestrator._is_generator_ready_contract({
            "active_sprint_id": "sp-1",
            "contract_objective": "build a thing",
            "acceptance_checks": ["c1"],
            "edit_strategy": "surgical",
        }) is False


# ── _repairable_generator_failure ──────────────────────────────────────


class TestRepairableGeneratorFailure:
    def test_syntax_error_in_findings_is_repairable(self):
        assert HarnessOrchestrator._repairable_generator_failure({
            "findings": ["File has SyntaxError on line 10"],
        }) is True

    def test_traceback_in_summary_is_repairable(self):
        assert HarnessOrchestrator._repairable_generator_failure({
            "summary": "Traceback (most recent call last):\nNameError: x",
        }) is True

    def test_module_not_found_is_repairable(self):
        assert HarnessOrchestrator._repairable_generator_failure({
            "required_revisions": ["fix ModuleNotFoundError: no module"],
        }) is True

    def test_indentation_error_in_validation_artifacts(self):
        assert HarnessOrchestrator._repairable_generator_failure({
            "validation_artifacts": ["IndentationError: unexpected indent"],
        }) is True

    def test_no_repairable_tokens_returns_false(self):
        assert HarnessOrchestrator._repairable_generator_failure({
            "summary": "the implementation is incomplete",
            "findings": ["needs more tests"],
        }) is False

    def test_empty_summary_returns_false(self):
        assert HarnessOrchestrator._repairable_generator_failure({}) is False

    def test_case_insensitive_match(self):
        # Tokens compared lowercase.
        assert HarnessOrchestrator._repairable_generator_failure({
            "summary": "TYPEERROR: int + str",
        }) is True


# ── _should_auto_approve ───────────────────────────────────────────────


class TestShouldAutoApprove:
    def test_proposed_with_ready_contract_auto_approves(self):
        assert HarnessOrchestrator._should_auto_approve({
            "contract_status": "proposed",
            "active_sprint_id": "sp-1",
            "contract_objective": "audit only",
            "acceptance_checks": ["c1"],
        }) is True

    def test_approved_status_does_not_auto_approve(self):
        # Already approved — _should_auto_approve only fires for proposed.
        assert HarnessOrchestrator._should_auto_approve({
            "contract_status": "approved",
            "active_sprint_id": "sp-1",
            "contract_objective": "audit only",
            "acceptance_checks": ["c1"],
        }) is False

    def test_proposed_but_not_ready_contract_does_not_auto_approve(self):
        # Status is right but contract isn't generator-ready.
        assert HarnessOrchestrator._should_auto_approve({
            "contract_status": "proposed",
            "active_sprint_id": "sp-1",
            "contract_objective": "build a feature",
            "acceptance_checks": ["c1"],
            # No target_files / read_only token.
        }) is False


# ── _completion_message ────────────────────────────────────────────────


class TestCompletionMessage:
    def test_passed_status_returns_already_passed(self):
        assert HarnessOrchestrator._completion_message({
            "contract_status": "passed",
        }) == "Sprint already passed"

    def test_no_active_sprint_returns_no_active(self):
        assert HarnessOrchestrator._completion_message({
            "active_sprint_id": "",
        }) == "No active sprint"

    def test_default_returns_no_action(self):
        assert HarnessOrchestrator._completion_message({
            "active_sprint_id": "sp-1",
            "contract_status": "approved",
        }) == "No further harness action required"


# ── _choose_next_role ──────────────────────────────────────────────────


class TestChooseNextRole:
    def test_passed_returns_none(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "passed",
        }) is None

    def test_no_active_sprint_returns_planner(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "approved",
            "active_sprint_id": "",
        }) == "planner"

    def test_proposed_status_returns_planner(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "proposed",
            "active_sprint_id": "sp-1",
        }) == "planner"

    def test_approved_status_returns_generator(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "approved",
            "active_sprint_id": "sp-1",
        }) == "generator"

    def test_needs_revision_returns_generator(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "needs_revision",
            "active_sprint_id": "sp-1",
        }) == "generator"

    def test_implemented_returns_evaluator(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "implemented",
            "active_sprint_id": "sp-1",
        }) == "evaluator"

    def test_blocked_verdict_with_repairable_failure_returns_generator(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "approved",
            "active_sprint_id": "sp-1",
            "evaluator_verdict": "blocked",
            "summary": "SyntaxError on line 5",  # repairable token
        }) == "generator"

    def test_blocked_verdict_without_repairable_returns_planner(self):
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "approved",
            "active_sprint_id": "sp-1",
            "evaluator_verdict": "blocked",
            "summary": "the spec is fundamentally wrong",
        }) == "planner"

    def test_failed_status_with_blocked_repairable_returns_generator(self):
        # The very first explicit branch: contract_status==failed AND
        # verdict==blocked AND repairable → generator (single retry).
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "failed",
            "active_sprint_id": "sp-1",
            "evaluator_verdict": "blocked",
            "findings": ["TypeError: int + str"],  # repairable token
        }) == "generator"

    def test_failed_status_without_blocked_returns_planner(self):
        # Failed but verdict isn't blocked — falls through to the
        # `contract_status in {"", "proposed", "failed"}` branch → planner.
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "failed",
            "active_sprint_id": "sp-1",
            "evaluator_verdict": "revise",
        }) == "planner"

    def test_unknown_status_falls_to_planner(self):
        # Unrecognized status → falls through to the final return → planner.
        assert HarnessOrchestrator._choose_next_role({
            "contract_status": "weird_custom",
            "active_sprint_id": "sp-1",
        }) == "planner"

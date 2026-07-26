"""Tests for v0.5.15a1 — harness/service.py::build_resume_prompt.

The remaining 31% of harness/service.py left uncovered after v0.5.14a1
is build_resume_prompt — a long composer that produces the system
prompt for a resumed harness session. Three roles (planner /
generator / evaluator), each with a distinct read-order + role-
specific task block. Plus seven optional sections (blockers /
next_steps / acceptance_checks / validation_checks / validation_
artifacts / acceptance_evidence / required_revisions) that fire
conditionally based on workspace state.

A regression in this method would silently degrade resumed-session
prompts — agents would still run but with incomplete context, the
exact kind of slow-burn defect that's hard to notice without a
direct test.

Coverage delta target on harness/service.py: 69% → ~95%.
"""
from __future__ import annotations

import pytest

from resonant_client.harness.service import HarnessService
from resonant_client.harness.state import (
    EvaluatorReport,
    HarnessWorkspace,
    ProductSpec,
    ProgressState,
    SprintContract,
)


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def service():
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


@pytest.fixture
def populated_workspace(state_home, project_dir):
    """A HarnessWorkspace with a complete sprint state — all the
    optional fields populated so individual tests can verify their
    conditional sections render."""
    ws = HarnessWorkspace(project_dir)
    ws.ensure_layout()

    ws.write_spec(ProductSpec(
        title="Resonant Client",
        summary="Ollama-native agentic coder",
    ))
    ws.write_sprint_contract(SprintContract(
        sprint_id="sp-42",
        feature_name="grill-codification",
        objective="Codify the 5-beat grill exemplar",
        deliverables=["update prompt block"],
        acceptance_checks=["check 1", "check 2", "check 3", "check 4", "check 5", "check 6"],
        target_files=["resonant_client/orchestration/grill_me.py"],
        target_line_hints=["EXEMPLAR section near top"],
        validation_commands=["pytest -q tests/test_rigorous_grill.py"],
        edit_strategy="surgical",
        status="approved",
    ))
    ws.write_progress(ProgressState(
        product_goal="Tighter grill",
        current_phase="implementation",
        active_sprint_id="sp-42",
        active_role="generator",
        summary="halfway through codification",
        blockers=["b1", "b2", "b3", "b4", "b5", "b6"],
        next_steps=["n1", "n2", "n3", "n4", "n5", "n6"],
        validation_checks=["v1", "v2", "v3", "v4", "v5", "v6"],
        validation_artifacts=["a1", "a2", "a3", "a4", "a5", "a6"],
        acceptance_evidence={
            f"check{i}": "evidence " * 30  # ~210 chars each → triggers truncation
            for i in range(1, 7)
        },
    ))
    ws.write_evaluator_report(EvaluatorReport(
        sprint_id="sp-42",
        verdict="revise",
        required_revisions=["r1", "r2", "r3", "r4", "r5", "r6"],
        findings=["finding 1"],
    ))
    return ws


# ── Chat mode short-circuit ────────────────────────────────────────────


class TestBuildResumePromptChatMode:
    def test_chat_mode_returns_canned_string(self, service):
        out = service.build_resume_prompt(
            project_path="/p", session_mode="chat", session_role="planner",
        )
        assert out == "Resume the chat conversation naturally from the existing context."


# ── Per-role read order ────────────────────────────────────────────────


class TestBuildResumePromptReadOrder:
    def test_planner_includes_spec_path_first(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint",
            session_role="planner",
        )
        # Planner reads spec FIRST, then progress, then sprint_contract,
        # then evaluator_report, then handoff.
        spec_idx = out.index("spec.json")
        progress_idx = out.index("progress_state.json")
        contract_idx = out.index("sprint_contract.json")
        report_idx = out.index("evaluator_report.json")
        handoff_idx = out.index("handoff.md")
        assert spec_idx < progress_idx < contract_idx < report_idx < handoff_idx

    def test_evaluator_omits_spec_path(
        self, service, populated_workspace, project_dir,
    ):
        # Evaluator's read order skips spec.json (focus is on the sprint
        # contract + report + progress, not the product vision).
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint",
            session_role="evaluator",
        )
        # Spec path NOT in the read-order list (file path won't appear).
        # We can't just check "spec.json not in out" — the path WOULD
        # appear if it were there. Verify by checking the read-order
        # block more carefully: progress is the FIRST file mentioned.
        # The "Start by reading these files in order:" block should
        # have progress as item 1.
        block_start = out.index("Start by reading these files in order:")
        next_block = out.index("\n\n", block_start)
        read_order_block = out[block_start:next_block]
        assert "1. " in read_order_block
        # First numbered entry should NOT be spec.
        first_line = read_order_block.split("\n")[1]
        assert "progress_state.json" in first_line
        assert "spec.json" not in first_line


# ── Role-specific task blocks ──────────────────────────────────────────


class TestBuildResumePromptRoleBlocks:
    def test_planner_task_block_present(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        # Planner gets the spec-refinement + sprint-contract task list.
        assert "refine or complete the spec" in out
        assert "sprint_contract" in out
        assert "validation_commands" in out
        assert "planner_update" in out

    def test_generator_task_block_present(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="generator",
        )
        # Generator gets the implementation task list.
        assert "implement only the active sprint" in out
        assert "acceptance_evidence" in out
        assert "generator_update" in out
        # Should NOT have planner-specific phrasing.
        assert "refine or complete the spec" not in out

    def test_evaluator_task_block_present(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="evaluator",
        )
        # Evaluator gets the verify task list.
        assert "verify the current implementation" in out
        assert "evaluator_verdict" in out


# ── Spec lines presence (planner+generator have, evaluator omits) ──────


class TestBuildResumePromptSpecLines:
    def test_planner_includes_product_title_and_summary(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        assert "Product title: Resonant Client" in out
        assert "Product summary: Ollama-native agentic coder" in out

    def test_evaluator_omits_product_title(
        self, service, populated_workspace, project_dir,
    ):
        # Evaluator role skips spec lines.
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="evaluator",
        )
        assert "Product title:" not in out
        assert "Product summary:" not in out

    def test_unknown_spec_renders_as_unknown_placeholder(
        self, service, state_home, project_dir,
    ):
        # When spec is empty, falls back to "Unknown" / "Not set".
        ws = HarnessWorkspace(project_dir)
        ws.ensure_layout()  # default empty spec
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        assert "Product title: Unknown" in out
        assert "Product summary: Not set" in out


# ── Conditional sections (blockers / next_steps / etc.) ────────────────


class TestBuildResumePromptConditionalSections:
    def test_blockers_capped_at_5(
        self, service, populated_workspace, project_dir,
    ):
        # Workspace has 6 blockers; output only includes 5.
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        block_section = out.split("Current blockers:")[1]
        # The 6th blocker shouldn't appear.
        assert "- b6" not in block_section
        # The first 5 should.
        for i in range(1, 6):
            assert f"- b{i}" in block_section

    def test_next_steps_capped_at_5(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        ns_section = out.split("Current next steps:")[1]
        assert "- n6" not in ns_section
        for i in range(1, 6):
            assert f"- n{i}" in ns_section

    def test_acceptance_checks_section_capped_at_5(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        ac_section = out.split("Acceptance checks:")[1]
        # Workspace has 6 ("check 1".."check 6"); only 5 should appear.
        assert "- check 6" not in ac_section
        for i in range(1, 6):
            assert f"- check {i}" in ac_section

    def test_validation_checks_section_present(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        assert "Recorded validation checks:" in out
        for i in range(1, 6):
            assert f"- v{i}" in out

    def test_validation_artifacts_section_present(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        assert "Validation artifacts:" in out
        for i in range(1, 6):
            assert f"- a{i}" in out

    def test_acceptance_evidence_truncated_to_180_chars(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        # Evidence rows have form "- check1: <truncated text>".
        # Pick the first one and verify the trailing text doesn't
        # exceed 180 chars (plus one for ellipsis).
        ev_section = out.split("Explicit acceptance evidence:")[1]
        first_row = ev_section.lstrip("\n").split("\n")[0]
        assert first_row.startswith("- check")
        # Strip the prefix "- checkN: " to get just the truncated value.
        _, value = first_row.split(": ", 1)
        # _truncate_text caps at max_chars total (179 chars + ellipsis = 180).
        assert len(value) <= 180

    def test_required_revisions_section_capped_at_5(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        rev_section = out.split("Required revisions from evaluator:")[1]
        assert "- r6" not in rev_section
        for i in range(1, 6):
            assert f"- r{i}" in rev_section

    def test_empty_workspace_omits_optional_sections(
        self, service, state_home, project_dir,
    ):
        # Default workspace has all optional fields empty — the
        # corresponding section headers should NOT appear.
        ws = HarnessWorkspace(project_dir)
        ws.ensure_layout()
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        for section in (
            "Current blockers:",
            "Current next steps:",
            "Acceptance checks:",
            "Recorded validation checks:",
            "Validation artifacts:",
            "Explicit acceptance evidence:",
            "Required revisions from evaluator:",
        ):
            assert section not in out, (
                f"empty workspace shouldn't render {section!r}"
            )


# ── Sprint metadata ────────────────────────────────────────────────────


class TestBuildResumePromptMetadata:
    def test_sprint_id_and_objective_present(
        self, service, populated_workspace, project_dir,
    ):
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        assert "Active sprint: sp-42" in out
        assert "Sprint objective: Codify the 5-beat grill exemplar" in out

    def test_unknown_metadata_uses_fallback_strings(
        self, service, state_home, project_dir,
    ):
        # Empty workspace — sprint_id falls back to "none", objective
        # to "none". Contract status defaults to "proposed" (the
        # SprintContract dataclass default), evaluator verdict defaults
        # to "unknown" (the EvaluatorReport dataclass default).
        ws = HarnessWorkspace(project_dir)
        ws.ensure_layout()
        out = service.build_resume_prompt(
            project_path=str(project_dir),
            session_mode="sprint", session_role="planner",
        )
        assert "Active sprint: none" in out
        assert "Sprint objective: none" in out
        # Default contract status is "proposed" (not "unknown" — the
        # `or "unknown"` fallback only fires when the value is falsy).
        assert "Contract status: proposed" in out
        assert "Last evaluator verdict: unknown" in out

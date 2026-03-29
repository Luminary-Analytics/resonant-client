"""
Harness-facing service helpers.

This module contains the reusable harness summary, instruction, output
contract, and resume-prompt logic. Client shells can delegate to this service
rather than embedding the harness prompt surface directly.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from .state import HarnessWorkspace


class HarnessService:
    def __init__(
        self,
        *,
        normalize_session_mode: Callable[[str], str],
        normalize_session_role: Callable[[str, str], str],
    ):
        self._normalize_session_mode = normalize_session_mode
        self._normalize_session_role = normalize_session_role

    @staticmethod
    def normalize_contract_status(status: str, *, session_role: str) -> str:
        raw = str(status or "").strip().lower()
        if not raw:
            return ""
        aliases = {
            "propose": "proposed",
            "proposed": "proposed",
            "contract_proposed": "proposed",
            "contract_draft": "proposed",
            "planning": "proposed",
            "planning_started": "proposed",
            "planning_in_progress": "proposed",
            "drafting": "proposed",
            "contract_drafting": "proposed",
            "active": "approved",
            "ready": "approved",
            "ready_for_implementation": "approved",
            "implementation_ready": "approved",
            "ready_to_implement": "approved",
            "ready_to_execute": "approved",
            "ready_for_execution": "approved",
            "ready_for_generator": "approved",
            "ready_for_generation": "approved",
            "ready_for_generator_handoff": "approved",
            "ready_for_generator_execution": "approved",
            "contract_ready": "approved",
            "contract_finalized": "approved",
            "contract_locked": "approved",
            "contract_finalized_ready_for_generator": "approved",
            "generator_ready": "approved",
            "generator_handoff_ready": "approved",
            "execution_ready": "approved",
            "ready_to_start": "approved",
            "planning_complete": "approved",
            "planning_completed": "approved",
            "plan_complete": "approved",
            "approve": "approved",
            "approved": "approved",
            "revise": "needs_revision",
            "revision": "needs_revision",
            "needs_revision": "needs_revision",
            "implement": "implemented",
            "implemented": "implemented",
            "repair": "implemented",
            "repaired": "implemented",
            "fixed": "implemented",
            "ready_for_evaluation": "implemented",
            "evaluation_ready": "implemented",
            "ready_to_evaluate": "implemented",
            "ready_to_review": "implemented",
            "review": "implemented",
            "under_review": "implemented",
            "fail": "failed",
            "failed": "failed",
            "block": "failed",
            "blocked": "failed",
            "pass": "passed",
            "passed": "passed",
        }
        if raw in aliases:
            return aliases[raw]
        if session_role == "planner":
            if "generator" in raw and "ready" in raw:
                return "approved"
            if "planning" in raw and any(token in raw for token in ("complete", "completed", "final", "locked")):
                return "approved"
            if raw.startswith("planning"):
                return "proposed"
        if raw in {"complete", "completed", "done"}:
            if session_role == "planner":
                return "approved"
            if session_role == "generator":
                return "implemented"
            return "passed"
        return raw

    @staticmethod
    def _truncate_text(value: str, *, max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 1)].rstrip() + "…"

    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if value in (None, ""):
            return []
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, (list, tuple, set)):
            result: list[str] = []
            for item in value:
                text = str(item or "").strip()
                if text:
                    result.append(text)
            return result
        text = str(value).strip()
        return [text] if text else []

    @staticmethod
    def _normalize_string_mapping(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        result: dict[str, str] = {}
        for key, item in value.items():
            key_text = str(key or "").strip()
            if not key_text:
                continue
            if isinstance(item, bool):
                item_text = "PASS" if item else ""
            else:
                item_text = str(item or "").strip()
            if item_text:
                result[key_text] = item_text
        return result

    def build_output_contract(
        self,
        *,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self._normalize_session_mode(session_mode)
        session_role = self._normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return ""

        templates = {
            "planner": {
                "action": "planner_update",
                "spec": {
                    "title": "",
                    "summary": "",
                    "user_stories": [],
                    "sprint_order": [],
                    "design_principles": [],
                    "technical_notes": [],
                },
                "progress": {
                    "product_goal": "",
                    "summary": "",
                    "blockers": [],
                    "next_steps": [],
                    "current_phase": "planning",
                },
                "sprint_contract": {
                    "sprint_id": "",
                    "feature_name": "",
                    "objective": "",
                    "deliverables": [],
                    "acceptance_checks": [],
                    "evaluator_focus": [],
                    "target_files": [],
                    "target_line_hints": [],
                    "validation_commands": [],
                    "edit_strategy": "",
                    "status": "proposed",
                },
            },
            "generator": {
                "action": "generator_update",
                "progress": {
                    "summary": "",
                    "blockers": [],
                    "next_steps": [],
                    "touched_files": [],
                    "last_validation": "",
                    "validation_checks": [],
                    "validation_artifacts": [],
                    "acceptance_evidence": {
                        "<acceptance check>": "Concrete evidence for this check",
                    },
                    "current_phase": "implementation",
                },
                "sprint_status": "implemented",
                "handoff_markdown": "# Summary\n\n# Next Action\n",
            },
            "evaluator": {
                "action": "evaluator_verdict",
                "sprint_id": "",
                "verdict": "revise",
                "score": 0.0,
                "findings": [],
                "required_revisions": [],
                "passed_checks": [],
                "failed_checks": [],
            },
        }
        template_text = json.dumps(templates[session_role], indent=2, ensure_ascii=False)
        return (
            "End your final assistant response with a fenced code block named "
            "`resonant-harness` containing valid JSON for your role. Always include the explicit "
            "`action` field shown below. If the objective asks for bullets, a report, or an audit, "
            "put that human-readable content in `handoff_markdown` or concise summary fields, then "
            "still end with the full harness block. Use only concrete values, keep lists short, and "
            "omit fields you cannot justify.\n"
            f"```resonant-harness\n{template_text}\n```"
        )

    def build_instructions(
        self,
        *,
        project_path: str,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self._normalize_session_mode(session_mode)
        session_role = self._normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return ""

        harness = HarnessWorkspace(project_path)
        role_block = {
            "planner": (
                "You are the planner session. Start by reading the harness files, then update "
                "`spec.json`, `progress_state.json`, and `sprint_contract.json`. Define the next "
                "sprint in concrete, testable terms before implementation starts."
            ),
            "generator": (
                "You are the generator session. Read the current harness state first. Implement only "
                "the active sprint, run the cheapest relevant validation, then update "
                "`progress_state.json` and `handoff.md` before finishing. Do not start coding if the "
                "sprint contract is not approved or marked for revision."
            ),
            "evaluator": (
                "You are the evaluator session. Read the sprint contract and current progress first. "
                "Verify the implementation against the stated acceptance checks, run focused tests or "
                "tool-based checks, then write a concrete verdict to `evaluator_report.json`."
            ),
        }[session_role]

        return (
            "\n\n--- HARNESS INSTRUCTIONS (.resonant-harness) ---\n"
            f"Session role: {session_role}\n"
            f"Harness root: {harness.root}\n"
            f"Read first:\n"
            f"- {harness.spec_path}\n"
            f"- {harness.progress_path}\n"
            f"- {harness.sprint_contract_path}\n"
            f"- {harness.evaluator_report_path}\n"
            f"- {harness.handoff_path}\n\n"
            f"{role_block}\n"
            "If the harness files are mostly empty, initialize only the minimum state needed for the current request.\n"
            f"{self.build_output_contract(session_mode=session_mode, session_role=session_role)}\n"
            "--- END HARNESS INSTRUCTIONS ---"
        )

    def get_summary(self, project_path: str) -> dict[str, Any]:
        target_path = os.path.normpath(project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        progress = harness.read_progress()
        contract = harness.read_sprint_contract()
        report = harness.read_evaluator_report()
        recent_history = harness.read_run_history(limit=5)
        recent_teacher_escalations = harness.read_teacher_escalations(limit=3)
        normalized_contract_status = (
            self.normalize_contract_status(
                contract.status,
                session_role=str(progress.active_role or "planner").strip() or "planner",
            )
            or str(contract.status or "").strip()
        )
        return {
            "root": str(harness.root),
            "spec_path": str(harness.spec_path),
            "progress_path": str(harness.progress_path),
            "sprint_contract_path": str(harness.sprint_contract_path),
            "evaluator_report_path": str(harness.evaluator_report_path),
            "handoff_path": str(harness.handoff_path),
            "run_history_path": str(harness.run_history_path),
            "teacher_escalations_path": str(harness.teacher_escalations_path),
            "current_phase": progress.current_phase,
            "active_sprint_id": progress.active_sprint_id,
            "active_role": progress.active_role,
            "summary": progress.summary,
            "blockers": list(progress.blockers),
            "next_steps": list(progress.next_steps),
            "touched_files": list(progress.touched_files),
            "last_validation": progress.last_validation,
            "validation_checks": list(progress.validation_checks),
            "validation_artifacts": list(progress.validation_artifacts),
            "acceptance_evidence": dict(progress.acceptance_evidence),
            "contract_status": normalized_contract_status,
            "contract_status_raw": contract.status,
            "contract_feature_name": contract.feature_name,
            "contract_objective": contract.objective,
            "deliverables": list(contract.deliverables),
            "acceptance_checks": list(contract.acceptance_checks),
            "evaluator_focus": list(contract.evaluator_focus),
            "target_files": list(contract.target_files),
            "target_line_hints": list(contract.target_line_hints),
            "validation_commands": list(contract.validation_commands),
            "edit_strategy": contract.edit_strategy,
            "evaluator_verdict": report.verdict,
            "findings": list(report.findings),
            "required_revisions": list(report.required_revisions),
            "recent_run_events": recent_history,
            "recent_teacher_escalations": recent_teacher_escalations,
        }

    def build_resume_prompt(
        self,
        *,
        project_path: str,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self._normalize_session_mode(session_mode)
        session_role = self._normalize_session_role(session_mode, session_role)
        if session_mode == "chat":
            return "Resume the chat conversation naturally from the existing context."

        target_path = os.path.normpath(project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_summary(target_path)
        spec = harness.read_spec()

        read_order = {
            "planner": [
                summary["spec_path"],
                summary["progress_path"],
                summary["sprint_contract_path"],
                summary["evaluator_report_path"],
                summary["handoff_path"],
            ],
            "generator": [
                summary["spec_path"],
                summary["progress_path"],
                summary["sprint_contract_path"],
                summary["evaluator_report_path"],
                summary["handoff_path"],
            ],
            "evaluator": [
                summary["progress_path"],
                summary["sprint_contract_path"],
                summary["evaluator_report_path"],
                summary["handoff_path"],
            ],
        }[session_role]

        common_lines = [
            f"Resume this project as the {session_role} session using the harness state, not long chat history.",
            "Start by reading these files in order:",
            *[f"{index}. {path}" for index, path in enumerate(read_order, start=1)],
            "",
        ]

        if session_role != "evaluator":
            common_lines.extend(
                [
                    f"Product title: {spec.title or 'Unknown'}",
                    f"Product summary: {spec.summary or 'Not set'}",
                    f"Current phase: {summary['current_phase'] or 'unknown'}",
                ]
            )

        common_lines.extend(
            [
                f"Active sprint: {summary['active_sprint_id'] or 'none'}",
                f"Sprint objective: {summary['contract_objective'] or 'none'}",
                f"Contract status: {summary['contract_status'] or 'unknown'}",
                f"Last evaluator verdict: {summary['evaluator_verdict'] or 'unknown'}",
            ]
        )

        blockers = self._normalize_string_list(summary.get("blockers"))
        next_steps = self._normalize_string_list(summary.get("next_steps"))
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))

        if blockers:
            common_lines.append("Current blockers:")
            common_lines.extend(f"- {item}" for item in blockers[:5])
        if next_steps:
            common_lines.append("Current next steps:")
            common_lines.extend(f"- {item}" for item in next_steps[:5])
        if checks:
            common_lines.append("Acceptance checks:")
            common_lines.extend(f"- {item}" for item in checks[:5])
        if validation_checks:
            common_lines.append("Recorded validation checks:")
            common_lines.extend(f"- {item}" for item in validation_checks[:5])
        if validation_artifacts:
            common_lines.append("Validation artifacts:")
            common_lines.extend(f"- {item}" for item in validation_artifacts[:5])
        if acceptance_evidence:
            common_lines.append("Explicit acceptance evidence:")
            common_lines.extend(
                f"- {check}: {self._truncate_text(evidence, max_chars=180)}"
                for check, evidence in list(acceptance_evidence.items())[:5]
            )
        if revisions:
            common_lines.append("Required revisions from evaluator:")
            common_lines.extend(f"- {item}" for item in revisions[:5])

        role_lines = {
            "planner": [
                "",
                "Your task:",
                "- refine or complete the spec only where needed",
                "- define or revise the next sprint contract in concrete, testable terms",
                "- do not execute the sprint itself; leave the audit, implementation, or verification work to later roles",
                "- put contract fields under `sprint_contract`; do not invent wrapper keys like `next_sprint_contract` or `scope` without also filling `sprint_contract`",
                "- for code-changing tasks, include `target_files`, `target_line_hints`, `validation_commands`, and `edit_strategy` in `sprint_contract`",
                "- every `validation_commands` entry must be executable as written from the project root; do not use placeholders like `<path>` or pseudo-commands",
                "- finish with a normal summary plus a valid ```resonant-harness JSON block for planner_update",
            ],
            "generator": [
                "",
                "Your task:",
                "- implement only the active sprint",
                "- if the contract is not approved or needs_revision, stop and say that first",
                "- run the cheapest relevant validation before finishing",
                "- record exact validation evidence in progress.last_validation and short check bullets in progress.validation_checks",
                "- record compact validation artifacts in progress.validation_artifacts",
                "- fill progress.acceptance_evidence with one concise evidence line per satisfied acceptance check",
                "- finish with a normal summary plus a valid ```resonant-harness JSON block for generator_update",
            ],
            "evaluator": [
                "",
                "Your task:",
                "- verify the current implementation against the sprint contract",
                "- state pass, revise, or blocked with concrete findings",
                "- finish with a normal summary plus a valid ```resonant-harness JSON block for evaluator_verdict",
            ],
        }[session_role]

        return "\n".join(common_lines + role_lines)

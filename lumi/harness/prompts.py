"""Harness prompt construction, payload inference, and contract normalization.

These 95 methods used to live on `gui.AppState`, where they were 4,439 of its
5,700 lines — roughly four fifths of a class whose actual job is holding the
GUI server's runtime state (backends, sessions, projects, settings). Prompt
engineering for the generator and evaluator roles is domain logic; it does not
belong to a web server's state object, and burying it there meant it could only
be exercised by constructing the whole application.

What they genuinely need from the application is small and now explicit: the
current project, the settings store, the active backend and its spec, the list
of available backends, the harness service, two token ceilings, and five
methods for building sessions and normalizing session identifiers. That is the
entire contract, reached through `self._app`.

Anything requiring more than that surface belongs on AppState, not here.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ..engine import AGENT_TOOLS
from ..events import EngineEvent
from ..processes import background_process_kwargs
from .service import HarnessService
from .state import EvaluatorReport, HarnessWorkspace

if TYPE_CHECKING:
    # Annotation-only. Importing gui.runtime at runtime would invert the
    # layering — the harness would depend on the GUI it is meant to serve —
    # and `from __future__ import annotations` makes it unnecessary.
    from ..engine import Session
    from ..gui.runtime import BackendSpec

logger = logging.getLogger(__name__)


class HarnessPrompts:
    """Prompt/payload logic for the sprint-harness generator and evaluator roles.

    `app` is duck-typed rather than annotated as AppState to keep the dependency
    one-directional and the class testable with a small stub. It must provide
    the names listed in the module docstring.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    def _get_remote_harness_step_payload(
        self,
        *,
        project_path: str,
        session_mode: str,
        session_role: str,
        objective: str = "",
        backend=None,
    ) -> dict[str, Any] | None:
        target_backend = backend or self._app.backend
        backend_name = str(getattr(target_backend, "name", "") or "").strip().lower()
        if backend_name != "resonant" or not hasattr(target_backend, "prepare_harness_step"):
            return None
        try:
            payload = target_backend.prepare_harness_step(
                project_path=project_path,
                session_mode=session_mode,
                session_role=session_role,
                objective=objective,
                execute=False,
            )
        except Exception as exc:
            logger.warning("Falling back to local harness step prep for %s: %s", project_path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    def harness_enabled(self) -> bool:
        """Master switch for the sprint workflow.

        When False (the default): no `.resonant-harness/` directory is created,
        no harness preamble is injected into the system prompt, no message-wrap
        runs, and the role/badge UI stays hidden. The agent operates as a plain
        ReAct loop — the same flow Claude Code / Codex / OpenCode / Cursor offer.

        When True: planner / generator / evaluator roles, sprint contracts,
        evaluator reports, and the autonomous HarnessOrchestrator cycle all wake up.
        """
        return bool(self._app.settings.get("general", "harness_enabled", False))

    def build_harness_instructions(
        self,
        *,
        project_path: str,
        session_mode: str,
        session_role: str,
        backend=None,
        objective: str = "",
    ) -> str:
        # Master gate: sprint workflow is opt-in. When off, nothing about the
        # harness leaks into the system prompt.
        if not self.harness_enabled():
            return ""
        # Remote engines that own canonical harness state always take precedence.
        payload = self._get_remote_harness_step_payload(
            project_path=project_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=objective,
            backend=backend,
        )
        if payload and payload.get("instructions"):
            return str(payload["instructions"])
        # Local fallback: only inject the "Read first: spec.json / progress_state.json /
        # sprint_contract.json / ..." block when there's actually an active sprint.
        # Otherwise every casual question (e.g. "help me with desktop issues") wastes a
        # tool-call cycle reading empty harness files.
        try:
            summary = self.get_harness_summary(project_path) or {}
        except Exception:
            summary = {}
        has_active_sprint = bool(
            summary.get("active_sprint_id")
            and str(summary.get("contract_status") or "").strip()
            in {"approved", "needs_revision"}
        )
        if not has_active_sprint:
            return ""
        return self._app.harness_service.build_instructions(
            project_path=project_path,
            session_mode=session_mode,
            session_role=session_role,
        )

    def build_harness_output_contract(
        self,
        *,
        session_mode: str,
        session_role: str,
        project_path: Optional[str] = None,
        backend=None,
        objective: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        payload = self._get_remote_harness_step_payload(
            project_path=target_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=objective,
            backend=backend,
        )
        if payload and payload.get("output_contract"):
            return str(payload["output_contract"])
        return self._app.harness_service.build_output_contract(
            session_mode=session_mode,
            session_role=session_role,
        )

    def get_harness_summary(self, project_path: Optional[str] = None) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        backend_name = str(getattr(self._app.backend, "name", "") or "").strip().lower()
        if backend_name == "resonant" and hasattr(self._app.backend, "get_harness_state"):
            try:
                payload = self._app.backend.get_harness_state(target_path)
                summary = payload.get("summary")
                if isinstance(summary, dict) and summary:
                    return summary
            except Exception as exc:
                logger.warning("Falling back to local harness summary for %s: %s", target_path, exc)
        return self._app.harness_service.get_summary(target_path)

    @staticmethod
    @staticmethod
    def _truncate_text(value: str, *, max_chars: int) -> str:
        text = str(value or "").strip()
        if len(text) <= max_chars:
            return text
        return text[: max(0, max_chars - 1)].rstrip() + "…"

    def get_harness_evaluator_mode(self) -> str:
        raw = str(os.environ.get("RESONANT_HARNESS_EVALUATOR_MODE", "hybrid") or "").strip().lower()
        if raw in {"full", "artifacts", "structured", "hybrid"}:
            return raw
        return "hybrid"

    def get_harness_evaluator_artifact_max_tokens(self) -> None:
        return None

    def get_harness_evaluator_structured_max_tokens(self) -> None:
        return None

    def get_harness_generator_mode(self) -> str:
        raw = str(os.environ.get("RESONANT_HARNESS_GENERATOR_MODE", "hybrid") or "").strip().lower()
        if raw in {"full", "artifacts", "patch", "structured", "hybrid"}:
            return raw
        return "hybrid"

    def get_harness_generator_artifact_max_tokens(self) -> None:
        return None

    def get_harness_generator_structured_max_tokens(self) -> None:
        return None

    def get_harness_generator_patch_max_tokens(self) -> None:
        return None

    def get_harness_generator_repair_max_tokens(self) -> None:
        return None

    def should_use_harness_artifact_evaluator(self, project_path: Optional[str] = None) -> bool:
        mode = self.get_harness_evaluator_mode()
        if mode == "full":
            return False
        if mode == "artifacts":
            return True

        target_path = os.path.normpath(project_path or self._app.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        contract = harness.read_sprint_contract()
        progress = harness.read_progress()

        objective_lower = str(contract.objective or "").strip().lower()
        feature_lower = str(contract.feature_name or "").strip().lower()

        explicit_read_only_tokens = (
            "read-only",
            "read files only",
            "do not modify repository files",
        )
        if any(token in objective_lower for token in explicit_read_only_tokens):
            return True

        reporting_tokens = (
            "summarize",
            "summary",
            "audit",
            "compare",
            "inventory",
            "explain",
            "record findings",
            "capture findings",
            "handoff artifact",
            "table",
            "bullet",
            "bullets",
        )
        if objective_lower.startswith("read ") and any(token in objective_lower for token in reporting_tokens):
            return True

        if (
            not list(progress.touched_files or [])
            and any(token in feature_lower for token in ("audit", "summary", "inventory", "validation"))
            and any(token in objective_lower for token in ("read ", "record findings", "capture findings", "handoff"))
        ):
            return True

        return False

    @staticmethod
    @staticmethod
    def _resolve_harness_touched_path(project_path: str, raw_path: str) -> Path:
        raw = str(raw_path or "").strip()
        if not raw:
            return Path(project_path)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = Path(project_path) / candidate
        try:
            return candidate.resolve()
        except Exception:
            return candidate

    @staticmethod
    @staticmethod
    def _format_numbered_excerpt(path: Path, *, max_lines: int = 80, max_chars: int = 2400) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[unreadable: {exc}]"
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + "\n...[truncated]"
        lines = text.splitlines()
        if len(lines) > max_lines:
            lines = lines[:max_lines] + ["...[truncated]"]
        return "\n".join(f"{index:>4}: {line}" for index, line in enumerate(lines, start=1))

    @staticmethod
    @staticmethod
    def _format_numbered_window(
        path: Path,
        *,
        start_line: int,
        end_line: int,
        padding: int = 12,
        max_lines: int = 96,
        max_chars: int = 3400,
    ) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[unreadable: {exc}]"
        lines = text.splitlines()
        if not lines:
            return ""
        lo = max(1, int(start_line) - padding)
        hi = min(len(lines), int(end_line) + padding)
        selected = lines[lo - 1 : hi]
        if len(selected) > max_lines:
            hi = lo + max_lines - 1
            selected = lines[lo - 1 : hi]
        output = "\n".join(f"{line_no:>4}: {line}" for line_no, line in enumerate(selected, start=lo))
        if len(output) > max_chars:
            output = output[:max_chars].rstrip() + "\n...[truncated]"
        return output

    @staticmethod
    @staticmethod
    def _extract_line_hint_window(file_path: str, hints: list[str]) -> tuple[int, int, str] | None:
        normalized_path = file_path.replace("\\", "/").strip()
        normalized_name = Path(normalized_path).name
        for raw_hint in hints:
            hint = str(raw_hint or "").strip()
            if not hint:
                continue
            match = re.search(
                r"(?:(?P<path>`?[^`\s:]+(?:/[^`\s:]+)*`?)\s*:\s*)?"
                r"(?:(?:after|around)\s+line\s+|line\s+)?"
                r"(?P<start>\d+)(?:\s*-\s*(?P<end>\d+))?",
                hint,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            hint_path = str(match.group("path") or "").replace("\\", "/").strip("` ").strip()
            if hint_path and hint_path not in {normalized_path, normalized_name}:
                continue
            start_line = int(match.group("start"))
            end_line = int(match.group("end") or match.group("start"))
            return start_line, end_line, hint
        return None

    @staticmethod
    @staticmethod
    def _requests_broad_repo_scope(text: str) -> bool:
        lowered = " ".join(str(text or "").lower().split())
        if not lowered:
            return False

        negative_phrases = (
            "no repo-wide",
            "without repo-wide",
            "avoid repo-wide",
            "not repo-wide",
            "does not require repo-wide",
            "doesn't require repo-wide",
            "without requiring repo-wide",
            "no whole repo",
            "without whole repo",
            "avoid whole repo",
            "does not require whole repo",
            "doesn't require whole repo",
            "no full codebase",
            "without full codebase",
            "avoid full codebase",
            "does not require full codebase",
            "doesn't require full codebase",
            "no entire repository",
            "without entire repository",
            "avoid entire repository",
            "does not require entire repository",
            "doesn't require entire repository",
            "no across the repo",
            "without across the repo",
            "avoid across the repo",
            "does not require across the repo",
            "doesn't require across the repo",
        )
        for phrase in negative_phrases:
            lowered = lowered.replace(phrase, "")

        return any(
            token in lowered
            for token in (
                "entire repository",
                "whole repo",
                "full codebase",
                "across the repo",
                "repo-wide",
            )
        )

    @staticmethod
    @staticmethod
    def _extract_patch_scaffold(file_path: str, hints: list[str]) -> list[str]:
        normalized_path = file_path.replace("\\", "/").strip()
        normalized_name = Path(normalized_path).name
        notes: list[str] = []
        seen: set[str] = set()

        def add_note(value: Any) -> None:
            text = str(value or "").strip()
            if not text or text in seen:
                return
            seen.add(text)
            notes.append(text)

        for raw_hint in hints:
            hint = str(raw_hint or "").strip()
            if not hint:
                continue
            parsed: Any = None
            if hint.startswith("{") and hint.endswith("}"):
                try:
                    parsed = ast.literal_eval(hint)
                except Exception:
                    parsed = None
            if isinstance(parsed, dict):
                scoped = parsed.get(normalized_path) or parsed.get(normalized_name) or parsed
                if isinstance(scoped, dict):
                    for value in scoped.values():
                        add_note(value)
                else:
                    add_note(scoped)
                continue
            if re.search(r"\bline\s+\d+", hint, flags=re.IGNORECASE):
                continue
            add_note(hint)
        return notes[:4]

    @staticmethod
    @staticmethod
    def _format_anchor_windows(
        path: Path,
        anchors: list[str],
        *,
        padding: int = 10,
        max_windows: int = 3,
        max_chars: int = 3400,
    ) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"[unreadable: {exc}]"
        lines = text.splitlines()
        if not lines:
            return ""

        anchor_names: list[str] = []
        for item in anchors:
            for name in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\(\)", str(item or "")):
                if name not in anchor_names:
                    anchor_names.append(name)

        windows: list[tuple[int, int, str]] = []
        for name in anchor_names:
            def_pattern = re.compile(rf"^\s*def\s+{re.escape(name)}\s*\(")
            call_pattern = re.compile(rf"\b{re.escape(name)}\s*\(")
            target_line = 0
            for line_no, line in enumerate(lines, start=1):
                if def_pattern.search(line):
                    target_line = line_no
                    break
            if not target_line:
                for line_no, line in enumerate(lines, start=1):
                    if call_pattern.search(line):
                        target_line = line_no
                        break
            if target_line:
                windows.append(
                    (
                        max(1, target_line - padding),
                        min(len(lines), target_line + padding),
                        name,
                    )
                )
            if len(windows) >= max_windows:
                break

        if not windows:
            return ""

        rendered: list[str] = []
        for start_line, end_line, name in windows:
            rendered.append(f"--- context around {name}() ---")
            rendered.extend(
                f"{line_no:>4}: {line}"
                for line_no, line in enumerate(lines[start_line - 1 : end_line], start=start_line)
            )
        output = "\n".join(rendered)
        if len(output) > max_chars:
            output = output[:max_chars].rstrip() + "\n...[truncated]"
        return output

    @staticmethod
    @staticmethod
    def _filter_tool_definitions(allowed_names: list[str]) -> list[dict[str, Any]]:
        allowed = set(allowed_names)
        return [
            tool
            for tool in AGENT_TOOLS
            if tool.get("function", {}).get("name", "") in allowed
        ]

    def extract_harness_referenced_files(
        self,
        project_path: Optional[str] = None,
        *texts: Any,
        limit: int = 4,
    ) -> list[str]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        project_root = Path(target_path).resolve()
        candidates: list[str] = []
        seen_raw: set[str] = set()
        fenced_pattern = re.compile(r"`([^`\n]+)`")
        path_patterns = (
            re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9_.-]+)"),
            re.compile(r"(?<![\w./-])([A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)"),
        )

        for raw_text in texts:
            text = str(raw_text or "")
            if not text.strip():
                continue
            for match in fenced_pattern.finditer(text):
                candidate = match.group(1).strip()
                if candidate and candidate not in seen_raw:
                    seen_raw.add(candidate)
                    candidates.append(candidate)
            for pattern in path_patterns:
                for match in pattern.finditer(text):
                    candidate = match.group(1).strip()
                    if candidate and candidate not in seen_raw:
                        seen_raw.add(candidate)
                        candidates.append(candidate)

        referenced: list[str] = []
        seen_display: set[str] = set()
        for raw in candidates:
            cleaned = raw.strip().strip("`'\"()[]{}<>.,;:")
            if cleaned.startswith("./"):
                cleaned = cleaned[2:]
            if not cleaned:
                continue
            resolved = self._resolve_harness_touched_path(target_path, cleaned)
            try:
                resolved.relative_to(project_root)
            except ValueError:
                continue
            if not resolved.exists() or not resolved.is_file():
                continue
            display_path = os.path.relpath(str(resolved), target_path).replace(os.sep, "/")
            if display_path not in seen_display:
                seen_display.add(display_path)
                referenced.append(display_path)
            if len(referenced) >= limit:
                break
        return referenced

    @staticmethod
    @staticmethod
    def _normalize_acceptance_check_phrase(check: str) -> str:
        phrase = re.sub(
            r"^(mention|include|cover|state|validate|return|record|show|verify|use)\s+",
            "",
            str(check).strip().lower(),
        )
        phrase = re.sub(r"\s+", " ", phrase).strip(" .")
        return phrase

    @staticmethod
    @staticmethod
    def _acceptance_check_tokens(check: str) -> list[str]:
        stopwords = {
            "the",
            "and",
            "with",
            "that",
            "this",
            "from",
            "into",
            "then",
            "when",
            "where",
            "which",
            "does",
            "have",
            "should",
            "while",
            "within",
            "without",
            "used",
            "using",
            "output",
            "includes",
            "include",
            "prints",
            "print",
            "counts",
            "count",
            "flag",
            "default",
            "existing",
            "current",
            "behavior",
            "remain",
            "remains",
            "unchanged",
            "change",
            "stays",
            "stay",
            "file",
            "files",
            "script",
        }
        tokens: list[str] = []
        for token in re.findall(r"[a-z0-9_:+-]+", str(check or "").lower()):
            cleaned = token.strip("_:+-")
            if not cleaned or len(cleaned) <= 1:
                continue
            if cleaned in stopwords or cleaned in {"n", "m"}:
                continue
            tokens.append(cleaned)
        return tokens

    def _build_acceptance_check_coverage(
        self,
        acceptance_checks: list[str],
        evidence_text: str,
    ) -> list[dict[str, Any]]:
        lowered = str(evidence_text or "").lower()
        coverage = []
        for check in acceptance_checks[:8]:
            phrase = self._normalize_acceptance_check_phrase(check)
            tokens = self._acceptance_check_tokens(check)
            overlap = [token for token in tokens if token in lowered]
            required_overlap = 0
            if tokens:
                required_overlap = max(2, min(len(tokens), 3))
                if len(tokens) <= 2:
                    required_overlap = len(tokens)
            matched = bool(phrase and phrase in lowered)
            if not matched and tokens:
                matched = len(overlap) >= required_overlap
            coverage.append(
                {
                    "check": check,
                    "matched": matched,
                    "normalized_phrase": phrase,
                    "matched_tokens": overlap[:6],
                }
            )
        return coverage

    def build_harness_structured_evidence_bundle(self, project_path: Optional[str] = None) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        line_hints = self._normalize_string_list(summary.get("target_line_hints"))
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1600)
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))

        files: list[dict[str, Any]] = []
        evidence_parts = [
            summary.get("summary") or "",
            summary.get("last_validation") or "",
            "\n".join(self._normalize_string_list(summary.get("validation_checks"))),
            "\n".join(validation_artifacts),
            "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
            handoff_text,
        ]

        for raw_path in touched_files[:4]:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            file_record: dict[str, Any] = {
                "path": raw_path,
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
            }
            if resolved.exists() and resolved.is_file():
                try:
                    file_record["size_bytes"] = resolved.stat().st_size
                except OSError:
                    file_record["size_bytes"] = None
                hint_window = self._extract_line_hint_window(raw_path, line_hints)
                if hint_window:
                    start_line, end_line, _ = hint_window
                    excerpt = self._format_numbered_window(
                        resolved,
                        start_line=start_line,
                        end_line=end_line,
                        padding=16,
                        max_lines=100,
                        max_chars=3200,
                    )
                else:
                    excerpt = self._format_numbered_excerpt(resolved)
                file_record["excerpt"] = excerpt
                evidence_parts.append(excerpt)
            else:
                file_record["excerpt"] = "[missing file]"
                evidence_parts.append(f"{raw_path}: missing file")
            files.append(file_record)

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        combined_evidence = "\n".join(part for part in evidence_parts if part)
        coverage = self._build_acceptance_check_coverage(acceptance_checks, combined_evidence)

        return {
            "summary": summary,
            "handoff_excerpt": handoff_text,
            "files": files,
            "validation_artifacts": validation_artifacts,
            "acceptance_evidence": acceptance_evidence,
            "acceptance_check_coverage": coverage,
        }

    def should_use_harness_generator_artifact_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        mode = self.get_harness_generator_mode()
        if mode == "full":
            return False

        target_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(target_path)
        read_only = self._is_read_only_harness_request(
            prompt,
            summary.get("contract_objective", ""),
            summary.get("contract_feature_name", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
        )
        if not read_only and self._harness_generator_needs_evidence_followup(target_path):
            return True
        if not read_only:
            return False

        referenced_files = self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
        )
        if mode == "artifacts":
            return bool(referenced_files)
        return bool(referenced_files)

    def _harness_generator_needs_evidence_followup(self, project_path: Optional[str] = None) -> bool:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(target_path)
        contract_status = str(summary.get("contract_status") or "").strip()
        evaluator_verdict = str(summary.get("evaluator_verdict") or "").strip()
        if contract_status != "needs_revision" or evaluator_verdict != "revise":
            return False
        if self._harness_generator_needs_frontier_repair(target_path):
            return False
        if self._normalize_string_list(summary.get("blockers")):
            return False
        if not self._normalize_string_list(summary.get("touched_files")):
            return False

        combined = "\n".join(
            [
                "\n".join(self._normalize_string_list(summary.get("required_revisions"))),
                "\n".join(self._normalize_string_list(summary.get("findings"))),
                str(summary.get("last_validation") or ""),
            ]
        ).lower()
        evidence_tokens = (
            "missing evidence",
            "insufficient evidence",
            "not enough evidence",
            "cannot verify",
            "record",
            "validation",
            "confirm",
            "show",
            "callable",
            "empty dict",
            "no other files are modified",
        )
        return bool(combined and any(token in combined for token in evidence_tokens))

    def should_use_harness_generator_structured_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        mode = self.get_harness_generator_mode()
        if mode == "full":
            return False

        target_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(target_path)
        scaffold_target_files = self._normalize_string_list(summary.get("target_files"))
        if self._is_read_only_harness_request(
            prompt,
            summary.get("contract_objective", ""),
            summary.get("contract_feature_name", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
        ):
            return False

        referenced_files = scaffold_target_files or self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
            "\n".join(self._normalize_string_list(summary.get("touched_files"))),
            limit=3,
        )
        if not referenced_files or len(referenced_files) > 2:
            return False

        combined = " ".join(
            [
                str(prompt or ""),
                str(summary.get("contract_objective") or ""),
                str(summary.get("contract_feature_name") or ""),
                "\n".join(self._normalize_string_list(summary.get("deliverables"))),
                "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
            ]
        ).lower()
        if self._requests_broad_repo_scope(combined):
            return False
        return True

    def get_harness_generator_strategy(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        mode = self.get_harness_generator_mode()
        if self.should_use_harness_generator_artifact_mode(project_path, prompt):
            return "artifacts"
        if mode != "full" and self.can_use_harness_generator_repair_mode(project_path, prompt):
            return "repair"
        if mode in {"patch", "structured", "hybrid"} and self.can_use_harness_generator_patch_mode(project_path, prompt):
            return "patch"
        if mode in {"structured", "hybrid"} and self.should_use_harness_generator_structured_mode(project_path, prompt):
            return "structured"
        return "full"

    def build_harness_generator_artifact_bundle(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1400)
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
        referenced_paths = self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
        )
        files: list[dict[str, Any]] = []
        evidence_parts = [
            summary.get("summary") or "",
            summary.get("last_validation") or "",
            "\n".join(self._normalize_string_list(summary.get("validation_checks"))),
            "\n".join(validation_artifacts),
            "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
            handoff_text,
        ]

        for raw_path in referenced_paths[:4]:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            file_record: dict[str, Any] = {
                "path": raw_path,
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
            }
            if resolved.exists() and resolved.is_file():
                excerpt = self._format_numbered_excerpt(resolved, max_lines=100, max_chars=3200)
                file_record["excerpt"] = excerpt
                evidence_parts.append(excerpt)
            else:
                file_record["excerpt"] = "[missing file]"
                evidence_parts.append(f"{raw_path}: missing file")
            files.append(file_record)

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        combined_evidence = "\n".join(part for part in evidence_parts if part)
        coverage = self._build_acceptance_check_coverage(acceptance_checks, combined_evidence)

        return {
            "summary": summary,
            "handoff_excerpt": handoff_text,
            "files": files,
            "validation_artifacts": validation_artifacts,
            "acceptance_evidence": acceptance_evidence,
            "acceptance_check_coverage": coverage,
        }

    def build_harness_generator_structured_bundle(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1400)
        scaffold_target_files = self._normalize_string_list(summary.get("target_files"))
        referenced_paths = scaffold_target_files or self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
            "\n".join(self._normalize_string_list(summary.get("acceptance_checks"))),
            "\n".join(self._normalize_string_list(summary.get("touched_files"))),
            limit=3,
        )
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))

        files: list[dict[str, Any]] = []
        evidence_parts = [
            summary.get("summary") or "",
            summary.get("last_validation") or "",
            "\n".join(self._normalize_string_list(summary.get("validation_checks"))),
            "\n".join(validation_artifacts),
            "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
            handoff_text,
        ]

        for raw_path in referenced_paths[:3]:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            file_record: dict[str, Any] = {
                "path": raw_path,
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
            }
            if resolved.exists() and resolved.is_file():
                excerpt = self._format_numbered_excerpt(resolved, max_lines=120, max_chars=3600)
                file_record["excerpt"] = excerpt
                evidence_parts.append(excerpt)
            else:
                file_record["excerpt"] = "[missing file]"
                evidence_parts.append(f"{raw_path}: missing file")
            files.append(file_record)

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        combined_evidence = "\n".join(part for part in evidence_parts if part)
        coverage = self._build_acceptance_check_coverage(acceptance_checks, combined_evidence)

        return {
            "summary": summary,
            "handoff_excerpt": handoff_text,
            "files": files,
            "validation_artifacts": validation_artifacts,
            "acceptance_evidence": acceptance_evidence,
            "acceptance_check_coverage": coverage,
        }

    def can_use_harness_generator_patch_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        if not self.should_use_harness_generator_structured_mode(project_path, prompt):
            return False
        bundle = self.build_harness_generator_structured_bundle(project_path, prompt)
        files = bundle.get("files") or []
        if len(files) != 1:
            return False
        file_item = files[0]
        if not bool(file_item.get("exists")):
            return False
        summary = bundle.get("summary") or {}
        line_hints = self._normalize_string_list(summary.get("target_line_hints"))
        if self._extract_line_hint_window(str(file_item.get("path") or ""), line_hints):
            return True
        resolved_path = Path(str(file_item.get("resolved_path") or ""))
        try:
            return resolved_path.stat().st_size <= 9000
        except OSError:
            return False

    def can_use_harness_generator_repair_mode(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> bool:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        if not self._harness_generator_needs_frontier_repair(target_path):
            return False
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        files = bundle.get("files") or []
        if len(files) != 1:
            return False
        file_item = files[0]
        if not bool(file_item.get("exists")):
            return False
        return bool(self._extract_harness_repair_context(target_path, file_item, bundle.get("summary") or {}))

    def _extract_harness_repair_traceback(
        self,
        project_path: str,
        file_item: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_path = Path(str(file_item.get("resolved_path") or "")).resolve()
        candidate_labels = {
            str(file_item.get("path") or "").replace("\\", "/"),
            resolved_path.name,
            str(resolved_path),
        }
        combined_sources = [
            *self._normalize_string_list(summary.get("validation_artifacts")),
            *self._normalize_string_list(summary.get("findings")),
            *self._normalize_string_list(summary.get("required_revisions")),
            str(summary.get("last_validation") or ""),
        ]
        combined = "\n".join(part for part in combined_sources if str(part or "").strip())

        best_line = 0
        best_path = ""
        for match in re.finditer(r'File\s+"([^"]+)",\s+line\s+(\d+)', combined):
            raw_path = str(match.group(1) or "").strip()
            line_number = int(match.group(2))
            normalized = raw_path.replace("\\", "/")
            if normalized in candidate_labels or Path(normalized).name in candidate_labels:
                best_path = raw_path
                best_line = line_number
                break

        error_line = ""
        for raw_line in combined.splitlines():
            stripped = raw_line.strip()
            lowered = stripped.lower()
            if any(
                token in lowered
                for token in (
                    "syntaxerror",
                    "indentationerror",
                    "unexpected indent",
                    "expected an indented block",
                    "invalid syntax",
                    "parse error",
                    "runtimeerror",
                    "importerror",
                    "nameerror",
                    "typeerror",
                    "attributeerror",
                )
            ):
                error_line = stripped
                break

        return {
            "line_number": best_line,
            "path": best_path,
            "error_line": error_line,
            "combined": combined,
        }

    def _extract_harness_repair_context(
        self,
        project_path: str,
        file_item: dict[str, Any],
        summary: dict[str, Any],
    ) -> dict[str, Any]:
        resolved_path = Path(str(file_item.get("resolved_path") or ""))
        traceback_data = self._extract_harness_repair_traceback(project_path, file_item, summary)
        line_number = int(traceback_data.get("line_number") or 0)
        if line_number > 0:
            file_context = self._format_numbered_window(
                resolved_path,
                start_line=line_number,
                end_line=line_number,
                padding=8,
                max_lines=48,
                max_chars=1800,
            )
        else:
            file_context = self._format_numbered_excerpt(
                resolved_path,
                max_lines=48,
                max_chars=1800,
            )
        return {
            "line_number": line_number,
            "error_line": str(traceback_data.get("error_line") or "").strip(),
            "combined": str(traceback_data.get("combined") or "").strip(),
            "file_context": file_context,
            "edit_snippets": self._extract_edit_snippet_artifacts(summary),
        }

    def build_harness_generator_repair_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        summary = bundle["summary"]
        file_item = (bundle.get("files") or [{}])[0]
        repair = self._extract_harness_repair_context(target_path, file_item, summary)
        validation_commands = self._normalize_harness_validation_commands(
            summary.get("validation_commands"),
            project_path=target_path,
        )
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))
        error_line = repair.get("error_line") or "unknown runtime/parse error"
        error_trace = self._truncate_text(str(repair.get("error_line") or ""), max_chars=220)
        lines = [
            "Generator repair mode for a blocked single-file sprint.",
            "Fix only the concrete failure shown below.",
            "Edit only the target file and keep the original sprint scope.",
            "Do not re-plan, do not explore the repo, and do not rewrite unrelated parts of the file.",
            "Repair only the failed edit window or its immediate surroundings.",
            "Do not touch the shebang, __future__ import, or unrelated imports unless the failure is on that exact line.",
            "Use file_edit for the patch and at most one cheap bash validation command.",
            "Use the suggested validation command exactly as written after the repair.",
            "",
            f"Target file: {file_item.get('path') or '(unknown)'}",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Blocking failure: {error_line}",
        ]
        if repair.get("line_number"):
            lines.append(f"Repair focus line: {repair['line_number']}")
        lines.extend(
            [
                "",
                "Required revision:",
            ]
        )
        lines.extend(f"- {item}" for item in revisions[:3] or ["Fix the blocking failure and keep the sprint intent intact."])
        lines.extend(
            [
                "",
                "Keep this intended behavior after the repair:",
            ]
        )
        lines.extend(f"- {item}" for item in deliverables[:3] or ["(none)"])
        lines.extend(
            [
                "",
                "Acceptance checks to preserve:",
            ]
        )
        lines.extend(f"- {item}" for item in checks[:4] or ["(none)"])
        if validation_commands:
            lines.extend(["", "Suggested validation command (use exactly this command):"])
            lines.append(f"- {validation_commands[0]}")
        edit_snippets = repair.get("edit_snippets") or []
        if edit_snippets:
            lines.extend(["", "Last attempted edit snippets:"])
            lines.extend(edit_snippets[:4])
        lines.extend(
            [
                "",
                "Recent failure evidence:",
                error_trace or "(none)",
                "",
                "Current target file excerpt:",
                repair.get("file_context") or "[missing file excerpt]",
                "",
                "Required output behavior:",
                "- apply the minimal repair directly",
                "- record the exact touched file",
                "- record one concise validation summary",
                "- map satisfied checks into acceptance_evidence",
                "- finish with a valid ```resonant-harness JSON block for generator_update",
            ]
        )
        return "\n".join(lines)

    def build_harness_generator_patch_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        summary = bundle["summary"]
        file_item = (bundle.get("files") or [{}])[0]
        resolved_path = Path(str(file_item.get("resolved_path") or ""))
        line_hints = self._normalize_string_list(summary.get("target_line_hints"))
        validation_commands = self._normalize_harness_validation_commands(
            summary.get("validation_commands"),
            project_path=target_path,
        )
        edit_strategy = str(summary.get("edit_strategy") or "").strip()
        scaffold_notes = self._extract_patch_scaffold(str(file_item.get("path") or ""), line_hints)
        hint_window = self._extract_line_hint_window(str(file_item.get("path") or ""), line_hints)
        if hint_window:
            start_line, end_line, raw_hint = hint_window
            file_context = self._format_numbered_window(
                resolved_path,
                start_line=start_line,
                end_line=end_line,
                padding=12,
                max_lines=96,
                max_chars=3400,
            )
        elif scaffold_notes:
            raw_hint = ""
            file_context = self._format_anchor_windows(
                resolved_path,
                scaffold_notes,
                padding=10,
                max_windows=3,
                max_chars=3400,
            ) or self._format_numbered_excerpt(resolved_path, max_lines=80, max_chars=3000)
        else:
            raw_hint = ""
            file_context = self._format_numbered_excerpt(resolved_path, max_lines=100, max_chars=3800)
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))

        lines = [
            "Single-file patch generator mode.",
            "Make the smallest patch that satisfies the sprint.",
            "Edit only the target file shown below.",
            "Do not explore the repo or open unrelated files.",
            "Do not rewrite the file prologue, shebang, __future__ imports, or unrelated import blocks unless the acceptance checks require it.",
            "Prefer the smallest local replacement over generating new scaffolding or broad rewrites.",
            "Use file_edit for the patch and at most one cheap bash validation command if it is obvious.",
            "If the file context below is insufficient or the change needs another file, stop and record a blocker instead.",
            "",
            f"Target file: {file_item.get('path') or '(unknown)'}",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            *( [f"Line hint: {raw_hint}"] if raw_hint else [] ),
            *( [f"Edit strategy: {edit_strategy}"] if edit_strategy else [] ),
            *(["Patch scaffold:"] + [f"- {item}" for item in scaffold_notes] if scaffold_notes else []),
            "",
            "Deliverables:",
        ]
        lines.extend(f"- {item}" for item in deliverables[:4] or ["(none)"])
        lines.extend(["", "Acceptance checks:"])
        lines.extend(f"- {item}" for item in checks[:4] or ["(none)"])
        if validation_commands:
            lines.extend(["", "Suggested validation commands:"])
            lines.extend(f"- {item}" for item in validation_commands[:1])
        lines.extend(
            [
                "",
                "Required output behavior:",
                "- apply the patch directly",
                "- record the exact touched file",
                "- record one concise validation summary",
                "- map satisfied checks into acceptance_evidence",
                "- finish with a valid ```resonant-harness JSON block for generator_update",
                "",
                "Target file contents:",
                file_context or "[missing file excerpt]",
            ]
        )
        return "\n".join(lines)

    def build_harness_generator_structured_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        bundle = self.build_harness_generator_structured_bundle(target_path, prompt)
        summary = bundle["summary"]
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        blockers = self._normalize_string_list(summary.get("blockers"))
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))
        next_steps = self._normalize_string_list(summary.get("next_steps"))
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))

        lines = [
            "Compact structured generator mode for a small code-changing sprint.",
            "Keep the implementation narrowly scoped to the referenced files below unless you hit a real blocker.",
            "Use the provided file excerpts first, then use tools only if needed.",
            "Allowed tools are limited to file_read, file_edit, and cheap bash validation commands.",
            "Do not broaden into repo-wide exploration; if the sprint cannot be completed within the shown file scope, record a blocker instead.",
            "After making changes, record exact touched_files, concise validation checks, validation artifacts, and acceptance evidence aligned to the contract checks.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            "",
            "Deliverables:",
        ]
        lines.extend(f"- {item}" for item in deliverables[:6] or ["(none)"])
        lines.append("")
        lines.append("Acceptance checks:")
        lines.extend(f"- {item}" for item in checks[:8] or ["(none)"])

        if blockers:
            lines.append("")
            lines.append("Current blockers:")
            lines.extend(f"- {item}" for item in blockers[:6])
        if next_steps:
            lines.append("")
            lines.append("Current next steps:")
            lines.extend(f"- {item}" for item in next_steps[:6])
        if revisions:
            lines.append("")
            lines.append("Required revisions from evaluator:")
            lines.extend(f"- {item}" for item in revisions[:6])

        lines.extend(
            [
                "",
                "Current harness summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
            ]
        )
        if validation_artifacts:
            lines.append("")
            lines.append("Existing validation artifacts:")
            lines.extend(f"- {item}" for item in validation_artifacts[:8])
        if acceptance_evidence:
            lines.append("")
            lines.append("Existing acceptance evidence:")
            lines.extend(
                f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
                for check, evidence in list(acceptance_evidence.items())[:8]
            )

        lines.extend(
            [
                "",
                "Existing handoff excerpt:",
                bundle["handoff_excerpt"] or "(none)",
                "",
                "Acceptance-check coverage guess from current evidence:",
            ]
        )
        for item in bundle["acceptance_check_coverage"]:
            marker = "matched" if item.get("matched") else "unmatched"
            lines.append(f"- {marker}: {item.get('check') or '(unknown)'}")

        lines.append("")
        lines.append("Referenced file excerpts:")
        for file_item in bundle["files"]:
            lines.append(f"- {file_item['path']} ({'exists' if file_item['exists'] else 'missing'})")
            excerpt = str(file_item.get("excerpt") or "").strip()
            if excerpt:
                lines.append(excerpt)
                lines.append("")

        lines.extend(
            [
                "Keep the prose concise.",
                "Then finish with a valid ```resonant-harness JSON block for generator_update that includes:",
                "- progress.summary",
                "- progress.last_validation",
                "- progress.touched_files",
                "- progress.validation_checks",
                "- progress.validation_artifacts",
                "- progress.acceptance_evidence",
                "- handoff_markdown with concise implementation notes and file references",
                "- sprint_status set to implemented if the sprint is done, otherwise needs_revision or failed if blocked",
            ]
        )
        return "\n".join(lines)

    def build_harness_generator_artifact_prompt(
        self,
        project_path: Optional[str] = None,
        prompt: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        bundle = self.build_harness_generator_artifact_bundle(target_path, prompt)
        summary = bundle["summary"]
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        deliverables = self._normalize_string_list(summary.get("deliverables"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        blockers = self._normalize_string_list(summary.get("blockers"))
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))

        lines = [
            "Artifact-only generator mode.",
            "Do not modify repository files.",
            "Use this mode to capture grounded findings or missing evidence without reopening edits.",
            "Do not use tools unless the runtime already supplied enough artifacts to answer honestly.",
            "Use only the harness context and file excerpts below.",
            "Capture concise, grounded findings in handoff/progress artifacts.",
            "If a check is not supported by the provided evidence, do not invent it; note the gap honestly.",
            "Set sprint_status to implemented once the artifact update is recorded.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            "",
            "Deliverables:",
        ]
        lines.extend(f"- {item}" for item in deliverables[:6] or ["(none)"])
        lines.append("")
        lines.append("Acceptance checks:")
        lines.extend(f"- {item}" for item in checks[:8] or ["(none)"])

        if blockers:
            lines.append("")
            lines.append("Current blockers:")
            lines.extend(f"- {item}" for item in blockers[:6])
        if revisions:
            lines.append("")
            lines.append("Required revisions from evaluator:")
            lines.extend(f"- {item}" for item in revisions[:6])

        lines.extend(
            [
                "",
                "Current harness summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
            ]
        )
        if validation_artifacts:
            lines.append("")
            lines.append("Existing validation artifacts:")
            lines.extend(f"- {item}" for item in validation_artifacts[:8])
        if acceptance_evidence:
            lines.append("")
            lines.append("Existing acceptance evidence:")
            lines.extend(
                f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
                for check, evidence in list(acceptance_evidence.items())[:8]
            )
        lines.extend(
            [
                "",
                "Existing handoff excerpt:",
                bundle["handoff_excerpt"] or "(none)",
                "",
                "Acceptance-check coverage guess from the included evidence:",
            ]
        )
        for item in bundle["acceptance_check_coverage"]:
            marker = "matched" if item.get("matched") else "unmatched"
            lines.append(f"- {marker}: {item.get('check') or '(unknown)'}")

        lines.append("")
        lines.append("Referenced file excerpts:")
        for file_item in bundle["files"]:
            lines.append(f"- {file_item['path']} ({'exists' if file_item['exists'] else 'missing'})")
            excerpt = str(file_item.get("excerpt") or "").strip()
            if excerpt:
                lines.append(excerpt)
                lines.append("")

        lines.extend(
            [
                "Keep the prose to at most 6 short lines.",
                "Then finish with a valid ```resonant-harness JSON block for generator_update that includes:",
                "- progress.summary",
                "- progress.last_validation",
                "- progress.validation_checks",
                "- progress.validation_artifacts",
                "- progress.acceptance_evidence",
                "- handoff_markdown with concise findings and file references",
                "- sprint_status set to implemented",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    @staticmethod
    def _strip_list_marker(value: str) -> str:
        return re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s*", "", str(value or "").strip())

    def _choose_supporting_line_for_check(self, check: str, lines: list[str]) -> str:
        check_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", str(check or "").lower())
            if len(token) > 2 and token not in {
                "the", "and", "with", "that", "this", "from", "into", "then", "what",
                "when", "where", "which", "does", "have", "names", "includes", "include",
                "explains", "primary", "concrete", "visible", "code", "main",
            }
        }
        if not check_tokens:
            return ""

        best_line = ""
        best_score = 0
        for raw_line in lines:
            line = self._strip_list_marker(raw_line)
            if not line:
                continue
            line_tokens = set(re.findall(r"[a-z0-9]+", line.lower()))
            score = len(check_tokens & line_tokens)
            if score > best_score:
                best_score = score
                best_line = line

        minimum = max(2, min(len(check_tokens), 3))
        return best_line if best_score >= minimum else ""

    def align_acceptance_evidence_to_contract(
        self,
        *,
        acceptance_checks: list[str],
        evidence: dict[str, str],
        objective: str = "",
        user_request: str = "",
    ) -> dict[str, str]:
        if not acceptance_checks or not evidence:
            return {}

        normalized_exact = {
            self._normalize_acceptance_check_phrase(key): str(value).strip()
            for key, value in evidence.items()
            if self._normalize_acceptance_check_phrase(key) and str(value).strip()
        }
        raw_entries = [
            (str(key).strip(), str(value).strip())
            for key, value in evidence.items()
            if str(key).strip() and str(value).strip()
        ]
        aligned: dict[str, str] = {}

        for check in acceptance_checks:
            phrase = self._normalize_acceptance_check_phrase(check)
            if phrase and phrase in normalized_exact:
                aligned[check] = self._truncate_text(normalized_exact[phrase], max_chars=220)
                continue

            best_value = ""
            best_score = 0
            check_tokens = set(re.findall(r"[a-z0-9]+", phrase.replace("_", " ")))
            for raw_key, raw_value in raw_entries:
                key_tokens = set(re.findall(r"[a-z0-9]+", raw_key.lower().replace("_", " ")))
                score = len(check_tokens & key_tokens)
                if score > best_score:
                    best_score = score
                    best_value = raw_value
            if best_value and best_score >= 2:
                aligned[check] = self._truncate_text(best_value, max_chars=220)

        if self._is_read_only_harness_request(objective, user_request):
            for check in acceptance_checks:
                lowered = check.lower()
                if (
                    check not in aligned
                    and "no repository files" in lowered
                    and ("read-only" in lowered or "repository files" in lowered or "modified" in lowered)
                ):
                    aligned[check] = "Artifact-only read-only sprint; no repository files were modified."

        return aligned

    def infer_generator_artifact_payload(
        self,
        *,
        project_path: Optional[str] = None,
        text: str,
        prompt: str = "",
    ) -> dict[str, Any] | None:
        stripped = str(text or "").strip()
        if not stripped:
            return None

        target_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(target_path)
        sprint_id = str(summary.get("active_sprint_id") or "").strip()
        if not sprint_id:
            return None
        if not self.should_use_harness_generator_artifact_mode(target_path, prompt):
            return None

        lines = [
            self._strip_list_marker(line)
            for line in stripped.splitlines()
            if self._strip_list_marker(line)
        ]
        if not lines:
            return None

        findings: list[str] = []
        seen_findings: set[str] = set()
        for line in lines:
            normalized = line.strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen_findings:
                continue
            seen_findings.add(lowered)
            findings.append(self._truncate_text(normalized, max_chars=260))
            if len(findings) >= 6:
                break
        if not findings:
            return None

        referenced_files = self.extract_harness_referenced_files(
            target_path,
            prompt,
            summary.get("contract_objective", ""),
            "\n".join(self._normalize_string_list(summary.get("deliverables"))),
        )
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        acceptance_evidence: dict[str, str] = {}
        for check in acceptance_checks[:8]:
            supporting = self._choose_supporting_line_for_check(check, findings)
            if supporting:
                acceptance_evidence[check] = self._truncate_text(supporting, max_chars=220)

        validation_checks = []
        if referenced_files:
            validation_checks.append(f"Reviewed read-only evidence in: {', '.join(referenced_files[:4])}")
        validation_checks.extend(findings[:3])
        validation_artifacts = []
        if referenced_files:
            validation_artifacts.append(
                f"Artifact-only read-only audit captured findings from {', '.join(referenced_files[:4])}."
            )
        validation_artifacts.append("Findings were recorded in handoff_markdown and harness progress fields.")

        summary_text = self._truncate_text(" | ".join(findings[:2]), max_chars=220)
        handoff_lines = [
            "# Read-only audit handoff",
            "",
            "## Summary",
            summary_text or "Completed a read-only artifact audit.",
            "",
        ]
        if referenced_files:
            handoff_lines.append("## Referenced files")
            handoff_lines.extend(f"- `{path}`" for path in referenced_files[:6])
            handoff_lines.append("")
        handoff_lines.append("## Findings")
        handoff_lines.extend(f"- {item}" for item in findings)

        return {
            "action": "generator_update",
            "progress": {
                "summary": summary_text or "Completed the read-only sprint with grounded audit findings.",
                "last_validation": "Completed an artifact-only read-only audit over the referenced file excerpts.",
                "validation_checks": validation_checks[:6],
                "validation_artifacts": validation_artifacts[:4],
                "acceptance_evidence": acceptance_evidence,
                "touched_files": referenced_files[:6],
                "current_phase": "implementation",
            },
            "handoff_markdown": "\n".join(handoff_lines),
            "sprint_status": "implemented",
        }

    @staticmethod
    @staticmethod
    def _extract_tool_arguments_from_event(event: dict[str, Any]) -> dict[str, Any]:
        args = event.get("arguments", {})
        if isinstance(args, dict):
            return args
        if isinstance(args, str):
            try:
                parsed = json.loads(args)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
        return {}

    def extract_generator_structured_event_summary(
        self,
        *,
        project_path: Optional[str] = None,
        display_events: list[dict[str, Any]],
    ) -> tuple[list[str], list[str]]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        touched_files: list[str] = []
        seen_files: set[str] = set()
        validation_artifacts: list[str] = []
        seen_artifacts: set[str] = set()

        for event in display_events:
            if event.get("event") == EngineEvent.TOOL_CALL.value:
                tool_name = str(event.get("name") or "").strip()
                arguments = self._extract_tool_arguments_from_event(event)
                if tool_name in {"file_edit", "file_write"}:
                    raw_path = str(arguments.get("path") or "").strip()
                    if raw_path:
                        display_path = os.path.relpath(
                            str(self._resolve_harness_touched_path(target_path, raw_path)),
                            target_path,
                        ).replace(os.sep, "/")
                        if display_path not in seen_files:
                            seen_files.add(display_path)
                            touched_files.append(display_path)
                elif tool_name == "bash":
                    command = self._truncate_text(str(arguments.get("command") or "").strip(), max_chars=120)
                    if command:
                        artifact = f"Ran validation command: {command}"
                        if artifact not in seen_artifacts:
                            seen_artifacts.add(artifact)
                            validation_artifacts.append(artifact)
            elif event.get("event") == EngineEvent.TOOL_RESULT.value:
                tool_name = str(event.get("name") or "").strip()
                if tool_name == "bash":
                    output = self._truncate_text(str(event.get("output") or "").strip(), max_chars=220)
                    if output:
                        artifact = f"Bash result: {output}"
                        if artifact not in seen_artifacts:
                            seen_artifacts.add(artifact)
                            validation_artifacts.append(artifact)

        return touched_files[:6], validation_artifacts[:6]

    def extract_generator_edit_snippets(
        self,
        *,
        project_path: Optional[str] = None,
        display_events: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        snippets: list[dict[str, str]] = []
        seen_files: set[str] = set()

        for event in display_events:
            if event.get("event") != EngineEvent.TOOL_CALL.value:
                continue
            if str(event.get("name") or "").strip() != "file_edit":
                continue
            arguments = self._extract_tool_arguments_from_event(event)
            raw_path = str(arguments.get("path") or "").strip()
            if not raw_path:
                continue
            display_path = os.path.relpath(
                str(self._resolve_harness_touched_path(target_path, raw_path)),
                target_path,
            ).replace(os.sep, "/")
            if display_path in seen_files:
                continue
            seen_files.add(display_path)
            snippets.append(
                {
                    "path": display_path,
                    "old_text": self._truncate_text(str(arguments.get("old_text") or "").strip(), max_chars=500),
                    "new_text": self._truncate_text(str(arguments.get("new_text") or "").strip(), max_chars=500),
                }
            )
            if len(snippets) >= 2:
                break

        return snippets

    def _preferred_harness_python(self, project_path: Optional[str] = None) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path or os.getcwd())
        candidates = [
            Path(target_path) / ".venv" / "bin" / "python",
            Path(target_path) / ".venv" / "Scripts" / "python.exe",
            Path(sys.executable).resolve(),
        ]
        for candidate in candidates:
            try:
                if candidate.exists():
                    return str(candidate)
            except OSError:
                continue
        return "python3"

    def _sanitize_harness_validation_command(
        self,
        command: str,
        *,
        project_path: Optional[str] = None,
    ) -> str:
        cleaned = re.sub(r"\s+#.*$", "", str(command or "").strip()).strip()
        preferred_python = self._preferred_harness_python(project_path)
        for prefix in ("python3.11 ", "python3 ", "python "):
            if cleaned.startswith(prefix):
                cleaned = f"{preferred_python} {cleaned[len(prefix):]}"
                break
        return cleaned

    @staticmethod
    @staticmethod
    def _extract_validation_artifact_candidates(command: str) -> list[str]:
        candidates = re.findall(r"--(?:summary-output|summary|output)\s+([^\s|]+)", command)
        if "validation_summary.json" in command and "validation_summary.json" not in candidates:
            candidates.append("validation_summary.json")
        return candidates[:3]

    @staticmethod
    def _extract_harness_target_function_name(self, summary: dict[str, Any]) -> str:
        candidates = [
            *self._normalize_string_list(summary.get("acceptance_checks")),
            *self._normalize_string_list(summary.get("deliverables")),
            str(summary.get("contract_objective") or ""),
        ]
        patterns = (
            r"\bfunction\s+`?([A-Za-z_][A-Za-z0-9_]*)`?\b",
            r"`([A-Za-z_][A-Za-z0-9_]*)\([^`]*\)`",
            r"\b([A-Za-z_][A-Za-z0-9_]*)\([^)]*\)\s+function\b",
        )
        for text in candidates:
            stripped = str(text or "").strip()
            if not stripped:
                continue
            for pattern in patterns:
                match = re.search(pattern, stripped, re.IGNORECASE)
                if match:
                    return str(match.group(1) or "").strip()
        return ""

    def _infer_static_acceptance_evidence(
        self,
        *,
        acceptance_checks: list[str],
        summary: dict[str, Any],
    ) -> dict[str, str]:
        target_files = {
            item.replace("\\", "/")
            for item in self._normalize_string_list(summary.get("target_files"))
        }
        touched_files = {
            item.replace("\\", "/")
            for item in self._normalize_string_list(summary.get("touched_files"))
        }
        aligned: dict[str, str] = {}
        for check in acceptance_checks:
            lowered = check.lower()
            if (
                "no other files" in lowered
                and "modified" in lowered
                and touched_files
                and (not target_files or touched_files.issubset(target_files))
            ):
                aligned[check] = self._truncate_text(
                    "Only recorded touched files: " + ", ".join(sorted(touched_files)),
                    max_chars=220,
                )
        return aligned

    def _acceptance_evidence_covers_contract(
        self,
        *,
        acceptance_checks: list[str],
        evidence: dict[str, str],
    ) -> bool:
        if not acceptance_checks:
            return False
        normalized = {
            self._normalize_acceptance_check_phrase(key)
            for key, value in evidence.items()
            if self._normalize_acceptance_check_phrase(key) and str(value or "").strip()
        }
        return all(
            self._normalize_acceptance_check_phrase(check) in normalized
            for check in acceptance_checks
        )

    def _build_derived_harness_validation_commands(
        self,
        *,
        project_path: Optional[str] = None,
        summary: dict[str, Any],
    ) -> list[str]:
        target_files = self._normalize_string_list(summary.get("target_files")) or self._normalize_string_list(
            summary.get("touched_files")
        )
        if len(target_files) != 1:
            return []
        target_file = target_files[0]
        module_basename = Path(target_file).stem
        module_dir = Path(target_file).parent.as_posix() or "."
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module_basename):
            return []
        function_name = self._extract_harness_target_function_name(summary)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", function_name):
            return []

        python_bin = shlex.quote(self._preferred_harness_python(project_path))
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        derived: list[str] = []
        for check in acceptance_checks:
            lowered = check.lower()
            if "callable" in lowered:
                code = (
                    f"import pathlib, sys; sys.path.insert(0, str(pathlib.Path({module_dir!r}).resolve())); "
                    f"from {module_basename} import {function_name}; "
                    f"print('CALLABLE_OK' if callable({function_name}) else 'CALLABLE_FAIL')"
                )
                derived.append(f"{python_bin} -c {shlex.quote(code)}")
            if "empty dict" in lowered and "no duplicates" in lowered:
                code = (
                    f"import pathlib, sys; sys.path.insert(0, str(pathlib.Path({module_dir!r}).resolve())); "
                    f"from {module_basename} import {function_name}; "
                    f"result = {function_name}([{{'id': 'alpha'}}, {{'id': 'beta'}}]); "
                    "print('EMPTY_OK' if result == {} else repr(result))"
                )
                derived.append(f"{python_bin} -c {shlex.quote(code)}")

        deduped: list[str] = []
        for command in derived:
            if command not in deduped:
                deduped.append(command)
        return deduped[:2]

    @staticmethod
    @staticmethod
    def _validation_command_has_placeholder(command: str) -> bool:
        text = str(command or "")
        return bool(re.search(r"<[^>\n]+>", text))

    def apply_generator_post_patch_safety_gate(
        self,
        *,
        project_path: Optional[str] = None,
        payload: dict[str, Any],
        generator_mode: str,
        display_events: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], str]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        if generator_mode not in {"patch", "repair", "structured"}:
            return payload, ""
        if not isinstance(payload, dict):
            return payload, ""

        generator_payload = payload.get("generator_update") if isinstance(payload.get("generator_update"), dict) else {}
        progress = payload.get("progress") if isinstance(payload.get("progress"), dict) else {}
        if not progress and isinstance(generator_payload.get("progress"), dict):
            progress = dict(generator_payload.get("progress") or {})
        else:
            progress = dict(progress or {})

        summary = self.get_harness_summary(target_path)
        raw_touched = self._normalize_string_list(progress.get("touched_files")) or self._normalize_string_list(
            summary.get("target_files")
        )
        python_targets: list[tuple[str, Path]] = []
        seen_targets: set[str] = set()
        for raw_path in raw_touched:
            resolved = self._resolve_harness_touched_path(target_path, raw_path)
            if resolved.suffix != ".py" or not resolved.exists() or not resolved.is_file():
                continue
            display_path = os.path.relpath(str(resolved), target_path).replace(os.sep, "/")
            if display_path in seen_targets:
                continue
            seen_targets.add(display_path)
            python_targets.append((display_path, resolved))
        if not python_targets:
            return payload, ""

        python_bin = self._preferred_harness_python(target_path)
        command = [python_bin, "-m", "py_compile", *[str(path) for _, path in python_targets[:2]]]
        try:
            completed = subprocess.run(
                command,
                cwd=target_path,
                text=True,
                capture_output=True,
                timeout=20,
                **background_process_kwargs(),
            )
            output = "\n".join(
                part for part in (str(completed.stdout or "").strip(), str(completed.stderr or "").strip()) if part
            ).strip()
        except Exception as exc:
            completed = None
            output = f"Failed to start syntax gate: {exc}"

        if completed is not None and completed.returncode == 0:
            return payload, ""

        edit_snippets = self.extract_generator_edit_snippets(project_path=target_path, display_events=display_events)
        validation_checks = self._normalize_string_list(progress.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(progress.get("validation_artifacts"))
        blockers = self._normalize_string_list(progress.get("blockers"))
        gate_output = self._truncate_text(output or "py_compile failed without output", max_chars=260)
        gate_command = " ".join(command)
        gate_message = self._truncate_text(f"Post-patch syntax gate failed: {gate_output}", max_chars=220)

        if gate_message not in validation_checks:
            validation_checks.append(gate_message)
        gate_artifacts = [
            self._truncate_text(f"Post-patch syntax gate command: {gate_command}", max_chars=220),
            gate_message,
        ]
        for snippet in edit_snippets:
            path = snippet.get("path") or "(unknown)"
            old_text = str(snippet.get("old_text") or "").strip()
            new_text = str(snippet.get("new_text") or "").strip()
            if old_text:
                gate_artifacts.append(f"Edited snippet before ({path}):\n{old_text}")
            if new_text:
                gate_artifacts.append(f"Edited snippet after ({path}):\n{new_text}")

        merged_artifacts: list[str] = []
        for artifact in [*gate_artifacts, *validation_artifacts]:
            if artifact and artifact not in merged_artifacts:
                merged_artifacts.append(artifact)

        blocker = "Fix the syntax/runtime failure before claiming implementation."
        if blocker not in blockers:
            blockers.append(blocker)

        summary_text = self._truncate_text(
            f"Patch introduced a blocking syntax/runtime failure in {python_targets[0][0]}. Repair is required.",
            max_chars=220,
        )
        progress.update(
            {
                "summary": summary_text,
                "last_validation": gate_message,
                "validation_checks": validation_checks[:8],
                "validation_artifacts": merged_artifacts[:8],
                "acceptance_evidence": {},
                "blockers": blockers[:4],
                "next_steps": [
                    "Repair the broken patch in the target file only.",
                    "Rerun the validation command after the repair.",
                ],
                "current_phase": "blocked",
            }
        )
        payload["progress"] = progress
        payload["sprint_status"] = "failed"
        payload["handoff_markdown"] = "\n".join(
            [
                "# Repair required",
                "",
                f"## Summary\n{summary_text}",
                "",
                "## Blocking validation",
                f"- {gate_message}",
                "",
                "## Target files",
                *[f"- `{path}`" for path, _ in python_targets[:4]],
            ]
        )
        if isinstance(generator_payload, dict):
            generator_payload["progress"] = progress
            generator_payload["sprint_status"] = "failed"
            generator_payload["handoff_markdown"] = payload.get("handoff_markdown", "")
            payload["generator_update"] = generator_payload

        return payload, gate_message

    @staticmethod
    @staticmethod
    def _extract_edit_snippet_artifacts(summary: dict[str, Any]) -> list[str]:
        snippets: list[str] = []
        for item in summary.get("validation_artifacts") or []:
            text = str(item or "").strip()
            if text.startswith("Edited snippet before") or text.startswith("Edited snippet after"):
                snippets.append(text)
        return snippets[:4]

    def run_harness_generator_validation_probes(
        self,
        *,
        project_path: Optional[str] = None,
        summary: dict[str, Any],
    ) -> tuple[list[str], list[str], dict[str, str]]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        commands = self._normalize_string_list(summary.get("validation_commands"))
        for command in self._build_derived_harness_validation_commands(project_path=target_path, summary=summary):
            if command not in commands:
                commands.append(command)
        if not commands:
            return [], [], self._infer_static_acceptance_evidence(
                acceptance_checks=acceptance_checks,
                summary=summary,
            )

        revision_focus = " ".join(
            self._normalize_string_list(summary.get("required_revisions"))
            + self._normalize_string_list(summary.get("next_steps"))
            + acceptance_checks
        ).lower()

        ranked_commands: list[tuple[int, str]] = []
        for index, raw_command in enumerate(commands):
            command = self._sanitize_harness_validation_command(raw_command, project_path=target_path)
            if not command:
                continue
            lowered = command.lower()
            score = 0
            if any(token in lowered for token in ("whitespace", "missing", "empty", "grep", "/tmp/test_", "echo '{")):
                score += 3
            if any(token in lowered for token in ("--summary-output", "--summary", "validation_summary.json")):
                score += 2
            if "whitespace" in revision_focus and "whitespace" in lowered:
                score += 3
            if "missing" in revision_focus and "missing" in lowered:
                score += 2
            if any(token in revision_focus for token in ("valid", "pass validation", "without errors")) and any(
                token in lowered for token in ("/tmp/test_valid", "valid")
            ):
                score += 2
            if any(token in lowered for token in ("python", "pytest", "uv", "bash")):
                score += 1
            score += max(0, 2 - index)
            ranked_commands.append((score, command))

        preferred: list[str] = []
        for _, command in sorted(ranked_commands, key=lambda item: (-item[0], item[1])):
            if command not in preferred:
                preferred.append(command)

        validation_checks: list[str] = []
        validation_artifacts: list[str] = []
        acceptance_evidence: dict[str, str] = self._infer_static_acceptance_evidence(
            acceptance_checks=acceptance_checks,
            summary=summary,
        )

        for command in preferred[:3]:
            if self._validation_command_has_placeholder(command):
                validation_artifacts.append(
                    self._truncate_text(
                        f"Skipped placeholder validation command: {command}",
                        max_chars=220,
                    )
                )
                continue
            try:
                completed = subprocess.run(
                    command,
                    shell=True,
                    cwd=target_path,
                    text=True,
                    capture_output=True,
                    timeout=25,
                    **background_process_kwargs(),
                )
            except Exception as exc:
                validation_artifacts.append(self._truncate_text(f"Auto validation failed to start: {exc}", max_chars=220))
                continue

            output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part).strip()
            output_lower = output.lower()
            unusable_failure = completed.returncode != 0 and any(
                token in output_lower
                for token in (
                    "syntax error near unexpected token",
                    "command not found",
                    "no such file or directory",
                )
            )
            validation_artifacts.append(self._truncate_text(f"Auto validation command: {command}", max_chars=220))
            validation_artifacts.append(
                self._truncate_text(
                    f"Auto validation exit={completed.returncode}: {output or '[no output]'}",
                    max_chars=260,
                )
            )

            if completed.returncode == 0:
                validation_checks.append(self._truncate_text(f"Validation succeeded: {command}", max_chars=160))
                if "CALLABLE_OK" in output:
                    for check in acceptance_checks:
                        if "callable" in check.lower():
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` confirmed the function is callable.", max_chars=180),
                            )
                if "EMPTY_OK" in output:
                    for check in acceptance_checks:
                        lowered = check.lower()
                        if "empty dict" in lowered and "no duplicates" in lowered:
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` returned {{}} for non-duplicate input.", max_chars=180),
                            )
                if re.search(r"\b[a-z0-9._-]+\s*:\s*\d+\b", output, re.IGNORECASE):
                    for check in acceptance_checks:
                        lowered = check.lower()
                        if "occurrence count" in lowered or "count for each duplicated id" in lowered:
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` printed duplicate ids with counts.", max_chars=180),
                            )
                        if "duplicate id" in lowered or "duplicate ids" in lowered:
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{command}` printed duplicate ids in the validation output.", max_chars=180),
                            )
                for check in acceptance_checks:
                    lowered = check.lower()
                    if any(token in lowered for token in ("exits without error", "without error", "exit without error")):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited 0.", max_chars=140),
                        )
                    if any(token in lowered for token in ("validates successfully", "exit code 0", "no change in behavior")):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited 0.", max_chars=140),
                        )
                    if any(token in lowered for token in ("valid training", "pass validation", "without errors")) and any(
                        token in command.lower() for token in ("test_valid", "valid")
                    ):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited 0 for the valid fixture.", max_chars=160),
                        )
            else:
                validation_checks.append(
                    self._truncate_text(f"Validation failed with exit {completed.returncode}: {command}", max_chars=180)
                )
                if unusable_failure:
                    continue
                for check in acceptance_checks:
                    lowered = check.lower()
                    if any(token in lowered for token in ("non-zero exit code", "exit with error", "exit code is non-zero")):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` exited {completed.returncode}.", max_chars=140),
                        )
                    if "whitespace" in lowered and "whitespace" in output_lower:
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` reported whitespace-only content.", max_chars=160),
                        )
                    if any(token in lowered for token in ("missing", "empty content", "empty strings", "empty")) and any(
                        token in output_lower for token in ("missing", "empty")
                    ):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` reported missing or empty content.", max_chars=160),
                        )
                    if "line number" in lowered and re.search(r":\d+:", output):
                        acceptance_evidence.setdefault(
                            check,
                            self._truncate_text(f"`{command}` reported a file:line diagnostic.", max_chars=160),
                        )

            for raw_candidate in self._extract_validation_artifact_candidates(command):
                resolved = self._resolve_harness_touched_path(target_path, raw_candidate)
                if not resolved.exists() or not resolved.is_file():
                    continue
                rel_path = os.path.relpath(str(resolved), target_path).replace(os.sep, "/")
                validation_artifacts.append(f"Validation artifact created: {rel_path}")
                try:
                    payload = json.loads(resolved.read_text(encoding="utf-8"))
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    keys = sorted(str(key) for key in payload.keys())
                    validation_checks.append(self._truncate_text(f"{rel_path} keys: {', '.join(keys)}", max_chars=180))
                    for check in acceptance_checks:
                        lowered = check.lower()
                        if any(token in lowered for token in ("json file is created", "summary json", "artifact is written", "artifact is produced")):
                            acceptance_evidence.setdefault(
                                check,
                                self._truncate_text(f"`{rel_path}` was created and parsed as JSON.", max_chars=160),
                            )
                        if "counts are accurate" in lowered:
                            total = payload.get("total_files_checked") or payload.get("total_files")
                            passed = payload.get("passed_files")
                            failed = payload.get("failed_files")
                            if all(isinstance(item, int) for item in (total, passed, failed)) and passed + failed == total:
                                acceptance_evidence.setdefault(
                                    check,
                                    f"`{rel_path}` reports total={total}, passed={passed}, failed={failed}.",
                                )

        return validation_checks[:6], validation_artifacts[:6], acceptance_evidence

    def infer_generator_structured_payload(
        self,
        *,
        project_path: Optional[str] = None,
        text: str,
        prompt: str = "",
        display_events: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(target_path)
        sprint_id = str(summary.get("active_sprint_id") or "").strip()
        if not sprint_id:
            return None
        if not self.should_use_harness_generator_structured_mode(target_path, prompt):
            return None

        touched_files, event_artifacts = self.extract_generator_structured_event_summary(
            project_path=target_path,
            display_events=display_events,
        )
        if not touched_files:
            return None

        stripped = str(text or "").strip()
        lines = [
            self._strip_list_marker(line)
            for line in stripped.splitlines()
            if self._strip_list_marker(line)
        ]
        findings: list[str] = []
        seen_findings: set[str] = set()
        for line in lines:
            normalized = line.strip()
            if not normalized:
                continue
            lowered = normalized.lower()
            if lowered in seen_findings:
                continue
            seen_findings.add(lowered)
            findings.append(self._truncate_text(normalized, max_chars=260))
            if len(findings) >= 6:
                break

        if not findings:
            findings = [f"Implemented the requested narrow change in {', '.join(touched_files[:3])}."]

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        acceptance_evidence: dict[str, str] = {}
        for check in acceptance_checks[:8]:
            supporting = self._choose_supporting_line_for_check(check, findings)
            if supporting:
                acceptance_evidence[check] = self._truncate_text(supporting, max_chars=220)

        validation_checks = [f"Updated file scope: {', '.join(touched_files[:4])}"]
        validation_checks.extend(findings[:3])
        validation_artifacts = list(event_artifacts[:4])
        if not validation_artifacts:
            validation_artifacts.append(
                f"Compact structured generator updated {', '.join(touched_files[:4])}."
            )
        combined_probe_seed = "\n".join(
            [
                "\n".join(findings),
                "\n".join(event_artifacts),
                "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
            ]
        )
        existing_coverage = self._build_acceptance_check_coverage(acceptance_checks, combined_probe_seed)
        should_probe = not event_artifacts or any(not item.get("matched") for item in existing_coverage)
        if should_probe:
            probe_checks, probe_artifacts, probe_evidence = self.run_harness_generator_validation_probes(
                project_path=target_path,
                summary=summary,
            )
            validation_checks.extend(item for item in probe_checks if item not in validation_checks)
            validation_artifacts.extend(item for item in probe_artifacts if item not in validation_artifacts)
            for check, evidence in probe_evidence.items():
                acceptance_evidence.setdefault(check, evidence)

        filtered_findings = [
            item
            for item in findings
            if item
            and item not in {"```json", "```", "{", "}"}
            and not item.startswith("\"name\":")
            and not item.startswith("\"arguments\":")
            and not item.startswith("\"path\":")
            and not item.startswith("\"old_text\":")
            and not item.startswith("\"new_text\":")
        ]
        findings = filtered_findings or [f"Implemented the requested narrow change in {', '.join(touched_files[:3])}."]

        summary_text = self._truncate_text(" | ".join(findings[:2]), max_chars=220)
        handoff_lines = [
            "# Structured implementation handoff",
            "",
            "## Summary",
            summary_text or "Completed a compact structured implementation update.",
            "",
            "## Touched files",
        ]
        handoff_lines.extend(f"- `{path}`" for path in touched_files[:6])
        handoff_lines.extend(["", "## Findings"])
        handoff_lines.extend(f"- {item}" for item in findings)

        return {
            "action": "generator_update",
            "progress": {
                "summary": summary_text or "Completed the compact structured sprint update.",
                "last_validation": event_artifacts[0] if event_artifacts else "Completed a compact structured implementation update.",
                "touched_files": touched_files[:6],
                "validation_checks": validation_checks[:6],
                "validation_artifacts": validation_artifacts[:6],
                "acceptance_evidence": acceptance_evidence,
                "current_phase": "implementation",
            },
            "handoff_markdown": "\n".join(handoff_lines),
            "sprint_status": "implemented",
        }

    def can_use_harness_structured_evaluator(self, project_path: Optional[str] = None) -> bool:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(target_path)
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        if not touched_files:
            return False
        bundle = self.build_harness_structured_evidence_bundle(target_path)
        return any(item.get("exists") for item in bundle["files"])

    def can_use_harness_explicit_artifact_evaluator(self, project_path: Optional[str] = None) -> bool:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(target_path)
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        if not acceptance_checks:
            return False
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
        if not acceptance_evidence:
            return False
        normalized_keys = {
            self._normalize_acceptance_check_phrase(check)
            for check in acceptance_evidence.keys()
            if self._normalize_acceptance_check_phrase(check)
        }
        covered = [
            check for check in acceptance_checks
            if self._normalize_acceptance_check_phrase(check) in normalized_keys
        ]
        if len(covered) != len(acceptance_checks):
            return False

        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        last_validation = str(summary.get("last_validation") or "").strip()
        return bool(last_validation or validation_checks or validation_artifacts)

    def get_harness_evaluator_strategy(self, project_path: Optional[str] = None) -> str:
        mode = self.get_harness_evaluator_mode()
        if mode == "full":
            return "full"
        if mode == "artifacts":
            return "artifacts"
        if mode == "structured":
            if self.can_use_harness_structured_evaluator(project_path):
                return "structured"
            if self.can_use_harness_explicit_artifact_evaluator(project_path):
                return "artifacts"
            return "full"
        if self.should_use_harness_artifact_evaluator(project_path):
            return "artifacts"
        if self.can_use_harness_structured_evaluator(project_path):
            return "structured"
        if self.can_use_harness_explicit_artifact_evaluator(project_path):
            return "artifacts"
        return "full"

    def build_harness_evaluator_artifact_prompt(self, project_path: Optional[str] = None) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        handoff_text = self._truncate_text(harness.read_handoff(), max_chars=1800)

        lines = [
            "Artifact-only evaluator mode.",
            "Do not inspect repository files and do not use tools.",
            "Judge the sprint only from the harness artifacts below.",
            "Pass only if the existing evidence already satisfies the acceptance checks.",
            "If the evidence is incomplete but recoverable, return revise with concrete required revisions.",
            "Use blocked only for a hard blocker or missing evidence that prevents a meaningful verdict.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            f"Last evaluator verdict: {summary['evaluator_verdict'] or 'unknown'}",
            "",
        ]

        blockers = self._normalize_string_list(summary.get("blockers"))
        next_steps = self._normalize_string_list(summary.get("next_steps"))
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
        revisions = self._normalize_string_list(summary.get("required_revisions"))
        touched_files = self._normalize_string_list(summary.get("touched_files"))

        if checks:
            lines.append("Acceptance checks:")
            lines.extend(f"- {item}" for item in checks[:8])
        if blockers:
            lines.append("Current blockers:")
            lines.extend(f"- {item}" for item in blockers[:8])
        if next_steps:
            lines.append("Current next steps:")
            lines.extend(f"- {item}" for item in next_steps[:8])
        if revisions:
            lines.append("Required revisions from prior evaluator:")
            lines.extend(f"- {item}" for item in revisions[:8])
        if touched_files:
            lines.append("Touched files:")
            lines.extend(f"- {item}" for item in touched_files[:12])
        if validation_checks:
            lines.append("Recorded validation checks:")
            lines.extend(f"- {item}" for item in validation_checks[:12])
        if validation_artifacts:
            lines.append("Validation artifacts:")
            lines.extend(f"- {item}" for item in validation_artifacts[:12])
        if acceptance_evidence:
            lines.append("Explicit acceptance evidence:")
            lines.extend(
                f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
                for check, evidence in list(acceptance_evidence.items())[:8]
            )

        validation_check_lines = [f"- {item}" for item in validation_checks[:12]] or ["(none)"]
        validation_artifact_lines = [f"- {item}" for item in validation_artifacts[:12]] or ["(none)"]
        acceptance_evidence_lines = [
            f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
            for check, evidence in list(acceptance_evidence.items())[:8]
        ] or ["(none)"]

        lines.extend(
            [
                "",
                "Progress summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
                "",
                "Recorded validation checks:",
                *validation_check_lines,
                "",
                "Validation artifacts:",
                *validation_artifact_lines,
                "",
                "Explicit acceptance evidence:",
                *acceptance_evidence_lines,
                "",
                "Handoff artifact excerpt:",
                handoff_text or "(none)",
                "",
                "Keep the prose to at most 4 short lines, then finish with a valid ```resonant-harness JSON block for evaluator_verdict.",
            ]
        )
        return "\n".join(lines)

    def build_harness_structured_evaluator_prompt(self, project_path: Optional[str] = None) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        bundle = self.build_harness_structured_evidence_bundle(target_path)
        summary = bundle["summary"]

        lines = [
            "Structured evaluator mode.",
            "Do not use tools and do not inspect any files beyond the evidence included below.",
            "Judge the sprint only from the compact evidence bundle.",
            "Prefer pass only when the evidence clearly satisfies the acceptance checks.",
            "Return revise when the implementation might be correct but the evidence is incomplete or a check is unsupported.",
            "Return blocked only for a hard blocker or clearly missing implementation evidence.",
            "",
            f"Active sprint: {summary['active_sprint_id'] or 'none'}",
            f"Feature: {summary['contract_feature_name'] or 'unknown'}",
            f"Objective: {summary['contract_objective'] or 'none'}",
            f"Contract status: {summary['contract_status'] or 'unknown'}",
            "",
            "Acceptance checks:",
        ]
        checks = self._normalize_string_list(summary.get("acceptance_checks"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(bundle.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(bundle.get("acceptance_evidence"))
        lines.extend(f"- {item}" for item in checks[:8] or ["(none)"])

        validation_check_lines = [f"- {item}" for item in validation_checks[:12]] or ["(none)"]
        validation_artifact_lines = [f"- {item}" for item in validation_artifacts[:12]] or ["(none)"]
        acceptance_evidence_lines = [
            f"- {check}: {self._truncate_text(evidence, max_chars=220)}"
            for check, evidence in list(acceptance_evidence.items())[:8]
        ] or ["(none)"]

        lines.extend(
            [
                "",
                "Progress summary:",
                summary.get("summary") or "(none)",
                "",
                "Last validation evidence:",
                summary.get("last_validation") or "(none)",
                "",
                "Recorded validation checks:",
                *validation_check_lines,
                "",
                "Validation artifacts:",
                *validation_artifact_lines,
                "",
                "Explicit acceptance evidence:",
                *acceptance_evidence_lines,
                "",
                "Handoff artifact excerpt:",
                bundle["handoff_excerpt"] or "(none)",
                "",
                "Acceptance-check coverage guess:",
            ]
        )
        for item in bundle["acceptance_check_coverage"]:
            marker = "matched" if item["matched"] else "unmatched"
            lines.append(f"- {marker}: {item['check']}")

        lines.append("")
        lines.append("Touched file evidence:")
        for file_item in bundle["files"]:
            lines.append(
                f"- {file_item['path']} ({'exists' if file_item['exists'] else 'missing'})"
            )
            excerpt = str(file_item.get("excerpt") or "").strip()
            if excerpt:
                lines.append(excerpt)
                lines.append("")

        lines.append("Keep the prose to at most 6 short lines, then finish with a valid ```resonant-harness JSON block for evaluator_verdict.")
        return "\n".join(lines)

    def precheck_harness_evaluator_payload(
        self,
        *,
        project_path: Optional[str] = None,
        evaluation_mode: str,
    ) -> dict[str, Any] | None:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        summary = self.get_harness_summary(target_path)
        sprint_id = str(summary.get("active_sprint_id") or "").strip()
        if not sprint_id:
            return None

        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))
        if not acceptance_checks:
            return None

        blockers = self._normalize_string_list(summary.get("blockers"))
        required_revisions = self._normalize_string_list(summary.get("required_revisions"))
        validation_checks = self._normalize_string_list(summary.get("validation_checks"))
        validation_artifacts = self._normalize_string_list(summary.get("validation_artifacts"))
        acceptance_evidence = self._normalize_string_mapping(summary.get("acceptance_evidence"))
        touched_files = self._normalize_string_list(summary.get("touched_files"))
        last_validation = str(summary.get("last_validation") or "").strip()
        handoff_excerpt = self._truncate_text(harness.read_handoff(), max_chars=1200)
        evidence_present = bool(last_validation or validation_checks or validation_artifacts or acceptance_evidence or handoff_excerpt)

        if blockers:
            findings = blockers[:3]
            return {
                "action": "evaluator_verdict",
                "sprint_id": sprint_id,
                "verdict": "blocked",
                "findings": findings,
                "passed_checks": [],
                "failed_checks": findings,
                "required_revisions": findings,
                "score": 0.0,
            }

        if evaluation_mode == "structured":
            bundle = self.build_harness_structured_evidence_bundle(target_path)
            coverage = bundle["acceptance_check_coverage"]
            files = bundle["files"]
            existing_file_count = sum(1 for item in files if item.get("exists"))
        else:
            evidence_text = "\n".join(
                part
                for part in (
                    summary.get("summary") or "",
                    last_validation,
                    "\n".join(validation_checks),
                    "\n".join(validation_artifacts),
                    "\n".join(f"{check}: {evidence}" for check, evidence in acceptance_evidence.items()),
                    handoff_excerpt,
                )
                if part
            )
            coverage = self._build_acceptance_check_coverage(acceptance_checks, evidence_text)
            existing_file_count = 0

        normalized_evidence = {
            self._normalize_acceptance_check_phrase(check): evidence
            for check, evidence in acceptance_evidence.items()
            if self._normalize_acceptance_check_phrase(check) and str(evidence).strip()
        }
        explicit_matches = []
        for check in acceptance_checks:
            normalized_check = self._normalize_acceptance_check_phrase(check)
            if normalized_check and normalized_check in normalized_evidence:
                explicit_matches.append(check)

        matched_checks = []
        for item in coverage:
            check = str(item.get("check") or "")
            if item.get("matched") or check in explicit_matches:
                matched_checks.append(check)
        unmatched_checks = [check for check in acceptance_checks if check not in matched_checks]
        has_complete_coverage = bool(coverage) and not unmatched_checks

        if has_complete_coverage and evidence_present and (
            evaluation_mode != "structured" or existing_file_count > 0
        ):
            findings = (
                validation_checks[:3]
                or validation_artifacts[:3]
                or list(acceptance_evidence.values())[:3]
                or [last_validation or "Acceptance checks are covered by the harness evidence bundle."]
            )
            return {
                "action": "evaluator_verdict",
                "sprint_id": sprint_id,
                "verdict": "pass",
                "findings": findings[:3],
                "passed_checks": matched_checks[:8],
                "failed_checks": [],
                "required_revisions": [],
                "score": 1.0,
            }

        obvious_revisions = required_revisions[:3]
        if not obvious_revisions:
            if evaluation_mode == "structured" and touched_files and existing_file_count == 0:
                obvious_revisions = ["Touched files were recorded, but the compact file evidence is missing."]
            elif unmatched_checks and not validation_checks and not validation_artifacts and not acceptance_evidence and (
                len(unmatched_checks) >= max(2, len(acceptance_checks) // 2)
            ):
                obvious_revisions = unmatched_checks[:3]

        if obvious_revisions:
            findings = obvious_revisions[:3]
            return {
                "action": "evaluator_verdict",
                "sprint_id": sprint_id,
                "verdict": "revise",
                "findings": findings,
                "passed_checks": matched_checks[:8],
                "failed_checks": findings,
                "required_revisions": findings,
                "score": 0.5,
            }

        return None

    def infer_evidence_only_evaluator_payload(
        self,
        *,
        project_path: Optional[str] = None,
        text: str,
    ) -> dict[str, Any] | None:
        stripped = str(text or "").strip()
        if not stripped:
            return None

        lowered = stripped.lower()
        summary = self.get_harness_summary(project_path)
        acceptance_checks = self._normalize_string_list(summary.get("acceptance_checks"))

        normalized_check_phrases = []
        for check in acceptance_checks:
            phrase = self._normalize_acceptance_check_phrase(check)
            if phrase:
                normalized_check_phrases.append(phrase)

        verdict = ""
        if any(token in lowered for token in (" blocked.", " blocked ", "hard blocker", "cannot proceed")):
            verdict = "blocked"
        elif any(
            token in lowered
            for token in (
                "✗",
                "not mentioned in evidence",
                "not covered",
                "not met",
                "missing from evidence",
                "needs revision",
                "need revision",
                "revise",
                "i need to examine",
                "i need to inspect",
                "i need to verify",
                "to properly evaluate",
                "insufficient evidence",
                "missing evidence",
                "cannot verify",
                "not enough evidence",
            )
        ):
            verdict = "revise"
        elif any(
            token in lowered
            for token in (
                "pass.",
                "pass ",
                "passed ",
                "no revisions are needed",
                "no revision is needed",
                "satisfies the acceptance checks",
            )
        ):
            verdict = "pass"
        elif normalized_check_phrases and all(phrase in lowered for phrase in normalized_check_phrases):
            verdict = "pass"

        if not verdict:
            return None

        sprint_id = str(summary.get("active_sprint_id") or "").strip()

        candidate_lines = []
        for raw_line in stripped.splitlines():
            cleaned = raw_line.strip().lstrip("-* ").strip()
            if not cleaned:
                continue
            if cleaned.startswith("```") or cleaned in {"{", "}"}:
                continue
            if cleaned.lower().startswith(("artifact-only evaluator mode", "finish with a short summary")):
                continue
            candidate_lines.append(self._truncate_text(cleaned, max_chars=220))

        findings = []
        for item in candidate_lines:
            if item not in findings:
                findings.append(item)
            if len(findings) >= 3:
                break
        if not findings:
            findings = [self._truncate_text(stripped, max_chars=220)]

        payload: dict[str, Any] = {
            "action": "evaluator_verdict",
            "sprint_id": sprint_id,
            "verdict": verdict,
            "findings": findings,
            "passed_checks": [],
            "failed_checks": [],
            "required_revisions": [],
            "score": None,
        }

        if verdict == "pass":
            payload["passed_checks"] = acceptance_checks[:8]
            payload["score"] = 1.0
        elif verdict == "revise":
            revisions = acceptance_checks[:3] or ["Record more concrete validation evidence in progress.last_validation and handoff.md."]
            payload["required_revisions"] = revisions
            payload["failed_checks"] = revisions
            payload["score"] = 0.5
        else:
            blockers = self._normalize_string_list(summary.get("blockers"))
            payload["required_revisions"] = blockers[:3] or ["Clear the blocker or add enough evaluation evidence to support a verdict."]
            payload["failed_checks"] = payload["required_revisions"]
            payload["score"] = 0.0

        return payload

    def _coerce_evaluator_verdict_payload(
        self,
        *,
        payload: dict[str, Any],
        harness: HarnessWorkspace,
        assistant_text: str = "",
    ) -> dict[str, Any]:
        current_contract = harness.read_sprint_contract()
        sprint_id = str(payload.get("sprint_id") or current_contract.sprint_id).strip()
        verdict = str(
            payload.get("verdict")
            or payload.get("evaluator_verdict")
            or payload.get("evaluation_verdict")
            or payload.get("status")
            or payload.get("result")
            or ""
        ).strip().lower()
        required_revisions = self._normalize_string_list(
            payload.get("required_revisions") or payload.get("required_actions")
        )
        findings = self._normalize_string_list(payload.get("findings"))
        passed_checks = self._normalize_string_list(payload.get("passed_checks"))
        failed_checks = self._normalize_string_list(payload.get("failed_checks"))

        rationale = str(
            payload.get("rationale")
            or payload.get("reason")
            or payload.get("explanation")
            or payload.get("notes")
            or payload.get("summary")
            or ""
        ).strip()
        combined = "\n".join(
            part
            for part in (
                verdict,
                rationale,
                assistant_text,
                "\n".join(required_revisions),
                "\n".join(findings),
            )
            if str(part or "").strip()
        ).lower()

        if verdict not in {"pass", "revise", "blocked"}:
            if any(
                token in combined
                for token in (
                    "blocked",
                    "hard blocker",
                    "cannot proceed",
                    "does not run",
                    "fails immediately",
                    "syntaxerror",
                    "indentationerror",
                    "unexpected indent",
                )
            ):
                verdict = "blocked"
            elif required_revisions or any(
                token in combined
                for token in (
                    "revise",
                    "revising",
                    "needs revision",
                    "required action",
                    "cannot verify",
                    "missing evidence",
                    "insufficient evidence",
                    "not enough evidence",
                    "not pass",
                    "not passable",
                    "does not pass",
                    "not supported from the evidence",
                    "not supported by the evidence",
                )
            ):
                verdict = "revise"
            elif re.search(r"\bpassed\b", combined) or re.search(r"\bpass\b", combined) or any(
                token in combined
                for token in (
                    "all acceptance checks are satisfied",
                    "satisfies the acceptance checks",
                    "passes evaluator checks",
                    "no revisions are needed",
                    "no revision is needed",
                )
            ):
                verdict = "pass"

        if not findings and rationale:
            findings = [self._truncate_text(rationale, max_chars=220)]
        if not findings and assistant_text:
            findings = [
                self._truncate_text(line, max_chars=220)
                for line in self._normalize_string_list(assistant_text)[:3]
            ]
        if verdict == "pass" and not passed_checks:
            passed_checks = self._normalize_string_list(current_contract.acceptance_checks)[:8]
        if verdict in {"revise", "blocked"} and not failed_checks:
            failed_checks = required_revisions[:8]
        if verdict == "blocked" and not required_revisions:
            required_revisions = failed_checks[:8] or [
                "Clear the blocker or add enough evaluation evidence to support a verdict."
            ]
        if verdict == "revise" and not required_revisions:
            required_revisions = failed_checks[:8] or [
                "Add clearer evidence or validation output for the unmet acceptance checks."
            ]

        score = payload.get("score")
        if score is None and verdict in {"pass", "revise", "blocked"}:
            score = {"pass": 1.0, "revise": 0.5, "blocked": 0.0}[verdict]

        return {
            "sprint_id": sprint_id,
            "verdict": verdict,
            "findings": findings[:8],
            "required_revisions": required_revisions[:8],
            "passed_checks": passed_checks[:8],
            "failed_checks": failed_checks[:8],
            "score": score,
        }

    def select_harness_teacher(
        self,
        *,
        session_role: str,
        reason: str = "",
    ) -> tuple[str, str]:
        """Resolve recovery through configured providers, never named models."""
        forced_backend = str(os.environ.get("RESONANT_HARNESS_TEACHER_BACKEND", "") or "").strip()
        forced_model = str(os.environ.get("RESONANT_HARNESS_TEACHER_MODEL", "") or "").strip()
        if forced_backend:
            spec = self._app.build_backend_spec(forced_backend, model=forced_model or None)
            return spec.backend_type, spec.model
        return self.select_harness_backend(session_role=session_role)

    def wrap_user_message_for_harness(
        self,
        *,
        user_msg: str,
        session_mode: str,
        session_role: str,
    ) -> str:
        session_mode = self._app.normalize_session_mode(session_mode)
        session_role = self._app.normalize_session_role(session_mode, session_role)

        payload = self._get_remote_harness_step_payload(
            project_path=self._app.project.project_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=user_msg,
        )
        summary = payload.get("summary_before") if payload else None
        if not isinstance(summary, dict) or not summary:
            summary = self.get_harness_summary(self._app.project.project_path)
        role_requirements = {
            "planner": "Create or refine the spec and propose the next sprint contract. Keep implementation out unless the user explicitly asks for it.",
            "generator": "Implement only the active sprint. Update progress and handoff artifacts before finishing.",
            "evaluator": "Verify against the sprint contract. Write a clear pass, revise, or blocked verdict with concrete required revisions.",
        }[session_role]
        output_contract = (
            str(payload.get("output_contract") or "")
            if payload else
            self.build_harness_output_contract(
                session_mode=session_mode,
                session_role=session_role,
                project_path=self._app.project.project_path,
            )
        )
        return (
            f"HARNESS ROLE: {session_role}\n"
            f"HARNESS ROOT: {summary['root']}\n"
            "READ THESE FILES BEFORE ACTING:\n"
            f"- {summary['spec_path']}\n"
            f"- {summary['progress_path']}\n"
            f"- {summary['sprint_contract_path']}\n"
            f"- {summary['evaluator_report_path']}\n"
            f"- {summary['handoff_path']}\n\n"
            f"ROLE REQUIREMENTS: {role_requirements}\n\n"
            f"FINAL OUTPUT CONTRACT:\n{output_contract}\n\n"
            "USER REQUEST:\n"
            f"{user_msg}"
        )

    @staticmethod
    @staticmethod
    def _normalize_string_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, dict):
            result = []
            for raw_key, raw_value in value.items():
                key = str(raw_key).strip()
                val = str(raw_value).strip()
                text = f"{key}: {val}" if key and val else key or val
                text = text.strip()
                if text:
                    result.append(text)
            return result
        if isinstance(value, (list, tuple, set)):
            result = []
            for item in value:
                text = str(item).strip()
                if text:
                    result.append(text)
            return result
        text = str(value).strip()
        return [text] if text else []

    @classmethod
    @classmethod
    def _normalize_contract_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return cls._normalize_string_list(value)
        if isinstance(value, (list, tuple, set)):
            result: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    title = str(item.get("title") or item.get("id") or "").strip()
                    description = str(item.get("description") or item.get("objective") or "").strip()
                    acceptance = str(item.get("acceptance") or "").strip()
                    parts = [part for part in (title, description) if part]
                    text = ": ".join(parts) if parts else ""
                    if acceptance:
                        text = f"{text} Acceptance: {acceptance}" if text else f"Acceptance: {acceptance}"
                    text = cls._truncate_text(text, max_chars=320)
                    if text:
                        result.append(text)
                    continue
                text = cls._truncate_text(str(item).strip(), max_chars=320)
                if text:
                    result.append(text)
            return result
        text = cls._truncate_text(str(value).strip(), max_chars=320)
        return [text] if text else []

    @staticmethod
    @staticmethod
    def _extract_explicit_harness_objective_text(text: str) -> str:
        raw = str(text or "")
        if not raw.strip():
            return ""
        match = re.search(
            r"TOP-LEVEL OBJECTIVE:\s*(.*?)\s*(?:OBJECTIVE HANDLING RULE:|$)",
            raw,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if match:
            return match.group(1).strip()
        return raw.strip()

    @classmethod
    @classmethod
    def _is_read_only_harness_request(cls, *texts: str) -> bool:
        combined = " ".join(
            cls._extract_explicit_harness_objective_text(text).lower()
            for text in texts
            if cls._extract_explicit_harness_objective_text(text)
        )
        if not combined:
            return False
        tokens = (
            "read-only",
            "read only",
            "read files only",
            "do not modify repository files",
            "do not modify files",
            "no code changes",
            "without making changes",
            "audit only",
            "inspect only",
            "capture findings through harness summary and handoff artifacts only",
            "read-only objective",
        )
        return any(token in combined for token in tokens)

    @classmethod
    @classmethod
    def _sanitize_read_only_contract(
        cls,
        *,
        user_request: str,
        objective: str,
        feature_name: str,
        deliverables: list[str],
        acceptance_checks: list[str],
        evaluator_focus: list[str],
    ) -> tuple[str, list[str], list[str], list[str], bool]:
        if not cls._is_read_only_harness_request(user_request, objective, feature_name):
            return objective, deliverables, acceptance_checks, evaluator_focus, False

        write_tokens = (
            "create ",
            "write ",
            "modify ",
            "update ",
            "edit ",
            "patch ",
            "implement ",
            "test file",
            "test files",
            "unit test",
            "pytest",
            "script/",
            "scripts/",
            "production code",
            "code change",
        )

        def keep_item(text: str) -> bool:
            lowered = text.lower()
            return not any(token in lowered for token in write_tokens)

        sanitized_deliverables = [item for item in deliverables if keep_item(item)][:4]
        sanitized_acceptance = [item for item in acceptance_checks if keep_item(item)][:6]
        sanitized_focus = [item for item in evaluator_focus if keep_item(item)][:5]

        sanitized_objective = objective.strip()
        if sanitized_objective and "read-only" not in sanitized_objective.lower():
            sanitized_objective = f"Read-only audit. {sanitized_objective}"

        if not sanitized_deliverables:
            sanitized_deliverables = [
                "Inspect only the referenced files and capture grounded findings in harness handoff or progress artifacts.",
                "Record concise file evidence and line references for the accepted findings.",
            ]

        if not sanitized_acceptance:
            sanitized_acceptance = [
                "Handoff or progress artifacts reference the audited files by name.",
                "Findings distinguish the requested behaviors with concrete code evidence.",
                "No repository files are modified; the sprint stays read-only.",
            ]
        elif not any("read-only" in item.lower() or "no repository files" in item.lower() for item in sanitized_acceptance):
            sanitized_acceptance.append("No repository files are modified; the sprint stays read-only.")

        if not sanitized_focus:
            sanitized_focus = [
                "Verify the findings are grounded in the audited files with direct evidence.",
                "Reject the sprint if it introduced file modifications or test-writing work.",
            ]

        changed = (
            sanitized_objective != objective
            or sanitized_deliverables != deliverables
            or sanitized_acceptance != acceptance_checks
            or sanitized_focus != evaluator_focus
        )
        return sanitized_objective, sanitized_deliverables, sanitized_acceptance, sanitized_focus, changed

    @staticmethod
    @staticmethod
    def _normalize_string_mapping(value: Any) -> dict[str, str]:
        if value is None:
            return {}
        result: dict[str, str] = {}
        if isinstance(value, dict):
            for raw_key, raw_value in value.items():
                key = str(raw_key).strip()
                if isinstance(raw_value, bool):
                    val = "PASS" if raw_value else ""
                else:
                    val = str(raw_value).strip()
                if key and val:
                    result[key] = val
            return result
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, dict):
                    key = str(item.get("check") or item.get("key") or "").strip()
                    val = str(item.get("evidence") or item.get("value") or "").strip()
                else:
                    key = str(item).strip()
                    val = key
                if key and val:
                    result[key] = val
            return result
        return {}

    @staticmethod
    @staticmethod
    def normalize_harness_contract_status(status: str, *, session_role: str) -> str:
        return HarnessService.normalize_contract_status(status, session_role=session_role)

    @staticmethod
    @staticmethod
    def normalize_harness_action(action: str, *, session_role: str) -> str:
        raw = str(action or "").strip().lower().replace("-", "_").replace(" ", "_")
        if not raw:
            return ""
        aliases = {
            "planner_update": "planner_update",
            "plan_update": "planner_update",
            "plan_complete": "planner_update",
            "planning_complete": "planner_update",
            "sprint_definition_complete": "planner_update",
            "sprint_defined": "planner_update",
            "sprint_contract_complete": "planner_update",
            "generator_update": "generator_update",
            "implementation_update": "generator_update",
            "implementation_complete": "generator_update",
            "code_update": "generator_update",
            "repair_update": "generator_update",
            "repair_complete": "generator_update",
            "repair_result": "generator_update",
            "evaluator_verdict": "evaluator_verdict",
            "evaluation_verdict": "evaluator_verdict",
            "evaluation_complete": "evaluator_verdict",
            "evaluation_result": "evaluator_verdict",
            "verdict": "evaluator_verdict",
        }
        if raw in aliases:
            return aliases[raw]
        if raw in {"complete", "completed", "done"}:
            if session_role == "planner":
                return "planner_update"
            if session_role == "generator":
                return "generator_update"
            if session_role == "evaluator":
                return "evaluator_verdict"
        if session_role == "planner" and any(token in raw for token in ("plan", "planner", "sprint", "contract")):
            return "planner_update"
        if session_role == "generator" and any(token in raw for token in ("generate", "generator", "implement", "code", "patch", "edit")):
            return "generator_update"
        if session_role == "evaluator" and any(token in raw for token in ("evaluate", "evaluator", "verdict", "review", "check")):
            return "evaluator_verdict"
        return raw

    def _normalize_harness_validation_commands(
        self,
        value: Any,
        *,
        project_path: Optional[str] = None,
    ) -> list[str]:
        commands: list[str] = []
        for item in self._normalize_string_list(value):
            cleaned = self._sanitize_harness_validation_command(item, project_path=project_path)
            if cleaned and self._looks_like_shell_command(cleaned) and cleaned not in commands:
                commands.append(cleaned)
        return commands[:6]

    @staticmethod
    @staticmethod
    def _looks_like_shell_command(value: str) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        shell_starts = (
            "python",
            "python3",
            "pytest",
            "node",
            "npm",
            "npx",
            "uv",
            "bash",
            "sh ",
            "./",
            "cat ",
            "grep ",
            "rg ",
            "ls ",
        )
        if text.startswith(shell_starts):
            return True
        return any(token in text for token in (" --", " | ", " && ", " > ", "2>&1", "echo $?"))

    def _extract_planner_contract_payload(
        self,
        *,
        payload: dict[str, Any],
        planner_payload: dict[str, Any],
        current_contract: Any,
    ) -> dict[str, Any]:
        candidate_contracts = [
            payload.get("sprint_contract"),
            payload.get("sprint"),
            payload.get("next_sprint_contract"),
            payload.get("contract"),
            planner_payload.get("sprint_contract"),
            planner_payload.get("sprint"),
            planner_payload.get("next_sprint_contract"),
            planner_payload.get("next_contract"),
            planner_payload.get("contract"),
        ]
        contract_data = next((item for item in candidate_contracts if isinstance(item, dict)), {})
        contract = dict(contract_data) if isinstance(contract_data, dict) else {}

        direct_fields = (
            "sprint_id",
            "feature_name",
            "objective",
            "deliverables",
            "acceptance_checks",
            "acceptance_focus",
            "evaluator_focus",
            "target_files",
            "target_line_hints",
            "validation_commands",
            "edit_strategy",
            "status",
        )
        for source in (planner_payload, payload):
            if not isinstance(source, dict):
                continue
            for key in direct_fields:
                if key not in contract and source.get(key) not in (None, "", [], {}):
                    contract[key] = source.get(key)

        scope = contract.get("scope")
        if not isinstance(scope, dict):
            scope = planner_payload.get("scope") if isinstance(planner_payload.get("scope"), dict) else {}
        if scope:
            if "target_files" not in contract and scope.get("target_files") not in (None, "", [], {}):
                contract["target_files"] = scope.get("target_files")
            if "target_line_hints" not in contract:
                for key in ("target_line_hints", "line_hints", "line_targets"):
                    if scope.get(key) not in (None, "", [], {}):
                        contract["target_line_hints"] = scope.get(key)
                        break
            if "edit_strategy" not in contract:
                for key in ("edit_strategy", "change_type", "approach"):
                    value = str(scope.get(key) or "").strip()
                    if value:
                        contract["edit_strategy"] = value
                        break

        if "acceptance_checks" not in contract and contract.get("acceptance_focus") not in (None, "", [], {}):
            contract["acceptance_checks"] = contract.get("acceptance_focus")
        if "evaluator_focus" not in contract and planner_payload.get("evaluator_checks") not in (None, "", [], {}):
            contract["evaluator_focus"] = planner_payload.get("evaluator_checks")
        if "deliverables" not in contract and planner_payload.get("key_constraints") not in (None, "", [], {}):
            contract["deliverables"] = planner_payload.get("key_constraints")
        if "validation_commands" not in contract:
            for key in ("validation_plan", "validation_steps", "validation_approach"):
                if planner_payload.get(key) not in (None, "", [], {}):
                    contract["validation_commands"] = planner_payload.get(key)
                    break
        if "feature_name" not in contract:
            title = str(planner_payload.get("title") or payload.get("title") or "").strip()
            if title:
                contract["feature_name"] = title
        if "status" not in contract:
            for key in ("contract_status", "status", "phase"):
                value = str(planner_payload.get(key) or payload.get(key) or "").strip()
                if value:
                    contract["status"] = value
                    break
        if bool(planner_payload.get("ready_for_generator")):
            contract["status"] = "approved"
        if bool(planner_payload.get("handoff_ready")) and str(planner_payload.get("next_role") or "").strip() == "generator":
            contract["status"] = "approved"

        if "sprint_id" not in contract and current_contract.sprint_id:
            contract["sprint_id"] = current_contract.sprint_id
        if "feature_name" not in contract and current_contract.feature_name:
            contract["feature_name"] = current_contract.feature_name
        if "objective" not in contract and current_contract.objective:
            contract["objective"] = current_contract.objective
        return contract

    def extract_harness_update(
        self,
        *,
        text: str,
        session_mode: str,
        session_role: str,
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        session_mode = self._app.normalize_session_mode(session_mode)
        session_role = self._app.normalize_session_role(session_mode, session_role)
        if not text:
            return text, None, None

        matches = list(
            re.finditer(r"```resonant-harness\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        )
        if not matches:
            return text, None, None

        match = matches[-1]
        payload_text = match.group(1).strip()
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError as exc:
            return text, None, f"Invalid resonant-harness JSON for {session_role}: {exc.msg}"

        cleaned = f"{text[:match.start()]}{text[match.end():]}".strip()
        return cleaned, payload, None

    @staticmethod
    def apply_harness_update(
        self,
        *,
        session_mode: str,
        session_role: str,
        payload: dict[str, Any],
        project_path: Optional[str] = None,
        assistant_text: str = "",
        user_request: str = "",
    ) -> str:
        session_mode = self._app.normalize_session_mode(session_mode)
        session_role = self._app.normalize_session_role(session_mode, session_role)

        harness = HarnessWorkspace(project_path or self._app.project.project_path)
        harness.ensure_layout()
        action = self.normalize_harness_action(
            payload.get("action") or payload.get("event") or "",
            session_role=session_role,
        )

        if not action:
            # A fenced `resonant-harness` block is already role-scoped by the
            # active session. Default to that role's action so minor teacher or
            # planner omissions do not discard otherwise usable harness state.
            action = {
                "planner": "planner_update",
                "generator": "generator_update",
                "evaluator": "evaluator_verdict",
            }[session_role]

        if action == "planner_update":
            planner_payload = payload.get("planner_update") if isinstance(payload.get("planner_update"), dict) else {}
            if not isinstance(planner_payload, dict):
                planner_payload = {}

            spec_data = (
                payload.get("spec")
                or planner_payload.get("spec")
                or planner_payload.get("spec_updates")
                or {}
            )
            if isinstance(spec_data, dict):
                spec_updates: dict[str, Any] = {}
                for key in ("title", "summary"):
                    value = str(spec_data.get(key) or "").strip()
                    if value:
                        spec_updates[key] = value
                for key in ("user_stories", "sprint_order", "design_principles", "technical_notes"):
                    if key in spec_data:
                        spec_updates[key] = self._normalize_string_list(spec_data.get(key))
                if spec_updates:
                    harness.update_spec(**spec_updates)

            current_contract = harness.read_sprint_contract()
            contract_data = self._extract_planner_contract_payload(
                payload=payload,
                planner_payload=planner_payload,
                current_contract=current_contract,
            )
            if isinstance(contract_data, dict):
                sprint_id = str(contract_data.get("sprint_id") or current_contract.sprint_id).strip()
                objective = str(contract_data.get("objective") or current_contract.objective).strip()
                feature_name = str(contract_data.get("feature_name") or current_contract.feature_name).strip()
                deliverables = self._normalize_contract_list(
                    contract_data.get("deliverables", current_contract.deliverables)
                )
                acceptance_checks = self._normalize_contract_list(
                    contract_data.get(
                        "acceptance_checks",
                        contract_data.get("acceptance_focus", current_contract.acceptance_checks),
                    )
                )
                evaluator_focus = self._normalize_contract_list(
                    contract_data.get("evaluator_focus", current_contract.evaluator_focus)
                )
                target_files = self._normalize_string_list(
                    contract_data.get("target_files", current_contract.target_files)
                )
                target_line_hints = self._normalize_string_list(
                    contract_data.get("target_line_hints", current_contract.target_line_hints)
                )
                validation_commands = self._normalize_harness_validation_commands(
                    contract_data.get("validation_commands", current_contract.validation_commands),
                    project_path=project_path,
                )
                edit_strategy = str(contract_data.get("edit_strategy") or current_contract.edit_strategy).strip()
                objective, deliverables, acceptance_checks, evaluator_focus, contract_sanitized = (
                    self._sanitize_read_only_contract(
                        user_request=user_request,
                        objective=objective,
                        feature_name=feature_name,
                        deliverables=deliverables,
                        acceptance_checks=acceptance_checks,
                        evaluator_focus=evaluator_focus,
                    )
                )
                if sprint_id and objective:
                    harness.set_active_sprint(
                        sprint_id=sprint_id,
                        feature_name=feature_name,
                        objective=objective,
                        deliverables=deliverables,
                        acceptance_checks=acceptance_checks,
                        evaluator_focus=evaluator_focus,
                        target_files=target_files,
                        target_line_hints=target_line_hints,
                        validation_commands=validation_commands,
                        edit_strategy=edit_strategy,
                        status=self.normalize_harness_contract_status(
                            str(contract_data.get("status") or current_contract.status or "proposed").strip(),
                            session_role="planner",
                        ) or "proposed",
                        role="planner",
                    )
                    if contract_sanitized:
                        harness.append_run_event(
                            "planner_contract_sanitized",
                            {
                                "sprint_id": sprint_id,
                                "feature_name": feature_name,
                                "objective": objective,
                                "deliverables": deliverables,
                                "acceptance_checks": acceptance_checks,
                                "evaluator_focus": evaluator_focus,
                                "target_files": target_files,
                                "target_line_hints": target_line_hints,
                                "validation_commands": validation_commands,
                                "edit_strategy": edit_strategy,
                            },
                        )
                elif contract_data:
                    contract_updates: dict[str, Any] = {}
                    for key in ("sprint_id", "feature_name", "objective", "status"):
                        value = str(contract_data.get(key) or "").strip()
                        if value:
                            if key == "status":
                                value = self.normalize_harness_contract_status(value, session_role="planner")
                            contract_updates[key] = value
                    if "deliverables" in contract_data:
                        contract_updates["deliverables"] = deliverables
                    if "acceptance_checks" in contract_data:
                        contract_updates["acceptance_checks"] = acceptance_checks
                    if "evaluator_focus" in contract_data:
                        contract_updates["evaluator_focus"] = evaluator_focus
                    if "target_files" in contract_data:
                        contract_updates["target_files"] = target_files
                    if "target_line_hints" in contract_data:
                        contract_updates["target_line_hints"] = target_line_hints
                    if "validation_commands" in contract_data:
                        contract_updates["validation_commands"] = validation_commands
                    if "edit_strategy" in contract_data:
                        contract_updates["edit_strategy"] = edit_strategy
                    if contract_updates:
                        harness.update_sprint_contract(**contract_updates)

            progress_data = (
                payload.get("progress")
                or payload.get("progress_state")
                or planner_payload.get("progress")
                or planner_payload.get("progress_state")
                or {}
            )
            if not isinstance(progress_data, dict):
                progress_data = {}
            if not progress_data:
                for key in ("summary", "revision_reason"):
                    value = str(planner_payload.get(key) or "").strip()
                    if value:
                        progress_data["summary"] = value
                        break
                for key in ("next_steps", "blockers", "touched_files", "validation_checks"):
                    if planner_payload.get(key) not in (None, "", [], {}):
                        progress_data[key] = planner_payload.get(key)
                phase_value = str(planner_payload.get("phase") or "").strip()
                if phase_value:
                    progress_data["current_phase"] = (
                        "implementation" if "generator" in phase_value or "ready" in phase_value else "planning"
                    )
            if isinstance(progress_data, dict):
                progress_updates: dict[str, Any] = {"active_role": "planner"}
                for key in ("product_goal", "summary", "last_validation"):
                    value = str(progress_data.get(key) or "").strip()
                    if value:
                        progress_updates[key] = value
                for key in ("blockers", "next_steps", "touched_files", "validation_checks"):
                    if key in progress_data:
                        progress_updates[key] = self._normalize_string_list(progress_data.get(key))
                current_phase = str(progress_data.get("current_phase") or "planning").strip()
                if current_phase:
                    progress_updates["current_phase"] = current_phase
                if progress_updates:
                    harness.update_progress(**progress_updates)

            handoff_markdown = str(payload.get("handoff_markdown") or planner_payload.get("handoff_markdown") or "").strip()
            if handoff_markdown:
                harness.write_handoff(handoff_markdown)

            sprint_id = harness.read_sprint_contract().sprint_id
            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": sprint_id,
                    "harness_payload": payload,
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return f"Applied planner harness update{f' for {sprint_id}' if sprint_id else ''}"

        if action == "generator_update":
            generator_payload = payload.get("generator_update") if isinstance(payload.get("generator_update"), dict) else {}
            if not isinstance(generator_payload, dict):
                generator_payload = {}
            progress_data = (
                payload.get("progress")
                or generator_payload.get("progress")
                or {}
            )
            if not isinstance(progress_data, dict):
                progress_data = {}
            if not progress_data:
                for source in (generator_payload, payload):
                    if not isinstance(source, dict):
                        continue
                    for key, target_key in (
                        ("summary", "summary"),
                        ("repair_summary", "summary"),
                        ("validation_summary", "last_validation"),
                        ("last_validation", "last_validation"),
                        ("product_goal", "product_goal"),
                        ("handoff_summary", "summary"),
                    ):
                        value = str(source.get(key) or "").strip()
                        if value and target_key not in progress_data:
                            progress_data[target_key] = value
                    for key in (
                        "blockers",
                        "next_steps",
                        "touched_files",
                        "validation_checks",
                        "validation_artifacts",
                    ):
                        if source.get(key) not in (None, "", [], {}):
                            progress_data[key] = source.get(key)
                    if source.get("acceptance_evidence") not in (None, "", [], {}):
                        progress_data["acceptance_evidence"] = source.get("acceptance_evidence")
                    if source.get("validation_command") not in (None, ""):
                        progress_data.setdefault(
                            "last_validation",
                            f"Ran validation command: {str(source.get('validation_command') or '').strip()}",
                        )
                        progress_data.setdefault("validation_artifacts", [])
                        progress_data["validation_artifacts"] = list(progress_data["validation_artifacts"]) + [
                            f"Validation command: {str(source.get('validation_command') or '').strip()}"
                        ]
                    if source.get("validation_output") not in (None, ""):
                        progress_data.setdefault("validation_artifacts", [])
                        exit_code = source.get("exit_code")
                        exit_suffix = ""
                        if isinstance(exit_code, (int, float)) and not isinstance(exit_code, bool):
                            exit_suffix = f" exit={int(exit_code)}"
                        progress_data["validation_artifacts"] = list(progress_data["validation_artifacts"]) + [
                            f"Validation output{exit_suffix}: {str(source.get('validation_output') or '').strip()}"
                        ]
                    if source.get("current_phase") not in (None, ""):
                        progress_data["current_phase"] = source.get("current_phase")
            if progress_data.get("last_validation") and "validation_artifacts" not in progress_data:
                progress_data["validation_artifacts"] = [progress_data["last_validation"]]
            if progress_data.get("touched_files") and "validation_checks" not in progress_data:
                progress_data["validation_checks"] = [
                    "Updated file scope: " + ", ".join(self._normalize_string_list(progress_data.get("touched_files"))[:3])
                ]
            progress_updates: dict[str, Any] = {"active_role": "generator"}
            current_contract = harness.read_sprint_contract()
            for key in ("summary", "product_goal", "last_validation"):
                value = str(progress_data.get(key) or "").strip()
                if value:
                    progress_updates[key] = value
            for key in ("blockers", "next_steps", "touched_files", "validation_checks", "validation_artifacts"):
                if key in progress_data:
                    progress_updates[key] = self._normalize_string_list(progress_data.get(key))
            if "acceptance_evidence" in progress_data:
                normalized_evidence = self._normalize_string_mapping(progress_data.get("acceptance_evidence"))
                progress_updates["acceptance_evidence"] = self.align_acceptance_evidence_to_contract(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    evidence=normalized_evidence,
                    objective=str(current_contract.objective or ""),
                    user_request=user_request,
                )
            current_phase = str(progress_data.get("current_phase") or "implementation").strip()
            if current_phase:
                progress_updates["current_phase"] = current_phase
            if progress_updates.get("touched_files") and "blockers" not in progress_updates:
                progress_updates["blockers"] = []
            if progress_updates.get("last_validation") and "next_steps" not in progress_updates:
                progress_updates["next_steps"] = []
            sprint_status = str(
                payload.get("sprint_status")
                or generator_payload.get("sprint_status")
                or payload.get("status")
                or generator_payload.get("status")
                or ""
            ).strip()
            sprint_status = self.normalize_harness_contract_status(sprint_status, session_role="generator")
            if not sprint_status:
                inferred_evidence = self._normalize_string_mapping(progress_data.get("acceptance_evidence"))
                touched_files = self._normalize_string_list(progress_data.get("touched_files"))
                blockers = self._normalize_string_list(progress_data.get("blockers"))
                last_validation = str(progress_data.get("last_validation") or "").strip()
                if touched_files and not blockers and (last_validation or inferred_evidence):
                    sprint_status = "implemented"
            successful_generator_update = sprint_status in {"approved", "implemented", "passed"}
            if successful_generator_update:
                target_files = self._normalize_string_list(current_contract.target_files)
                merged_evidence = self._normalize_string_mapping(progress_updates.get("acceptance_evidence"))
                for check, evidence in self._infer_static_acceptance_evidence(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    summary={
                        "target_files": target_files,
                        "touched_files": self._normalize_string_list(progress_updates.get("touched_files")),
                    },
                ).items():
                    merged_evidence.setdefault(check, evidence)

                if not self._acceptance_evidence_covers_contract(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    evidence=merged_evidence,
                ):
                    probe_checks, probe_artifacts, probe_evidence = self.run_harness_generator_validation_probes(
                        project_path=project_path or self._app.project.project_path,
                        summary={
                            "acceptance_checks": self._normalize_string_list(current_contract.acceptance_checks),
                            "validation_commands": self._normalize_string_list(current_contract.validation_commands),
                            "required_revisions": self._normalize_string_list(
                                harness.read_evaluator_report().required_revisions
                            ),
                            "next_steps": self._normalize_string_list(progress_updates.get("next_steps")),
                            "summary": str(progress_updates.get("summary") or ""),
                            "last_validation": str(progress_updates.get("last_validation") or ""),
                            "validation_checks": self._normalize_string_list(progress_updates.get("validation_checks")),
                            "validation_artifacts": self._normalize_string_list(progress_updates.get("validation_artifacts")),
                            "acceptance_evidence": merged_evidence,
                            "target_files": target_files,
                            "touched_files": self._normalize_string_list(progress_updates.get("touched_files")),
                            "deliverables": self._normalize_string_list(current_contract.deliverables),
                            "contract_objective": str(current_contract.objective or ""),
                        },
                    )
                    if probe_checks:
                        merged_checks = self._normalize_string_list(progress_updates.get("validation_checks"))
                        for item in probe_checks:
                            if item not in merged_checks:
                                merged_checks.append(item)
                        progress_updates["validation_checks"] = merged_checks[:8]
                    if probe_artifacts:
                        merged_artifacts = self._normalize_string_list(progress_updates.get("validation_artifacts"))
                        for item in probe_artifacts:
                            if item not in merged_artifacts:
                                merged_artifacts.append(item)
                        progress_updates["validation_artifacts"] = merged_artifacts[:8]
                    for check, evidence in probe_evidence.items():
                        merged_evidence.setdefault(check, evidence)

                progress_updates["acceptance_evidence"] = self.align_acceptance_evidence_to_contract(
                    acceptance_checks=self._normalize_string_list(current_contract.acceptance_checks),
                    evidence=merged_evidence,
                    objective=str(current_contract.objective or ""),
                    user_request=user_request,
                )
                repair_summary = str(
                    generator_payload.get("repair_summary")
                    or payload.get("repair_summary")
                    or progress_updates.get("summary")
                    or ""
                ).strip()
                if repair_summary and not progress_updates.get("last_validation"):
                    progress_updates["last_validation"] = repair_summary
                if progress_updates.get("last_validation") and "validation_artifacts" not in progress_updates:
                    touched_files = self._normalize_string_list(progress_updates.get("touched_files"))
                    success_artifacts = [str(progress_updates.get("last_validation") or "").strip()]
                    if touched_files:
                        success_artifacts.append(
                            "Touched files: " + ", ".join(touched_files[:3])
                        )
                    progress_updates["validation_artifacts"] = [item for item in success_artifacts if item][:6]
                progress_updates["blockers"] = []
                progress_updates["next_steps"] = []
                progress_updates["current_phase"] = "evaluation"
            harness.update_progress(**progress_updates)

            handoff_markdown = str(
                payload.get("handoff_markdown")
                or generator_payload.get("handoff_markdown")
                or ""
            ).strip()
            if not handoff_markdown and successful_generator_update:
                touched_files = self._normalize_string_list(progress_updates.get("touched_files"))
                validation_summary = str(progress_updates.get("last_validation") or progress_updates.get("summary") or "").strip()
                handoff_lines = [
                    "# Generator Handoff",
                    "",
                    "## Summary",
                    str(progress_updates.get("summary") or "Implemented the requested sprint update."),
                ]
                if validation_summary:
                    handoff_lines.extend(["", "## Validation", f"- {validation_summary}"])
                if touched_files:
                    handoff_lines.extend(["", "## Touched Files", *[f"- `{path}`" for path in touched_files[:6]]])
                handoff_markdown = "\n".join(handoff_lines)
            if handoff_markdown:
                harness.write_handoff(handoff_markdown)

            if sprint_status in {"proposed", "approved", "implemented", "needs_revision", "passed", "failed"}:
                harness.set_contract_status(status=sprint_status, role="generator")
            if sprint_status in {"approved", "implemented", "passed"}:
                harness.update_progress(blockers=[], next_steps=[])
            if current_contract.sprint_id and sprint_status != "failed":
                harness.write_evaluator_report(
                    EvaluatorReport(
                        sprint_id=current_contract.sprint_id,
                        verdict="unknown",
                        score=None,
                        findings=[],
                        required_revisions=[],
                        passed_checks=[],
                        failed_checks=[],
                    )
                )

            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": harness.read_sprint_contract().sprint_id,
                    "sprint_status": sprint_status or "",
                    "harness_payload": payload,
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return "Applied generator harness update"

        if action == "evaluator_verdict":
            evaluator_payload = self._coerce_evaluator_verdict_payload(
                payload=payload,
                harness=harness,
                assistant_text=assistant_text,
            )
            sprint_id = str(evaluator_payload.get("sprint_id") or "").strip()
            verdict = str(evaluator_payload.get("verdict") or "").strip()
            if not sprint_id or verdict not in {"pass", "revise", "blocked"}:
                raise ValueError("Evaluator verdict requires sprint_id and verdict")
            harness.record_evaluator_verdict(
                sprint_id=sprint_id,
                verdict=verdict,
                findings=self._normalize_string_list(evaluator_payload.get("findings")),
                required_revisions=self._normalize_string_list(evaluator_payload.get("required_revisions")),
                passed_checks=self._normalize_string_list(evaluator_payload.get("passed_checks")),
                failed_checks=self._normalize_string_list(evaluator_payload.get("failed_checks")),
                score=evaluator_payload.get("score"),
            )
            harness.append_run_event(
                "assistant_harness_update",
                {
                    "action": action,
                    "session_role": session_role,
                    "sprint_id": sprint_id,
                    "verdict": verdict,
                    "harness_payload": payload,
                    "user_request": user_request,
                    "assistant_text": assistant_text,
                },
            )
            return f"Applied evaluator verdict {verdict} for {sprint_id}"

        raise ValueError(f"Unknown harness action: {action}")

    def build_harness_resume_prompt(
        self,
        *,
        session_mode: str,
        session_role: str,
        project_path: Optional[str] = None,
        backend=None,
        objective: str = "",
    ) -> str:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        payload = self._get_remote_harness_step_payload(
            project_path=target_path,
            session_mode=session_mode,
            session_role=session_role,
            objective=objective,
            backend=backend,
        )
        if payload and payload.get("resume_prompt"):
            return str(payload["resume_prompt"])
        return self._app.harness_service.build_resume_prompt(
            project_path=target_path,
            session_mode=session_mode,
            session_role=session_role,
        )

    def _build_harness_cycle_prompt(
        self,
        *,
        session_role: str,
        project_path: str | None = None,
        objective: str = "",
    ) -> str:
        prompt = self.build_harness_resume_prompt(
            session_mode="code",
            session_role=session_role,
            project_path=project_path,
        )
        objective = objective.strip()
        if objective:
            role_guidance = {
                "planner": (
                    "Convert the objective into harness artifacts, not a standalone answer. "
                    "Your job is to define the sprint and leave execution to generator/evaluator; "
                    "do not perform the audit, code change, or validation work yourself unless the "
                    "objective explicitly says the planner must do it. "
                    "If the objective is explicitly read-only, keep the sprint deliverables read-only "
                    "and artifact-focused instead of inventing code changes. "
                    "For one-file coding tasks, include a compact patch scaffold in sprint_contract: "
                    "target_files, target_line_hints, validation_commands, and edit_strategy. "
                    "If the objective asks for bullets, findings, or an audit summary, place that "
                    "content in handoff_markdown and concise progress/spec fields, then finish with "
                    "a valid planner_update resonant-harness block. Put the concrete handoff under "
                    "`sprint_contract` itself; do not replace it with alternate wrapper keys."
                ),
                "generator": (
                    "Treat the objective as implementation guidance for the active sprint. "
                    "If the objective is explicitly read-only, do not modify repository files; only "
                    "read, analyze, and update harness artifacts. "
                    "Keep the final response brief, record validation in progress.last_validation, "
                    "store short artifacts in progress.validation_artifacts, map satisfied acceptance "
                    "checks into progress.acceptance_evidence, "
                    "and finish with a valid generator_update resonant-harness block."
                ),
                "evaluator": (
                    "Treat the objective as evaluation scope. Put human-readable findings in the "
                    "normal response and required_revisions/failed_checks, then finish with a valid "
                    "evaluator_verdict resonant-harness block."
                ),
            }[session_role]
            prompt = (
                f"TOP-LEVEL OBJECTIVE:\n{objective}\n\n"
                f"OBJECTIVE HANDLING RULE:\n{role_guidance}\n\n"
                f"{prompt}"
            )
        return prompt

    def select_harness_backend(
        self,
        *,
        session_role: str,
        project_path: Optional[str] = None,
    ) -> tuple[str, str]:
        """Select a harness backend from explicit configuration or active state."""
        if not self._app.available_backends:
            self._app.detect_backends()
        project_path = os.path.normpath(project_path or self._app.project.project_path)
        role_env = session_role.upper()
        forced_backend = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_BACKEND", "") or "").strip()
        forced_model = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_MODEL", "") or "").strip()
        if forced_backend or forced_model:
            retry_backend = forced_backend
            if not retry_backend and self._app.backend_spec:
                retry_backend = self._app.backend_spec.backend_type
            if not retry_backend:
                return "", ""
            spec = self._app.build_backend_spec(
                retry_backend,
                model=forced_model or None,
                project_path=project_path,
            )
            return spec.backend_type, spec.model
        if self._app.backend_spec and self._app.available_backends.get(self._app.backend_spec.backend_type):
            model = forced_model or self._app.backend_spec.model
            if model:
                return self._app.backend_spec.backend_type, model

        configured_backend = str(
            self._app.settings.get("general", "default_backend", "") or ""
        ).strip()
        candidates = [configured_backend, "ollama", "exo", "kimi", "codex"]
        for backend_type in dict.fromkeys(item for item in candidates if item):
            models = list(self._app.available_backends.get(backend_type, {}).get("models") or [])
            if not models:
                continue
            model = forced_model or models[0]
            spec = self._app.build_backend_spec(backend_type, model=model, project_path=project_path)
            return spec.backend_type, spec.model
        raise ValueError(f"No model available for harness role '{session_role}'")

    def _harness_generator_needs_frontier_repair(self, project_path: Optional[str] = None) -> bool:
        project_path = os.path.normpath(project_path or self._app.project.project_path)
        summary = self.get_harness_summary(project_path)
        contract_status = str(summary.get("contract_status") or "").strip()
        evaluator_verdict = str(summary.get("evaluator_verdict") or "").strip()
        if contract_status not in {"implemented", "needs_revision", "failed"}:
            return False
        combined = "\n".join(
            [
                "\n".join(self._normalize_string_list(summary.get("findings"))),
                "\n".join(self._normalize_string_list(summary.get("required_revisions"))),
                "\n".join(self._normalize_string_list(summary.get("validation_artifacts"))),
                str(summary.get("last_validation") or ""),
            ]
        ).lower()
        if any(
            token in combined
            for token in (
                "syntaxerror",
                "syntax error",
                "indentationerror",
                "indentation error",
                "expected an indented block",
                "unexpected indent",
                "invalid syntax",
                "parse error",
                "traceback (most recent call last)",
                "modulenotfounderror",
                "module not found",
                "importerror",
                "import error",
                "nameerror",
                "typeerror",
                "attributeerror",
                "runtimeerror",
                "runtime error",
            )
        ):
            return True
        if evaluator_verdict not in {"revise", "blocked"}:
            return False

        bundle = self.build_harness_generator_structured_bundle(project_path)
        files = bundle.get("files") or []
        if len(files) != 1 or not bool(files[0].get("exists")):
            return False
        traceback_data = self._extract_harness_repair_traceback(project_path, files[0], summary)
        return bool(traceback_data.get("line_number") or traceback_data.get("error_line"))

    def select_harness_retry_backend(
        self,
        *,
        session_role: str,
        failed_backend: str = "",
        project_path: Optional[str] = None,
    ) -> tuple[str, str]:
        """Return only an explicitly configured retry target."""
        if not self._app.available_backends:
            self._app.detect_backends()
        project_path = os.path.normpath(project_path or self._app.project.project_path)
        role_env = session_role.upper()
        forced_backend = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_BACKEND", "") or "").strip()
        forced_model = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_MODEL", "") or "").strip()
        if forced_backend.lower() in {"disabled", "none", "off", "false", "no"}:
            return "", ""

        if forced_backend:
            spec = self._app.build_backend_spec(
                forced_backend,
                model=forced_model or None,
                project_path=project_path,
            )
            return spec.backend_type, spec.model
        return "", ""

    def get_harness_role_timeout_seconds(self, session_role: str) -> float | None:
        role_env = session_role.upper()
        raw = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_TIMEOUT_SECONDS", "") or "").strip()
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError:
            return None
        return value if value > 0 else None

    def get_harness_role_retry_timeout_seconds(self, session_role: str) -> float | None:
        role_env = session_role.upper()
        raw = str(os.environ.get(f"RESONANT_HARNESS_{role_env}_RETRY_TIMEOUT_SECONDS", "") or "").strip()
        if not raw:
            return self.get_harness_role_timeout_seconds(session_role)
        try:
            value = float(raw)
        except ValueError:
            return self.get_harness_role_timeout_seconds(session_role)
        return value if value > 0 else self.get_harness_role_timeout_seconds(session_role)

    def get_harness_role_max_tokens(self, session_role: str) -> int | None:
        return self._app.HARNESS_ROLE_MAX_TOKENS.get(session_role, self._app.SESSION_MAX_TOKENS)

    def build_harness_role_session(
        self,
        *,
        project_path: Optional[str] = None,
        session_role: str,
        backend_type: Optional[str] = None,
        model: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        allowed_tools: Optional[list[dict[str, Any]]] = None,
        max_tokens_override: Optional[int] = None,
    ) -> tuple[Session, BackendSpec]:
        project_path = os.path.normpath(project_path or self._app.project.project_path)
        normalized_role = self._app.normalize_session_role("code", session_role)
        if not backend_type:
            backend_type, selected_model = self.select_harness_backend(
                session_role=normalized_role,
                project_path=project_path,
            )
            model = model or selected_model
        spec = self._app.build_backend_spec(backend_type, model=model or None, project_path=project_path)
        max_tokens = (
            max_tokens_override
            if max_tokens_override is not None
            else self.get_harness_role_max_tokens(normalized_role)
        )
        backend = spec.create_backend(self._app.settings)
        session = self._app.build_session(
            backend=backend,
            backend_spec=spec,
            project_path=project_path,
            cancel_event=cancel_event,
            session_mode="code",
            session_role=normalized_role,
            max_tokens=max_tokens,
            allowed_tools=allowed_tools,
        )
        return session, spec

    def run_harness_role_once(
        self,
        *,
        project_path: str,
        session_role: str,
        prompt: str,
        backend_type: Optional[str] = None,
        model: Optional[str] = None,
        cancel_event: Optional[threading.Event] = None,
        timeout_seconds: Optional[float] = None,
    ) -> dict[str, Any]:
        role_cancel_event = threading.Event()
        timeout_stop = threading.Event()
        timed_out = False

        if cancel_event is not None:
            def _watch_external_cancel() -> None:
                cancel_event.wait()
                role_cancel_event.set()

            threading.Thread(target=_watch_external_cancel, daemon=True).start()

        if timeout_seconds and timeout_seconds > 0:
            def _watch_timeout() -> None:
                nonlocal timed_out
                if not timeout_stop.wait(timeout_seconds):
                    timed_out = True
                    role_cancel_event.set()

            threading.Thread(target=_watch_timeout, daemon=True).start()

        normalized_role = self._app.normalize_session_role("code", session_role)
        evaluation_mode = "full"
        generator_mode = "full"
        if normalized_role == "evaluator":
            evaluation_mode = self.get_harness_evaluator_strategy(project_path)
        elif normalized_role == "generator":
            generator_mode = self.get_harness_generator_strategy(project_path, prompt)

        if normalized_role == "evaluator" and evaluation_mode in {"artifacts", "structured"}:
            prechecked_payload = self.precheck_harness_evaluator_payload(
                project_path=project_path,
                evaluation_mode=evaluation_mode,
            )
            if prechecked_payload is not None:
                self.apply_harness_update(
                    session_mode="code",
                    session_role=session_role,
                    payload=prechecked_payload,
                    project_path=project_path,
                    assistant_text="",
                    user_request=prompt,
                )
                return {
                    "result": "",
                    "error": "",
                    "steps": 0,
                    "display_events": [],
                    "backend_type": "precheck",
                    "model": "deterministic",
                    "timed_out": False,
                    "artifact_only": evaluation_mode == "artifacts",
                    "evaluation_mode": evaluation_mode,
                    "prechecked": True,
                }

        if normalized_role == "generator" and generator_mode == "artifacts":
            effective_prompt = self.build_harness_generator_artifact_prompt(project_path, prompt)
            allowed_tools = []
            max_tokens_override = self.get_harness_generator_artifact_max_tokens()
        elif normalized_role == "generator" and generator_mode == "repair":
            effective_prompt = self.build_harness_generator_repair_prompt(project_path, prompt)
            allowed_tools = self._filter_tool_definitions(["file_edit", "bash"])
            max_tokens_override = self.get_harness_generator_repair_max_tokens()
        elif normalized_role == "generator" and generator_mode == "patch":
            effective_prompt = self.build_harness_generator_patch_prompt(project_path, prompt)
            allowed_tools = self._filter_tool_definitions(["file_edit", "bash"])
            max_tokens_override = self.get_harness_generator_patch_max_tokens()
        elif normalized_role == "generator" and generator_mode == "structured":
            effective_prompt = self.build_harness_generator_structured_prompt(project_path, prompt)
            allowed_tools = self._filter_tool_definitions(["file_read", "file_edit", "bash"])
            max_tokens_override = self.get_harness_generator_structured_max_tokens()
        elif evaluation_mode == "artifacts":
            effective_prompt = self.build_harness_evaluator_artifact_prompt(project_path)
            allowed_tools = []
            max_tokens_override = self.get_harness_evaluator_artifact_max_tokens()
        elif evaluation_mode == "structured":
            effective_prompt = self.build_harness_structured_evaluator_prompt(project_path)
            allowed_tools = []
            max_tokens_override = self.get_harness_evaluator_structured_max_tokens()
        else:
            effective_prompt = prompt
            allowed_tools = None
            max_tokens_override = None

        # v0.4.0 — pre-cut, this branch routed Resonant Engine harness
        # cycles through `backend.prepare_harness_step` for remote
        # execution. With ResonantBackend gone, every harness role runs
        # the local session loop below.
        session, spec = self.build_harness_role_session(
            project_path=project_path,
            session_role=session_role,
            backend_type=backend_type,
            model=model,
            cancel_event=role_cancel_event,
            allowed_tools=allowed_tools,
            max_tokens_override=max_tokens_override,
        )
        collected_text: list[str] = []
        display_events: list[dict[str, Any]] = []
        steps = 0
        error = ""
        deferred_parse_error = ""
        post_apply_error = ""
        pending_harness_payload: dict[str, Any] | None = None
        pending_harness_text = ""

        try:
            for event in session.run(effective_prompt):
                display_events.append(event)
                event_type = event.get("event", "")
                if event_type == EngineEvent.TEXT_DONE.value:
                    text = str(event.get("text") or "").strip()
                    cleaned_text, harness_payload, parse_error = self.extract_harness_update(
                        text=text,
                        session_mode="code",
                        session_role=session_role,
                    )
                    if parse_error:
                        if evaluation_mode in {"artifacts", "structured"} or generator_mode in {"artifacts", "patch", "structured"}:
                            deferred_parse_error = parse_error
                        elif not error:
                            error = parse_error
                    if harness_payload is not None:
                        pending_harness_payload = harness_payload
                        pending_harness_text = cleaned_text
                    if cleaned_text:
                        collected_text.append(cleaned_text)
                elif event_type == EngineEvent.STEP_END.value:
                    steps += 1
                elif event_type == EngineEvent.ERROR.value:
                    message = str(event.get("message") or "Unknown error")
                    if role_cancel_event.is_set() and message == "Interrupted":
                        error = ""
                    elif message != "Interrupted":
                        error = message

                if role_cancel_event.is_set():
                    session.cancel()
        finally:
            timeout_stop.set()

        if not error and pending_harness_payload is None and normalized_role == "generator" and generator_mode == "artifacts":
            inferred_payload = self.infer_generator_artifact_payload(
                project_path=project_path,
                text="\n\n".join(collected_text).strip(),
                prompt=effective_prompt,
            )
            if inferred_payload is not None:
                pending_harness_payload = inferred_payload
        if not error and pending_harness_payload is None and normalized_role == "generator" and generator_mode in {"repair", "patch", "structured"}:
            inferred_payload = self.infer_generator_structured_payload(
                project_path=project_path,
                text="\n\n".join(collected_text).strip(),
                prompt=effective_prompt,
                display_events=display_events,
            )
            if inferred_payload is not None:
                pending_harness_payload = inferred_payload

        if not error and pending_harness_payload is None and evaluation_mode in {"artifacts", "structured"}:
            inferred_payload = self.infer_evidence_only_evaluator_payload(
                project_path=project_path,
                text="\n\n".join(collected_text).strip(),
            )
            if inferred_payload is not None:
                pending_harness_payload = inferred_payload

        if not error and pending_harness_payload is not None and normalized_role == "generator":
            pending_harness_payload, post_apply_error = self.apply_generator_post_patch_safety_gate(
                project_path=project_path,
                payload=pending_harness_payload,
                generator_mode=generator_mode,
                display_events=display_events,
            )

        if not error and pending_harness_payload is not None:
            try:
                self.apply_harness_update(
                    session_mode="code",
                    session_role=session_role,
                    payload=pending_harness_payload,
                    project_path=project_path,
                    assistant_text=pending_harness_text,
                    user_request=effective_prompt,
                )
                if post_apply_error and not error:
                    error = post_apply_error
            except Exception as exc:
                error = f"Failed to apply harness update: {exc}"
        elif not error and deferred_parse_error:
            error = deferred_parse_error
        elif not error:
            error = "No resonant-harness update emitted by automated role run"

        if timed_out and pending_harness_payload is None:
            error = f"Timed out after {float(timeout_seconds):.1f}s"

        return {
            "result": "\n\n".join(collected_text).strip(),
            "error": error,
            "steps": steps,
            "display_events": display_events,
            "backend_type": spec.backend_type,
            "model": spec.model,
            "timed_out": timed_out,
            "artifact_only": evaluation_mode == "artifacts" or generator_mode in {"artifacts", "structured", "repair"},
            "evaluation_mode": evaluation_mode,
            "role_mode": generator_mode if normalized_role == "generator" else evaluation_mode,
            "prechecked": False,
        }

    def run_harness_teacher_escalation(
        self,
        *,
        project_path: str,
        failed_role: str,
        reason: str,
        objective: str = "",
    ) -> dict[str, Any]:
        target_path = os.path.normpath(project_path or self._app.project.project_path)
        normalized_role = self._app.normalize_session_role("code", failed_role or "generator")
        provider, model = self.select_harness_teacher(
            session_role=normalized_role,
            reason=reason,
        )
        harness = HarnessWorkspace(target_path)
        harness.ensure_layout()
        recovery_prompt = "\n".join(
            part for part in (
                "Recover the stalled Resonant harness run using repository and harness evidence.",
                f"Failed role: {normalized_role}",
                f"Failure reason: {reason.strip() or 'unspecified'}",
                f"Objective: {objective.strip()}" if objective.strip() else "",
                "Diagnose the failure, make the necessary progress, and emit the required resonant-harness update.",
            ) if part
        )
        try:
            result = self.run_harness_role_once(
                project_path=target_path,
                session_role=normalized_role,
                prompt=recovery_prompt,
                backend_type=provider,
                model=model,
            )
            if result.get("error"):
                raise RuntimeError(str(result["error"]))
            applied_record = {
                "record_type": "harness_recovery",
                "teacher_provider": provider,
                "teacher_model": model,
                "project_path": target_path,
                "failed_role": normalized_role,
                "reason": reason,
                "objective": objective.strip(),
                "status": "applied",
                "recommended_role": normalized_role,
                "result": result.get("result", ""),
                "steps": result.get("steps", 0),
                "applied_at": time.time(),
            }
            harness.append_teacher_escalation(applied_record)
            harness.append_run_event(
                "teacher_intervention",
                {
                    "teacher_provider": provider,
                    "teacher_model": model,
                    "failed_role": normalized_role,
                    "reason": reason,
                    "recommended_role": normalized_role,
                    "status": "applied",
                },
            )
            return {
                "result": result.get("result", ""),
                "error": "",
                "teacher_provider": provider,
                "teacher_model": model,
                "recommended_role": normalized_role,
                "status_message": "Harness recovery applied",
                "record": applied_record,
            }
        except Exception as exc:
            failure_record = {
                "record_type": "harness_teacher_response",
                "teacher_provider": provider,
                "teacher_model": model,
                "project_path": target_path,
                "failed_role": normalized_role,
                "reason": reason,
                "objective": objective.strip(),
                "status": "failed",
                "error": str(exc),
                "captured_at": time.time(),
            }
            harness.append_teacher_escalation(failure_record)
            harness.append_run_event(
                "teacher_intervention",
                {
                    "teacher_provider": provider,
                    "teacher_model": model,
                    "failed_role": normalized_role,
                    "reason": reason,
                    "status": "failed",
                    "error": str(exc),
                },
            )
            raise


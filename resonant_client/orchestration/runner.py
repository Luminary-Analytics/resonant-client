"""
LocalSpecialistRunner — bridges PlanGraph nodes to the existing engine Session.

Given a `(node, graph)` pair, the runner builds a one-shot Session bound to the
specialist's profile (system prompt + tool allowlist + step budget), runs it
on the node's goal, and translates the engine events into a SpecialistResult
(status / confidence / summary / subgoals / verdict).

Confidence is heuristic — derived from `the session ended cleanly`, `error
count`, `step budget hit`. It's deliberately not asking the model to
self-report (models are bad at that). The walker uses confidence as a signal
to spawn auto-verify siblings; we'd rather over-spawn than under-spawn.

Plan / verify specialists also parse the model's final text for a JSON
code-fence containing structured output:
  - plan:     {"subgoals": [{"goal":..., "specialization":..., "depends_on":[indices]}]}
  - verify:   {"verdict": "pass"|"revise"|"blocked", "findings": ["..."]}

Parse failures are non-fatal: the result still ships with a tempered confidence.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Callable, Optional

from ..engine.session import Session
from .plan_graph import NodeSpecialization, NodeStatus, PlanGraph, PlanNode
from .specialists import (
    assemble_system_prompt,
    filter_tools_for_specialist,
    get_specialist,
)
from .walker import SpecialistResult

logger = logging.getLogger(__name__)


# ── Confidence model ────────────────────────────────────────────────────


def _confidence_from_outcome(
    *,
    error_count: int,
    hit_step_limit: bool,
    crashed: bool,
    produced_output: bool = True,
) -> float:
    """Map session-outcome signals to a 0.0–1.0 confidence number.

    A specialist that hit its step budget but still produced useful output
    (text or structured JSON) gets a much smaller penalty than one that
    timed out empty-handed — the work happened, just on the long end of
    the budget. Earlier this fired 0.4 unconditionally on step-limit,
    which made every multi-node intent fail to clear the skill-extraction
    threshold (avg ≥ 0.8) even when all nodes succeeded.
    """
    if crashed:
        return 0.0
    if hit_step_limit:
        # Step limit + output: meaningful work happened, just no headroom for cleanup.
        # Step limit + empty: the specialist spent its budget and surfaced nothing.
        return 0.7 if produced_output else 0.3
    if error_count > 2:
        return 0.5
    if error_count > 0:
        return 0.7
    return 1.0


# ── JSON code-fence extraction ─────────────────────────────────────────


_FENCE_RE = re.compile(
    r"```(?:json)?\s*\n(.+?)\n```", re.DOTALL,
)


def _extract_json_block(text: str) -> Optional[dict]:
    """Find the last fenced JSON block in `text` and parse it.

    Models tend to emit the structured payload at the end after some prose;
    iterating right-to-left grabs the structured one even if there's an earlier
    illustrative example fence. Returns None on parse failure.
    """
    if not text:
        return None
    matches = _FENCE_RE.findall(text)
    for raw in reversed(matches):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    # Fallback: whole-string parse (some models skip the fence)
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None
    return None


# ── Specialist runner ──────────────────────────────────────────────────


class LocalSpecialistRunner:
    """Run plan-graph nodes against a real engine Session.

    Construct one per intent (so the project_path / settings / backend are
    pinned). Pass the bound runner to `GraphWalker(runner=...)`.
    """

    def __init__(
        self,
        *,
        backend: Any,
        project_path: str,
        all_tools: list[dict],
        project_instructions: str = "",
        settings: Any = None,
        cancel_event: Optional[threading.Event] = None,
        on_session_event: Optional[Callable[[dict], None]] = None,
        audit_logger: Optional[Callable[..., None]] = None,
    ):
        self.backend = backend
        self.project_path = project_path
        self.all_tools = list(all_tools or [])
        self.project_instructions = project_instructions or ""
        self.settings = settings
        self.cancel_event = cancel_event or threading.Event()
        # Forward each engine event upstream so the GUI can show the chat trace
        # alongside the plan-graph viz.
        self.on_session_event = on_session_event or (lambda ev: None)
        # Per-tool-call audit hook (Phase 4 wires this).
        self.audit_logger = audit_logger

    def __call__(self, node: PlanNode, graph: PlanGraph) -> SpecialistResult:
        if self.cancel_event.is_set():
            return SpecialistResult(
                status=NodeStatus.ABANDONED, confidence=0.0,
                summary="cancelled before specialist could run",
            )
        try:
            return self._run_node(node, graph)
        except Exception as exc:
            logger.exception("LocalSpecialistRunner crashed for node %s", node.id)
            return SpecialistResult(
                status=NodeStatus.BLOCKED, confidence=0.0,
                summary=f"runner exception: {exc}",
            )

    # ── Internal ───────────────────────────────────────────────────

    def _run_node(self, node: PlanNode, graph: PlanGraph) -> SpecialistResult:
        profile = get_specialist(node.specialization)
        system_prompt = assemble_system_prompt(
            specialization=node.specialization,
            node_goal=node.goal,
            intent=graph.intent,
            project_conventions=self.project_instructions,
            extra_context=self._build_context_from_deps(node, graph),
        )
        allowed = filter_tools_for_specialist(node.specialization, self.all_tools)

        session = Session(
            backend=self.backend,
            max_steps=profile.max_steps,
            auto_approve=True,
            allowed_tools=allowed,
            project_instructions=system_prompt,
            cancel_event=self.cancel_event,
        )
        session.project_path = self.project_path
        # Hand the settings through so autonomy.check_floor can pick up custom
        # protected branches / budget cap / external paths during tool dispatch.
        session._settings_ref = self.settings

        return self._drive_session(session, node, graph)

    def _drive_session(
        self,
        session: Session,
        node: PlanNode,
        graph: PlanGraph,
    ) -> SpecialistResult:
        """Iterate session events, stream upstream, accumulate outcome signals."""
        started_at = time.time()
        text_chunks: list[str] = []
        tool_calls = 0
        error_count = 0
        crashed = False
        hit_step_limit = False
        step_count = 0

        crash_msg = ""
        try:
            for event in session.run(node.goal):
                self.on_session_event(event)

                etype = event.get("event") or event.get("type") or ""
                if etype == "text.delta":
                    delta = event.get("delta") or event.get("text") or ""
                    if delta:
                        text_chunks.append(delta)
                elif etype == "text.done":
                    text = event.get("text") or ""
                    if text and not text_chunks:
                        text_chunks.append(text)
                elif etype == "tool.call":
                    tool_calls += 1
                    if self.audit_logger:
                        try:
                            self.audit_logger(
                                kind="tool_call",
                                tool_name=event.get("tool_name") or event.get("name", ""),
                                args=event.get("args") or event.get("arguments") or {},
                                node_id=node.id,
                            )
                        except Exception:
                            logger.debug("audit_logger raised", exc_info=True)
                elif etype == "tool.result":
                    # Allowlist denials are an intentional signal to the model
                    # ("you can't use that tool"), not a system error. Don't let
                    # them inflate error_count and tank confidence — they're a
                    # working part of the autonomy boundary.
                    if event.get("is_error") and not event.get("denied"):
                        error_count += 1
                elif etype == "step.start":
                    step_count += 1
                elif etype == "session.end":
                    reason = event.get("reason") or event.get("end_reason") or ""
                    if reason in {"max_steps", "step_limit"}:
                        hit_step_limit = True
                    break
                elif etype == "error":
                    # Session emits an `error` event when it hits its own step
                    # budget (instead of session.end + reason=max_steps). Treat
                    # those as soft termination — DONE + low confidence — so a
                    # specialist that just ran out of room doesn't poison the
                    # graph with a BLOCKED node and abort downstream work.
                    msg = str(event.get("message", "") or "")
                    if "step limit" in msg.lower() or "max_steps" in msg.lower():
                        hit_step_limit = True
                        break
                    error_count += 1
                    crashed = True
                    break
        except Exception as exc:
            logger.exception("Session iteration crashed for node %s", node.id)
            crashed = True
            crash_msg = f"runner exception: {exc}"

        full_text = "".join(text_chunks).strip() or crash_msg
        duration_ms = (time.time() - started_at) * 1000.0

        # Did the specialist actually produce something? Used to soften the
        # step-limit penalty when the work landed despite running long.
        produced_output = bool(full_text) or tool_calls > 0

        confidence = _confidence_from_outcome(
            error_count=error_count,
            hit_step_limit=hit_step_limit,
            crashed=crashed,
            produced_output=produced_output,
        )

        # Specialization-specific post-processing
        subgoals: list[dict] = []
        verdict = ""
        findings: list[str] = []

        if node.specialization == NodeSpecialization.PLAN:
            subgoals, parse_ok = self._parse_subgoals(full_text)
            if not parse_ok:
                # Parse failed → soft ceiling at 0.5; the walker should still trust
                # the work somewhat, but downstream specialists won't lean on it.
                confidence = min(confidence, 0.5)
        elif node.specialization == NodeSpecialization.VERIFY:
            verdict, findings, parse_ok = self._parse_verdict(full_text)
            if not parse_ok:
                confidence = min(confidence, 0.5)
                verdict = verdict or "blocked"

        status = NodeStatus.DONE if not crashed else NodeStatus.BLOCKED
        if status == NodeStatus.DONE and self.cancel_event.is_set():
            status = NodeStatus.ABANDONED

        result = SpecialistResult(
            status=status,
            confidence=confidence,
            summary=full_text[:500] if full_text else "",
            data={
                "tool_calls": tool_calls,
                "error_count": error_count,
                "step_count": step_count,
                "hit_step_limit": hit_step_limit,
                "duration_ms": round(duration_ms, 1),
            },
            subgoals=subgoals,
            verdict=verdict,
            findings=findings,
        )
        return result

    # ── Helpers ────────────────────────────────────────────────────

    def _build_context_from_deps(self, node: PlanNode, graph: PlanGraph) -> str:
        """Assemble a "what did prior nodes find?" preamble from completed deps."""
        if not node.depends_on:
            return ""
        lines: list[str] = []
        for dep_id in node.depends_on:
            dep = graph.nodes.get(dep_id)
            if not dep or not isinstance(dep.result, dict):
                continue
            summary = dep.result.get("summary") or ""
            if summary:
                lines.append(f"From '{dep.goal}' ({dep.specialization}):")
                lines.append(summary[:400])
                lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _parse_subgoals(text: str) -> tuple[list[dict], bool]:
        """Pull a `{"subgoals": [...]}` payload out of a planner's response."""
        block = _extract_json_block(text)
        if not isinstance(block, dict):
            return [], False
        raw = block.get("subgoals")
        if not isinstance(raw, list):
            return [], False
        out: list[dict] = []
        for sg in raw:
            if not isinstance(sg, dict):
                continue
            goal = str(sg.get("goal", "")).strip()
            if not goal:
                continue
            spec = str(sg.get("specialization", "")).strip().lower() or "implement"
            deps_raw = sg.get("depends_on") or []
            deps: list[Any] = []
            for d in deps_raw:
                if isinstance(d, (int, str)):
                    deps.append(d)
            out.append({"goal": goal, "specialization": spec, "depends_on": deps})
        return out, bool(out)

    @staticmethod
    def _parse_verdict(text: str) -> tuple[str, list[str], bool]:
        """Pull a `{"verdict": "...", "findings": [...]}` payload out of a verifier's response."""
        block = _extract_json_block(text)
        if not isinstance(block, dict):
            # Heuristic fallback: inspect the prose for verdict keywords.
            lowered = (text or "").lower()
            if "verdict: pass" in lowered or "all checks pass" in lowered:
                return "pass", [], False
            if "verdict: revise" in lowered or "revisions required" in lowered:
                return "revise", [], False
            return "", [], False
        verdict = str(block.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "revise", "blocked"}:
            verdict = ""
        findings_raw = block.get("findings") or []
        findings = [str(f).strip() for f in findings_raw if isinstance(f, (str, int, float))]
        return verdict, findings, verdict != ""

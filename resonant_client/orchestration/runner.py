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
import os
import re
import threading
import time
from typing import Any, Callable, Optional

from ..engine.policies import policy_for_tier
from ..engine.sandbox import PathSandbox
from ..engine.session import Session
from .plan_graph import NodeSpecialization, NodeStatus, PlanGraph, PlanNode
from .specialists import (
    assemble_system_prompt,
    filter_tools_for_specialist,
)
from .walker import SpecialistResult

logger = logging.getLogger(__name__)


# v0.3.5 — `Working subdir:` declaration parser. Implementer specialists
# write a final summary; if they scaffolded files into a subdirectory,
# they declare it here so siblings inherit the path. The format is taught
# in the implement specialist's system_block (see specialists.py). Format:
#
#   Working subdir: web/
#   Working subdir: apps/api
#
# Rejected: absolute paths (`/foo`, `C:\foo`), parent traversal (`..`),
# and empty values. We coerce backslashes to forward slashes for
# cross-platform consistency.
_WORKING_SUBDIR_RE = re.compile(
    r"^\s*working\s+subdir\s*:\s*(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_working_subdir(text: str) -> Optional[str]:
    """Pull a `Working subdir: <path>` declaration from a specialist's
    summary. Returns the relative path (forward-slashes, no leading or
    trailing slash) or None if no valid declaration is present.
    """
    if not text:
        return None
    match = _WORKING_SUBDIR_RE.search(text)
    if not match:
        return None
    val = match.group(1).strip()
    # Strip wrapping quotes / backticks the model often adds.
    val = val.strip("`'\"")
    if not val:
        return None
    # Reject absolute paths and obvious garbage.
    if val.startswith(("/", "\\")):
        return None
    if len(val) >= 2 and val[1] == ":":  # Windows drive letter
        return None
    parts = val.replace("\\", "/").split("/")
    if any(p in ("..", "") for p in parts if p):
        # `web/../web` etc. — refuse rather than pretend to handle.
        return None
    normalized = "/".join(p for p in parts if p)
    return normalized or None


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

_PLAN_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "subgoals": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "specialization": {"type": "string"},
                    "depends_on": {
                        "type": "array",
                        "items": {"type": ["integer", "string"]},
                    },
                },
                "required": ["goal", "specialization", "depends_on"],
            },
        },
    },
    "required": ["subgoals"],
}

_VERIFY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "revise", "blocked"],
        },
        "findings": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["verdict", "findings"],
}


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
        specialist_backend_resolver: Optional[Callable[[str], Any]] = None,
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
        # v0.5.8a1 — per-specialist backend routing. Optional callable
        # that maps a NodeSpecialization (the string value, e.g. "reflect"
        # or "plan_deep") to a backend instance. Returns None to fall
        # through to `self.backend`. Lets the user pin pro for REFLECT/
        # PLAN_DEEP and flash for IMPLEMENT/EXPLORE without changing the
        # session-level default. See AppState._build_specialist_backend
        # for the production wiring.
        self._specialist_backend_resolver = specialist_backend_resolver

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

    def _resolve_backend_for(self, specialization: str) -> Any:
        """v0.5.8a1 — return the backend to use for this specialist call.

        Behavior:
          - No resolver configured → default backend
          - Resolver returns None → default backend
          - Resolver returns a backend → use it
          - Resolver raises → log + fall through to default

        We never let a routing failure block a specialist from running.
        The default backend is always a working option; the resolver is
        a "smarter model for hard moments" optimization.
        """
        if self._specialist_backend_resolver is None:
            return self.backend
        try:
            override = self._specialist_backend_resolver(specialization)
        except Exception:
            logger.exception(
                "specialist_backend_resolver raised for %s; falling back",
                specialization,
            )
            return self.backend
        if override is None:
            return self.backend
        return override

    def _run_node(self, node: PlanNode, graph: PlanGraph) -> SpecialistResult:
        # v0.3.5 — inherit `working_subdir` from completed deps. If a
        # parent implementer scaffolded into `<root>/web/`, this child
        # specialist runs there too instead of starting back at the
        # project root and re-discovering the layout. Explicit subdir
        # already on the node (set by some other code path) wins; only
        # falls back to dep-chain inheritance when unset.
        if not node.working_subdir:
            for dep_id in node.depends_on:
                dep = graph.nodes.get(dep_id)
                if dep and dep.working_subdir:
                    node.working_subdir = dep.working_subdir
                    break

        # Resolve the effective working directory. project_path stays
        # the intent root; the session's project_path is rooted there
        # plus any inherited subdir, while the sandbox keeps the full
        # project as the trust root.
        workspace_sandbox = PathSandbox(self.project_path)
        effective_path = workspace_sandbox.project_path
        if node.working_subdir:
            try:
                effective_path = workspace_sandbox.validate_path(
                    os.path.join(self.project_path, node.working_subdir)
                )
            except (TypeError, ValueError):
                # Defensive — if the subdir somehow has weird types,
                # fall back to the project root. Better to be slightly
                # off than crash the runner.
                effective_path = self.project_path
                logger.warning(
                    "working_subdir join failed for node %s subdir=%r",
                    node.id,
                    node.working_subdir,
                )

        system_prompt = assemble_system_prompt(
            specialization=node.specialization,
            node_goal=node.goal,
            intent=graph.intent,
            project_conventions=self.project_instructions,
            extra_context=self._build_context_from_deps(node, graph),
        )
        allowed = filter_tools_for_specialist(node.specialization, self.all_tools)

        # v0.5.8a1 — per-specialist backend resolution. Defaults to the
        # runner's default backend; resolver can override per-call (e.g.
        # pro for REFLECT, flash for IMPLEMENT). Resolution failures are
        # logged and fall through to the default — never let a missing
        # override block a specialist from running.
        backend_for_call = self._resolve_backend_for(node.specialization)

        session = Session(
            backend=backend_for_call,
            auto_approve=True,
            allowed_tools=allowed,
            project_instructions=system_prompt,
            cancel_event=self.cancel_event,
        )
        session.project_path = effective_path
        session.sandbox = workspace_sandbox
        session.execution_policy = policy_for_tier("full-auto")
        # Hand the settings through so autonomy.check_floor can pick up custom
        # protected branches / budget cap / external paths during tool dispatch.
        session._settings_ref = self.settings

        result = self._drive_session(session, node, graph)

        # v0.3.5 — record any newly-declared `Working subdir:` from the
        # specialist's summary. An implementer that scaffolds into a
        # fresh subdir announces it here; siblings will inherit it via
        # the dep-chain walk above on their next dispatch. We only
        # *upgrade* (set if unset, or replace with a more-specific
        # nested path); we never clear an inherited path.
        declared = _extract_working_subdir(result.summary or "")
        if declared:
            if not node.working_subdir or declared != node.working_subdir:
                # Only adopt if it's a strict refinement of what we
                # had, or the first declaration. This guards against
                # an implementer accidentally re-declaring the parent's
                # broader path as if it were a new pivot.
                if not node.working_subdir or declared.startswith(node.working_subdir + "/"):
                    node.working_subdir = declared

        return result

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
        structured_output_repaired = False

        # v0.5.1a3 — PLAN_DEEP shares the JSON-subgoals output schema
        # with PLAN, so it goes through the same parser. Without this,
        # PLAN_DEEP responses would never have their subgoals extracted
        # and the walker would think every pro autonomous mission is
        # un-decomposable.
        if node.specialization in (
            NodeSpecialization.PLAN, NodeSpecialization.PLAN_DEEP,
        ):
            subgoals, parse_ok = self._parse_subgoals(full_text)
            if not parse_ok:
                repaired = self._repair_structured_output(
                    session.backend, full_text, _PLAN_OUTPUT_SCHEMA,
                )
                if repaired is not None:
                    subgoals, parse_ok = self._parse_subgoals(json.dumps(repaired))
                    structured_output_repaired = parse_ok
            if not parse_ok:
                # Parse failed → soft ceiling at 0.5; the walker should still trust
                # the work somewhat, but downstream specialists won't lean on it.
                confidence = min(confidence, 0.5)
        elif node.specialization == NodeSpecialization.VERIFY:
            verdict, findings, parse_ok = self._parse_verdict(full_text)
            if not parse_ok:
                repaired = self._repair_structured_output(
                    session.backend, full_text, _VERIFY_OUTPUT_SCHEMA,
                )
                if repaired is not None:
                    verdict, findings, parse_ok = self._parse_verdict(json.dumps(repaired))
                    structured_output_repaired = parse_ok
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
                "structured_output_repaired": structured_output_repaired,
            },
            subgoals=subgoals,
            verdict=verdict,
            findings=findings,
        )
        return result

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _repair_structured_output(backend: Any, text: str, schema: dict) -> Optional[dict]:
        """Use constrained decoding when a specialist's JSON fence drifted."""
        generator = getattr(backend, "generate_structured", None)
        if not callable(generator) or not text:
            return None
        try:
            result = generator(
                "Convert the following completed specialist response into the requested "
                "JSON structure without adding facts:\n\n" + text[-12_000:],
                schema,
                max_tokens=2048,
            )
        except Exception:
            logger.warning("Structured specialist-output repair failed", exc_info=True)
            return None
        return result if isinstance(result, dict) else None

    def _build_context_from_deps(self, node: PlanNode, graph: PlanGraph) -> str:
        """Assemble a "what did prior nodes find?" preamble from completed deps."""
        if not node.depends_on:
            return ""
        lines: list[str] = []
        # v0.3.5 — surface inherited working_subdir at the top of the
        # context so the specialist sees it before anything else. Even
        # though session.project_path is already pointed there, telling
        # the model "you're working inside `web/`" prevents path
        # confusion in tool args and makes the summary less ambiguous
        # ("relative to what?") for downstream specialists.
        if node.working_subdir:
            lines.append(
                f"Working directory: a previous specialist scaffolded into "
                f"`{node.working_subdir}/`. Your tools (file_read, glob, file_write, "
                f"bash) all run inside that subdir — you don't need to prefix paths "
                f"with `{node.working_subdir}/`."
            )
            lines.append("")
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

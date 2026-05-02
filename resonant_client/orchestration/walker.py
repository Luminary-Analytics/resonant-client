"""
GraphWalker — runs a plan-graph by spawning specialists per node.

Loop:
    while not graph.is_complete():
        pick next runnable node(s)
        for each, spawn the right specialist (driven by node.specialization)
        collect result, update node status + confidence
        if confidence dropped below threshold AND no verify sibling exists:
            auto-spawn a `verify` sibling
        if a verify reports failure:
            auto-spawn a `repair` child

The walker is engine-agnostic — it takes a callback that knows how to actually
execute one specialist node (since that path differs between local Session
runs and remote `resonant` engine calls). The callback returns a result dict
that the walker maps back onto node status + confidence.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional

from .plan_graph import (
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    new_node_id,
)
from .specialists import get_specialist


logger = logging.getLogger(__name__)


# ── Events emitted by the walker ────────────────────────────────────────


@dataclass
class WalkerEvent:
    """One event in the walker's stream. The UI consumes these."""
    kind: str                       # node.start / node.done / node.confidence /
                                    # plan.rewrite / plan.complete / soft_checkpoint
    node_id: Optional[str] = None
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


# ── Specialist execution callback signature ─────────────────────────────


class SpecialistResult:
    """
    What a specialist returns to the walker.

    - status:     NodeStatus to apply (DONE / BLOCKED / ABANDONED)
    - confidence: 0.0–1.0
    - summary:    short human-readable summary of what happened
    - data:       free-form details (tool calls, files touched, ...)
    - subgoals:   only relevant for `plan` specialists — list of dicts:
                    [{"goal": str, "specialization": str, "depends_on": [str, ...]}]
                  The walker turns these into PlanNode children of the plan node.
    - verdict:    only relevant for `verify` specialists — "pass" / "revise" / "blocked"
    - findings:   only relevant for `verify` — list of strings to feed a `repair` child
    """
    def __init__(
        self,
        *,
        status: str = NodeStatus.DONE,
        confidence: float = 1.0,
        summary: str = "",
        data: Optional[dict] = None,
        subgoals: Optional[list[dict]] = None,
        verdict: str = "",
        findings: Optional[list[str]] = None,
    ):
        self.status = status
        self.confidence = max(0.0, min(1.0, confidence))
        self.summary = summary
        self.data = data or {}
        self.subgoals = subgoals or []
        self.verdict = verdict
        self.findings = findings or []

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "confidence": self.confidence,
            "summary": self.summary,
            "data": self.data,
            "subgoals": self.subgoals,
            "verdict": self.verdict,
            "findings": self.findings,
        }


# Callback: takes a node + the graph (read-only context), returns a result.
# The orchestrator wires this to either a local Session.run or a remote engine call.
SpecialistRunner = Callable[[PlanNode, PlanGraph], SpecialistResult]


# Both planner specializations share the same JSON-subgoals output
# schema and same expansion path. Keep this tuple in sync if a third
# planner variant is added in the future.
_PLANNER_SPECS = frozenset({
    NodeSpecialization.PLAN,
    NodeSpecialization.PLAN_DEEP,
})


# v0.5.1a3 — when a planner returns no parseable subgoals and the
# walker re-dispatches, the new node's goal gets this prefix. The
# planner sees the hint AND the original goal so it can correct
# course on the second attempt. The prefix is also how we recognize
# retry nodes when we walk back to the original (counter keying).
_PLANNER_RETRY_PREFIX = (
    "[RETRY: your previous attempt did not emit a parseable JSON "
    "envelope. Re-read the FORMAT REMINDER section of your prompt "
    "and emit `{\"subgoals\": [...]}` as your final output, in a "
    "fenced ```json block.]\n\n"
)


def _strip_retry_prefix(goal: str) -> str:
    """Remove the planner retry hint to recover the original goal."""
    if not goal:
        return goal
    if goal.startswith(_PLANNER_RETRY_PREFIX):
        return goal[len(_PLANNER_RETRY_PREFIX):]
    return goal


# ── The walker itself ───────────────────────────────────────────────────


class GraphWalker:
    """Drives a plan-graph to completion by dispatching specialists.

    Stateless aside from the cancel_event. Construct one per intent / run.
    """

    def __init__(
        self,
        *,
        runner: SpecialistRunner,
        on_event: Optional[Callable[[WalkerEvent], None]] = None,
        cancel_event: Optional[threading.Event] = None,
        max_total_nodes: int = 200,
        max_repair_attempts: int = 2,
        max_planner_retries: int = 1,
    ):
        self.runner = runner
        self.on_event = on_event or (lambda ev: None)
        self.cancel_event = cancel_event or threading.Event()
        self.max_total_nodes = max_total_nodes
        self.max_repair_attempts = max_repair_attempts
        # v0.5.1a3 — when a PLAN / PLAN_DEEP node returns no
        # parseable subgoals, retry once with a hint-prefixed
        # sibling. Without this, the walker silently no-ops the
        # whole plan-graph (Pro's failure mode in v0.5.0 GA smoke).
        self.max_planner_retries = max_planner_retries
        # Per-node retry count, keyed by the ORIGINAL planner node id.
        self._planner_retries: dict[str, int] = {}

    # ── Public driver ──────────────────────────────────────────────────

    def run(self, graph: PlanGraph) -> PlanGraph:
        """Walk the graph until complete or cancelled. Returns the (mutated) graph."""
        seen_node_ids: set[str] = set()
        while not graph.is_complete():
            if self.cancel_event.is_set():
                logger.info("GraphWalker cancelled before completion (intent=%s)", graph.intent_id)
                return graph
            if len(graph.nodes) > self.max_total_nodes:
                logger.warning(
                    "GraphWalker hit max_total_nodes (%d) for intent %s; abandoning remaining work",
                    self.max_total_nodes, graph.intent_id,
                )
                for n in graph.nodes.values():
                    if not n.is_terminal():
                        graph.mark_abandoned(n.id, reason="walker hit max_total_nodes")
                break

            runnable = graph.next_runnable()
            if not runnable:
                # No runnable nodes but graph not complete → all remaining are blocked.
                logger.info(
                    "No runnable nodes left in intent %s; %d non-terminal nodes blocked",
                    graph.intent_id,
                    sum(1 for n in graph.nodes.values() if not n.is_terminal()),
                )
                break

            for node in runnable:
                if node.id in seen_node_ids:
                    # Defensive: prevent infinite loops if a node somehow reappears as runnable.
                    continue
                seen_node_ids.add(node.id)
                self._run_one(graph, node)

        if graph.is_complete():
            self.on_event(WalkerEvent(kind="plan.complete", payload={
                "intent_id": graph.intent_id,
                "node_count": len(graph.nodes),
                "all_done": all(n.status == NodeStatus.DONE for n in graph.nodes.values()),
            }))
        return graph

    # ── Per-node execution ─────────────────────────────────────────────

    def _run_one(self, graph: PlanGraph, node: PlanNode) -> None:
        graph.mark_running(node.id)
        self.on_event(WalkerEvent(kind="node.start", node_id=node.id, payload={
            "goal": node.goal,
            "specialization": node.specialization,
        }))

        try:
            result = self.runner(node, graph)
        except Exception as exc:
            logger.exception("Specialist runner failed for node %s", node.id)
            graph.mark_blocked(node.id, reason=f"runner exception: {exc}")
            self.on_event(WalkerEvent(kind="node.done", node_id=node.id, payload={
                "status": NodeStatus.BLOCKED,
                "error": str(exc),
            }))
            return

        # Apply status + confidence
        if result.status == NodeStatus.DONE:
            graph.mark_done(node.id, result=result.to_dict(), confidence=result.confidence)
        elif result.status == NodeStatus.BLOCKED:
            graph.mark_blocked(node.id, reason=result.summary or "specialist returned BLOCKED")
        elif result.status == NodeStatus.ABANDONED:
            graph.mark_abandoned(node.id, reason=result.summary or "specialist returned ABANDONED")
        else:
            graph.mark_done(node.id, result=result.to_dict(), confidence=result.confidence)

        self.on_event(WalkerEvent(kind="node.done", node_id=node.id, payload={
            "status": graph.nodes[node.id].status,
            "confidence": graph.nodes[node.id].confidence,
            "summary": result.summary,
            "verdict": result.verdict,
        }))

        # ── Post-execution decisions ──────────────────────────────────

        # 1. Planner specialist (PLAN or PLAN_DEEP) returned subgoals
        #    → expand them as children. If the planner returned
        #    nothing parseable AND we haven't retried yet, spawn a
        #    sibling planner with a JSON-envelope reminder. This
        #    catches the v0.5.0 GA pro failure mode where the model
        #    emitted `<tool_call>` text instead of a JSON envelope
        #    and the walker silently no-op'd the whole plan-graph.
        if node.specialization in _PLANNER_SPECS:
            if result.subgoals:
                self._expand_subgoals(graph, node, result.subgoals)
            else:
                self._maybe_retry_planner(graph, node)

        # 2. `verify` specialist with a non-pass verdict → spawn `repair`
        if node.specialization == NodeSpecialization.VERIFY:
            if result.verdict == "revise":
                self._spawn_repair(graph, verify_node=node, findings=result.findings)
            # "blocked" verdict → just leave the chain blocked, no auto-repair

        # 3. Confidence dropped below specialist threshold AND this isn't already
        #    a verify → auto-spawn a verify sibling so the next pass second-guesses.
        elif node.specialization not in (NodeSpecialization.VERIFY, NodeSpecialization.PLAN):
            profile = get_specialist(node.specialization)
            if (
                graph.nodes[node.id].confidence < profile.confidence_threshold
                and not self._has_verify_sibling(graph, node.id)
            ):
                self._spawn_verify_sibling(graph, node)

    # ── Auto-spawning helpers ─────────────────────────────────────────

    def _expand_subgoals(self, graph: PlanGraph, plan_node: PlanNode, subgoals: list[dict]) -> None:
        """Turn a planner's output into children of `plan_node`."""
        added: list[str] = []
        # First pass: create id mappings so subgoals can reference each other by goal-text in deps.
        new_ids: dict[int, str] = {}
        for i in range(len(subgoals)):
            new_ids[i] = new_node_id()

        for i, sg in enumerate(subgoals):
            goal = str(sg.get("goal", "")).strip()
            spec = str(sg.get("specialization", NodeSpecialization.IMPLEMENT)).strip()
            if spec not in NodeSpecialization.ALL:
                spec = NodeSpecialization.IMPLEMENT
            # Translate sibling deps (positional indexes) into node ids
            deps_raw = sg.get("depends_on", []) or []
            deps: list[str] = []
            for d in deps_raw:
                if isinstance(d, int) and 0 <= d < i:
                    deps.append(new_ids[d])
                elif isinstance(d, str) and d in graph.nodes:
                    deps.append(d)
            try:
                new_node = PlanNode(
                    id=new_ids[i], intent_id=graph.intent_id,
                    goal=goal or f"subgoal-{i}",
                    specialization=spec,
                    parent_id=plan_node.id,
                    depends_on=deps,
                )
                graph.add_node(new_node)
                added.append(new_node.id)
            except ValueError as exc:
                logger.warning("Skipped invalid subgoal %d for node %s: %s", i, plan_node.id, exc)

        if added:
            self.on_event(WalkerEvent(kind="plan.rewrite", node_id=plan_node.id, payload={
                "added": added,
                "reason": "planner expanded subgoals",
            }))

    def _maybe_retry_planner(self, graph: PlanGraph, planner_node: PlanNode) -> None:
        """v0.5.1a3 — when a planner returned no parseable subgoals,
        spawn a sibling planner with a JSON-envelope reminder. Caps
        retries at `self.max_planner_retries` so we don't loop
        forever on a fundamentally-broken planner.

        The sibling node's GOAL is the original goal plus a hint
        prefix calling out the parse failure. This works for both
        PLAN and PLAN_DEEP — the prompts themselves stay generic;
        the per-attempt context comes from the goal.

        We trace retries by pointing each sibling back to the
        ORIGINAL planner via `parent_id` (siblings share parent).
        Counter is keyed on the original node id.
        """
        # Find the "original" planner — walk back through any prior
        # retry siblings to the first one (root of the retry chain).
        # In practice the chain depth is ≤ max_planner_retries.
        original_id = planner_node.id
        # If this node was itself spawned as a retry, point back to
        # the original via the data field we stash below.
        retry_marker = (planner_node.goal or "").startswith(_PLANNER_RETRY_PREFIX)
        if retry_marker:
            # Find the source node — its id is recorded in our
            # tracking dict's keys; we walk siblings to find the
            # one that matches the suffix-stripped goal.
            stripped_goal = _strip_retry_prefix(planner_node.goal)
            for n in graph.nodes.values():
                if (n.id != planner_node.id
                        and n.specialization in _PLANNER_SPECS
                        and n.goal == stripped_goal):
                    original_id = n.id
                    break

        attempts = self._planner_retries.get(original_id, 0)
        if attempts >= self.max_planner_retries:
            logger.info(
                "Planner %s exhausted %d retry attempts; giving up",
                original_id, self.max_planner_retries,
            )
            return

        self._planner_retries[original_id] = attempts + 1
        retry_id = new_node_id()
        # Strip any pre-existing retry prefix from the previous
        # planner's goal so we don't stack hints across attempts.
        original_goal_text = _strip_retry_prefix(planner_node.goal or "")
        retry_node = PlanNode(
            id=retry_id,
            intent_id=graph.intent_id,
            goal=_PLANNER_RETRY_PREFIX + original_goal_text,
            specialization=planner_node.specialization,
            parent_id=planner_node.parent_id,
            depends_on=list(planner_node.depends_on),
        )
        graph.add_node(retry_node)
        self.on_event(WalkerEvent(kind="plan.rewrite", node_id=planner_node.id, payload={
            "added": [retry_id],
            "reason": (
                f"planner retry {attempts + 1}/{self.max_planner_retries} — "
                f"previous attempt didn't emit parseable JSON subgoals"
            ),
        }))

    def _spawn_verify_sibling(self, graph: PlanGraph, node: PlanNode) -> None:
        verify = PlanNode(
            id=new_node_id(),
            intent_id=graph.intent_id,
            goal=f"Verify the result of: {node.goal}",
            specialization=NodeSpecialization.VERIFY,
            parent_id=node.parent_id,
            depends_on=[node.id],
        )
        graph.add_node(verify)
        self.on_event(WalkerEvent(kind="plan.rewrite", node_id=node.id, payload={
            "added": [verify.id],
            "reason": "low confidence triggered auto-verify",
        }))

    def _spawn_repair(
        self,
        graph: PlanGraph,
        *,
        verify_node: PlanNode,
        findings: list[str],
    ) -> None:
        # Cap repair attempts so we don't loop forever.
        existing_repairs = [
            n for n in graph.nodes.values()
            if n.parent_id == verify_node.parent_id and n.specialization == NodeSpecialization.REPAIR
        ]
        if len(existing_repairs) >= self.max_repair_attempts:
            graph.mark_abandoned(verify_node.id, reason="max repair attempts reached")
            return

        findings_block = "\n".join(f"- {f}" for f in findings if f)
        repair_goal = f"Fix the issues flagged by verification:\n{findings_block}" if findings_block else "Address verification failures."
        repair = PlanNode(
            id=new_node_id(),
            intent_id=graph.intent_id,
            goal=repair_goal,
            specialization=NodeSpecialization.REPAIR,
            parent_id=verify_node.parent_id,
            depends_on=[verify_node.id],
        )
        graph.add_node(repair)
        # Re-verify after the repair finishes
        re_verify = PlanNode(
            id=new_node_id(),
            intent_id=graph.intent_id,
            goal=f"Re-verify after repair: {verify_node.goal}",
            specialization=NodeSpecialization.VERIFY,
            parent_id=verify_node.parent_id,
            depends_on=[repair.id],
        )
        graph.add_node(re_verify)
        self.on_event(WalkerEvent(kind="plan.rewrite", node_id=verify_node.id, payload={
            "added": [repair.id, re_verify.id],
            "reason": "verify reported revise; spawned repair + re-verify",
        }))

    @staticmethod
    def _has_verify_sibling(graph: PlanGraph, node_id: str) -> bool:
        """True if any sibling under the same parent already verifies this node."""
        node = graph.nodes[node_id]
        for n in graph.nodes.values():
            if n.id == node_id:
                continue
            if n.parent_id == node.parent_id and n.specialization == NodeSpecialization.VERIFY:
                if node_id in n.depends_on:
                    return True
        return False

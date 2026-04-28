"""Tests for the GraphWalker — drives plan-graphs to completion via specialists."""

from __future__ import annotations

import threading
from typing import Callable

import pytest

from resonant_client.orchestration import (
    GraphWalker,
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    SpecialistResult,
    WalkerEvent,
    new_node_id,
)


# ── Test runner that records what was dispatched ────────────────────────


class RecordingRunner:
    """Configurable specialist runner that records calls + returns scripted results."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []   # (node_id, specialization)
        self.results_by_node_id: dict[str, SpecialistResult] = {}
        self.results_by_specialization: dict[str, SpecialistResult] = {}
        self.default = SpecialistResult(
            status=NodeStatus.DONE, confidence=1.0, summary="ok",
        )

    def __call__(self, node: PlanNode, graph: PlanGraph) -> SpecialistResult:
        self.calls.append((node.id, node.specialization))
        if node.id in self.results_by_node_id:
            return self.results_by_node_id[node.id]
        if node.specialization in self.results_by_specialization:
            return self.results_by_specialization[node.specialization]
        return self.default


def _node(graph, *, goal, parent=None, deps=None, spec=NodeSpecialization.IMPLEMENT):
    n = PlanNode(
        id=new_node_id(), intent_id=graph.intent_id,
        goal=goal, specialization=spec,
        parent_id=parent, depends_on=list(deps or []),
    )
    graph.add_node(n)
    return n


# ── Basic dispatch ──────────────────────────────────────────────────────


def test_walker_runs_each_node_once_in_dep_order():
    g = PlanGraph.new("intent")
    a = _node(g, goal="a")
    b = _node(g, goal="b", deps=[a.id])
    c = _node(g, goal="c", deps=[b.id])

    runner = RecordingRunner()
    walker = GraphWalker(runner=runner)
    walker.run(g)

    call_ids = [cid for cid, _ in runner.calls]
    assert call_ids == [a.id, b.id, c.id]
    assert g.is_complete()
    assert all(g.nodes[nid].status == NodeStatus.DONE for nid in (a.id, b.id, c.id))


def test_walker_emits_events():
    g = PlanGraph.new("intent")
    a = _node(g, goal="a")

    events: list[WalkerEvent] = []
    runner = RecordingRunner()
    walker = GraphWalker(runner=runner, on_event=events.append)
    walker.run(g)

    kinds = [e.kind for e in events]
    assert "node.start" in kinds
    assert "node.done" in kinds
    assert "plan.complete" in kinds


# ── Confidence-driven auto-verify ───────────────────────────────────────


def test_low_confidence_implement_spawns_verify_sibling():
    g = PlanGraph.new("intent")
    a = _node(g, goal="implement risky thing")

    runner = RecordingRunner()
    runner.results_by_specialization[NodeSpecialization.IMPLEMENT] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.3, summary="not super confident",
    )
    walker = GraphWalker(runner=runner)
    walker.run(g)

    # The auto-verify sibling should have been added and run
    verify_nodes = [n for n in g.nodes.values() if n.specialization == NodeSpecialization.VERIFY]
    assert len(verify_nodes) == 1, f"expected 1 verify sibling, got {len(verify_nodes)}"
    assert a.id in verify_nodes[0].depends_on


def test_high_confidence_skips_auto_verify():
    g = PlanGraph.new("intent")
    _node(g, goal="implement easy thing")

    runner = RecordingRunner()
    runner.results_by_specialization[NodeSpecialization.IMPLEMENT] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.95, summary="fine",
    )
    walker = GraphWalker(runner=runner)
    walker.run(g)

    verify_nodes = [n for n in g.nodes.values() if n.specialization == NodeSpecialization.VERIFY]
    assert verify_nodes == []


def test_verify_does_not_recurse_on_itself():
    """A verify node with low confidence shouldn't auto-spawn another verify."""
    g = PlanGraph.new("intent")
    _node(g, goal="check x", spec=NodeSpecialization.VERIFY)

    runner = RecordingRunner()
    runner.results_by_specialization[NodeSpecialization.VERIFY] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.1, summary="meh",
        verdict="pass",
    )
    walker = GraphWalker(runner=runner)
    walker.run(g)

    verify_count = sum(1 for n in g.nodes.values() if n.specialization == NodeSpecialization.VERIFY)
    assert verify_count == 1, "verify shouldn't auto-spawn more verifies on itself"


# ── Verify-revise loop spawns repair + re-verify ────────────────────────


def test_verify_revise_spawns_repair_and_reverify():
    g = PlanGraph.new("intent")
    impl = _node(g, goal="implement")
    verify = _node(g, goal="check it", spec=NodeSpecialization.VERIFY, deps=[impl.id])

    runner = RecordingRunner()
    runner.results_by_node_id[verify.id] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.9,
        verdict="revise", findings=["x is broken", "y is missing"],
    )
    # Make impl high-confidence so no extra verify gets spawned for it
    runner.results_by_node_id[impl.id] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.95, summary="done",
    )
    walker = GraphWalker(runner=runner, max_repair_attempts=2)
    walker.run(g)

    repairs = [n for n in g.nodes.values() if n.specialization == NodeSpecialization.REPAIR]
    assert len(repairs) >= 1
    # Repair goal should reference findings
    assert any("x is broken" in r.goal for r in repairs)
    # Re-verify gets added too
    verifies = [n for n in g.nodes.values() if n.specialization == NodeSpecialization.VERIFY]
    assert len(verifies) >= 2  # original + at least one re-verify


def test_repair_attempts_capped():
    g = PlanGraph.new("intent")
    impl = _node(g, goal="implement")
    verify = _node(g, goal="check it", spec=NodeSpecialization.VERIFY, deps=[impl.id])

    runner = RecordingRunner()
    # Every verify says "revise" → would loop forever without the cap
    runner.results_by_specialization[NodeSpecialization.VERIFY] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.9,
        verdict="revise", findings=["still broken"],
    )
    runner.results_by_specialization[NodeSpecialization.IMPLEMENT] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.95,
    )
    runner.results_by_specialization[NodeSpecialization.REPAIR] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.9,
    )
    walker = GraphWalker(runner=runner, max_repair_attempts=2)
    walker.run(g)

    repairs = [n for n in g.nodes.values() if n.specialization == NodeSpecialization.REPAIR]
    assert len(repairs) <= 2, f"max_repair_attempts not honored: {len(repairs)}"


# ── Planner expansion ───────────────────────────────────────────────────


def test_plan_specialist_expands_subgoals_into_children():
    g = PlanGraph.new("ship dark mode")
    plan_node = _node(g, goal="decompose this", spec=NodeSpecialization.PLAN)

    runner = RecordingRunner()
    runner.results_by_node_id[plan_node.id] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.9,
        subgoals=[
            {"goal": "research prefers-color-scheme", "specialization": "research"},
            {"goal": "add CSS variables", "specialization": "implement", "depends_on": [0]},
            {"goal": "add toggle UI", "specialization": "implement", "depends_on": [1]},
        ],
    )
    walker = GraphWalker(runner=runner)
    walker.run(g)

    children = g.children_of(plan_node.id)
    assert len(children) == 3
    goals = [c.goal for c in children]
    assert "research prefers-color-scheme" in goals
    assert "add CSS variables" in goals
    # Dependencies translated correctly: 2nd child depends on 1st
    research_node = next(c for c in children if "prefers-color-scheme" in c.goal)
    css_node = next(c for c in children if "CSS variables" in c.goal)
    toggle_node = next(c for c in children if "toggle UI" in c.goal)
    assert research_node.id in css_node.depends_on
    assert css_node.id in toggle_node.depends_on


def test_invalid_subgoal_is_skipped_not_crashing():
    g = PlanGraph.new("intent")
    plan_node = _node(g, goal="decompose", spec=NodeSpecialization.PLAN)

    runner = RecordingRunner()
    runner.results_by_node_id[plan_node.id] = SpecialistResult(
        status=NodeStatus.DONE, confidence=0.9,
        subgoals=[
            {"goal": "valid one", "specialization": "implement"},
            {"goal": "bad spec", "specialization": "nonsense"},  # falls back to implement
        ],
    )
    walker = GraphWalker(runner=runner)
    walker.run(g)

    # Both children should exist (bad spec falls back to default rather than crashing)
    children = g.children_of(plan_node.id)
    assert len(children) == 2


# ── Cancellation & limits ───────────────────────────────────────────────


def test_cancel_event_stops_walker_between_nodes():
    g = PlanGraph.new("intent")
    a = _node(g, goal="a")
    b = _node(g, goal="b", deps=[a.id])

    cancel = threading.Event()

    def runner(node: PlanNode, graph: PlanGraph) -> SpecialistResult:
        # Cancel after this node finishes — walker should bail before b runs.
        cancel.set()
        return SpecialistResult(status=NodeStatus.DONE, confidence=1.0)

    walker = GraphWalker(runner=runner, cancel_event=cancel)
    walker.run(g)

    assert g.nodes[a.id].status == NodeStatus.DONE
    # b should never have run
    assert g.nodes[b.id].status == NodeStatus.PENDING


def test_runner_exception_blocks_node_does_not_crash_walker():
    g = PlanGraph.new("intent")
    a = _node(g, goal="will fail")
    b = _node(g, goal="will run")  # independent of a

    def runner(node: PlanNode, graph: PlanGraph) -> SpecialistResult:
        if node.id == a.id:
            raise RuntimeError("simulated crash")
        return SpecialistResult(status=NodeStatus.DONE, confidence=1.0)

    walker = GraphWalker(runner=runner)
    walker.run(g)

    assert g.nodes[a.id].status == NodeStatus.BLOCKED
    assert g.nodes[b.id].status == NodeStatus.DONE


def test_max_total_nodes_cap_aborts_runaway_planner():
    """A planner that keeps spawning subgoals forever should hit the node cap."""
    g = PlanGraph.new("intent")
    plan_node = _node(g, goal="decompose forever", spec=NodeSpecialization.PLAN)

    def runaway_runner(node: PlanNode, graph: PlanGraph) -> SpecialistResult:
        if node.specialization == NodeSpecialization.PLAN:
            return SpecialistResult(
                status=NodeStatus.DONE, confidence=0.9,
                subgoals=[{"goal": f"sub-{i}", "specialization": "plan"} for i in range(5)],
            )
        return SpecialistResult(status=NodeStatus.DONE, confidence=1.0)

    walker = GraphWalker(runner=runaway_runner, max_total_nodes=20)
    walker.run(g)

    # Cap is checked at the top of each outer-loop iteration, so a single
    # planner expansion can briefly push past the limit. The important
    # contracts: (1) walker terminates, (2) no infinite growth, (3) all
    # surviving non-terminal nodes get abandoned.
    assert len(g.nodes) < 50, f"runaway planner should have been stopped, got {len(g.nodes)} nodes"
    pending = [n for n in g.nodes.values() if n.status == NodeStatus.PENDING]
    assert pending == [], "all pending nodes should be abandoned after the cap"

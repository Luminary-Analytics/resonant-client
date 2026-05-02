"""Tests for v0.5.1a3 — walker auto-retry on unparseable planner output.

The v0.5.0 GA pro smoke surfaced a real risk: when a planner
specialist returns NO parseable subgoals (the model emitted a
`<tool_call>` block as text, or got cut off mid-JSON, or just
forgot the envelope), the walker silently no-op'd the whole
plan-graph. From the user's POV: a long spinner followed by
"stuck" with no diagnostic surface.

v0.5.1a3 adds an automatic retry: spawn a sibling planner with a
JSON-envelope reminder prefixed to its goal. Capped at
max_planner_retries (default 1) so we don't loop forever on a
fundamentally-broken planner. Works for both PLAN and PLAN_DEEP.
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from resonant_client.orchestration.plan_graph import (
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    new_node_id,
)
from resonant_client.orchestration.walker import (
    GraphWalker,
    SpecialistResult,
    WalkerEvent,
    _PLANNER_RETRY_PREFIX,
    _PLANNER_SPECS,
    _strip_retry_prefix,
)


# ── Helpers ────────────────────────────────────────────────────────────


def _make_planner_graph(spec=NodeSpecialization.PLAN) -> tuple[PlanGraph, PlanNode]:
    """Fresh graph with one planner root node."""
    graph = PlanGraph.new("test intent")
    root = PlanNode(
        id=new_node_id(),
        intent_id=graph.intent_id,
        goal="Build a thing.",
        specialization=spec,
    )
    graph.add_node(root)
    return graph, root


def _no_subgoals_runner(node, graph) -> SpecialistResult:
    """Stub runner that always returns DONE with NO subgoals — the
    failure mode being tested."""
    return SpecialistResult(
        status=NodeStatus.DONE,
        confidence=0.5,
        summary="<tool_call name=\"glob\">...</tool_call>",
        subgoals=[],
    )


def _good_subgoals_runner(node, graph) -> SpecialistResult:
    """Stub runner that returns DONE with valid subgoals."""
    return SpecialistResult(
        status=NodeStatus.DONE,
        confidence=0.9,
        summary='```json\n{"subgoals":[{"goal":"do it","specialization":"implement"}]}\n```',
        subgoals=[{"goal": "do it", "specialization": "implement"}],
    )


def _alternating_runner_factory(outputs: list[SpecialistResult]):
    """Returns a runner that yields each canned SpecialistResult in
    turn. Consumed in order; raises if exhausted."""
    iterator = iter(outputs)

    def _runner(node, graph):
        return next(iterator)

    return _runner


# ── _strip_retry_prefix + _PLANNER_SPECS sanity ──────────────────────


class TestPlannerHelpers:
    def test_planner_specs_includes_both_planner_variants(self):
        assert NodeSpecialization.PLAN in _PLANNER_SPECS
        assert NodeSpecialization.PLAN_DEEP in _PLANNER_SPECS

    def test_planner_specs_excludes_non_planners(self):
        assert NodeSpecialization.IMPLEMENT not in _PLANNER_SPECS
        assert NodeSpecialization.VERIFY not in _PLANNER_SPECS
        assert NodeSpecialization.REFLECT not in _PLANNER_SPECS

    def test_strip_retry_prefix_removes_prefix(self):
        wrapped = _PLANNER_RETRY_PREFIX + "Original goal."
        assert _strip_retry_prefix(wrapped) == "Original goal."

    def test_strip_retry_prefix_passthrough_when_absent(self):
        assert _strip_retry_prefix("Original goal.") == "Original goal."

    def test_strip_retry_prefix_handles_empty(self):
        assert _strip_retry_prefix("") == ""


# ── Walker retry behavior ─────────────────────────────────────────────


class TestWalkerPlannerRetry:
    def test_no_retry_when_subgoals_emitted(self):
        """Sanity: a working planner produces subgoals on first try
        and the walker does NOT spawn a retry sibling."""
        graph, root = _make_planner_graph()
        walker = GraphWalker(
            runner=_alternating_runner_factory([
                _good_subgoals_runner(root, graph),
                # Implementer for the spawned subgoal
                SpecialistResult(status=NodeStatus.DONE, confidence=0.9, summary="ok"),
            ]),
        )
        walker.run(graph)

        # Original planner + 1 spawned implementer = 2 total nodes,
        # NO retry sibling.
        planners = [n for n in graph.nodes.values() if n.specialization in _PLANNER_SPECS]
        assert len(planners) == 1, f"unexpected retry planner: {planners}"

    def test_no_subgoals_triggers_one_retry(self):
        graph, root = _make_planner_graph()
        # First call: no subgoals (failure). Second call: still no
        # subgoals (we exhaust retries → walker gives up).
        walker = GraphWalker(
            runner=_alternating_runner_factory([
                _no_subgoals_runner(root, graph),
                _no_subgoals_runner(root, graph),
            ]),
            max_planner_retries=1,
        )
        walker.run(graph)

        planners = [n for n in graph.nodes.values() if n.specialization in _PLANNER_SPECS]
        # Original + 1 retry sibling = 2
        assert len(planners) == 2

        # The retry sibling has the prefix
        retries = [n for n in planners if n.id != root.id]
        assert len(retries) == 1
        assert retries[0].goal.startswith(_PLANNER_RETRY_PREFIX)

    def test_retry_recovers_when_second_attempt_succeeds(self):
        """If the first planner fails but the second emits subgoals,
        the walker proceeds normally — no spurious double-retry."""
        graph, root = _make_planner_graph()
        walker = GraphWalker(
            runner=_alternating_runner_factory([
                _no_subgoals_runner(root, graph),  # original fails
                _good_subgoals_runner(root, graph),  # retry succeeds
                # Implementer for the spawned subgoal
                SpecialistResult(status=NodeStatus.DONE, confidence=0.9, summary="ok"),
            ]),
            max_planner_retries=1,
        )
        walker.run(graph)

        planners = [n for n in graph.nodes.values() if n.specialization in _PLANNER_SPECS]
        # 1 original + 1 retry = 2
        assert len(planners) == 2
        # The retry's child implementer was added (proving expansion happened)
        implementers = [n for n in graph.nodes.values()
                        if n.specialization == NodeSpecialization.IMPLEMENT]
        assert len(implementers) == 1

    def test_retry_count_capped_at_max(self):
        """If max_planner_retries=1, we get ONE retry sibling — even
        if it ALSO fails. No retry-of-retry-of-retry."""
        graph, root = _make_planner_graph()
        # Three failing attempts in the queue, but only 2 should run
        # (original + 1 retry).
        walker = GraphWalker(
            runner=_alternating_runner_factory([
                _no_subgoals_runner(root, graph),
                _no_subgoals_runner(root, graph),
                _no_subgoals_runner(root, graph),  # never reached
            ]),
            max_planner_retries=1,
        )
        walker.run(graph)

        planners = [n for n in graph.nodes.values() if n.specialization in _PLANNER_SPECS]
        assert len(planners) == 2  # original + 1 retry, not 3

    def test_zero_retries_disables_retry_path(self):
        """max_planner_retries=0 disables the auto-retry behavior
        (back to v0.5.0 behavior). Useful for tests / users who
        explicitly opt out."""
        graph, root = _make_planner_graph()
        walker = GraphWalker(
            runner=_no_subgoals_runner,
            max_planner_retries=0,
        )
        walker.run(graph)

        planners = [n for n in graph.nodes.values() if n.specialization in _PLANNER_SPECS]
        assert len(planners) == 1  # no retry spawned

    def test_retry_works_for_plan_deep(self):
        """PLAN_DEEP gets the same retry treatment as PLAN."""
        graph, root = _make_planner_graph(spec=NodeSpecialization.PLAN_DEEP)
        walker = GraphWalker(
            runner=_alternating_runner_factory([
                _no_subgoals_runner(root, graph),
                _no_subgoals_runner(root, graph),
            ]),
            max_planner_retries=1,
        )
        walker.run(graph)

        planners = [n for n in graph.nodes.values() if n.specialization in _PLANNER_SPECS]
        assert len(planners) == 2
        retries = [n for n in planners if n.id != root.id]
        # Retry inherits the original's specialization
        assert retries[0].specialization == NodeSpecialization.PLAN_DEEP

    def test_retry_emits_plan_rewrite_event(self):
        """Walker should emit `plan.rewrite` with `reason` mentioning
        the retry so the GUI / audit log can surface it."""
        graph, root = _make_planner_graph()
        events: list[WalkerEvent] = []

        walker = GraphWalker(
            runner=_alternating_runner_factory([
                _no_subgoals_runner(root, graph),
                _no_subgoals_runner(root, graph),
            ]),
            on_event=events.append,
            max_planner_retries=1,
        )
        walker.run(graph)

        rewrites = [e for e in events if e.kind == "plan.rewrite"]
        retry_rewrites = [
            e for e in rewrites
            if "retry" in e.payload.get("reason", "").lower()
        ]
        assert len(retry_rewrites) == 1
        assert "JSON" in retry_rewrites[0].payload["reason"] or \
               "subgoals" in retry_rewrites[0].payload["reason"]

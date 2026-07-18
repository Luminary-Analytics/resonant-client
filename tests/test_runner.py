"""Tests for LocalSpecialistRunner — Session adapter for plan-graph nodes."""

from __future__ import annotations

import threading
import os
from unittest.mock import MagicMock, patch

from resonant_client.orchestration import (
    LocalSpecialistRunner,
    NodeSpecialization,
    NodeStatus,
    PlanGraph,
    PlanNode,
    new_node_id,
)


# ── Helpers ─────────────────────────────────────────────────────────────


def _node(graph, *, goal, spec, parent=None, deps=None):
    n = PlanNode(
        id=new_node_id(), intent_id=graph.intent_id,
        goal=goal, specialization=spec,
        parent_id=parent, depends_on=list(deps or []),
    )
    graph.add_node(n)
    return n


def _make_runner(events_to_yield=None, **overrides):
    """Build a runner with a Session that yields scripted events."""
    if events_to_yield is None:
        events_to_yield = [
            {"event": "session.start"},
            {"event": "text.delta", "delta": "ok done"},
            {"event": "text.done", "text": "ok done"},
            {"event": "session.end"},
        ]

    backend = MagicMock()
    fake_tools = [{"function": {"name": n}} for n in (
        "file_read", "glob", "grep",
        "file_write", "file_edit", "bash",
        "mcp_browseros_navigate_page", "mcp_browseros_click",
    )]

    runner = LocalSpecialistRunner(
        backend=backend,
        project_path="/tmp/proj",
        all_tools=fake_tools,
        project_instructions="# Project\nUse Tailwind.",
        settings=None,
        cancel_event=overrides.get("cancel_event"),
        on_session_event=overrides.get("on_session_event") or (lambda ev: None),
    )

    # Patch Session so we don't hit a real backend
    def fake_run(self, user_msg, on_permission=None, on_choice=None, images=None):
        for ev in events_to_yield:
            yield ev
    return runner, fake_run


# ── Outcome → confidence mapping ───────────────────────────────────────


def test_clean_run_yields_confidence_one():
    g = PlanGraph.new("intent")
    node = _node(g, goal="read README", spec=NodeSpecialization.EXPLORE)
    runner, fake_run = _make_runner()
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.status == NodeStatus.DONE
    assert result.confidence == 1.0


def test_step_limit_with_output_lowers_confidence_softly():
    """Hit step limit but DID produce output → soft penalty (0.7), not 0.3/0.4."""
    g = PlanGraph.new("intent")
    node = _node(g, goal="big task", spec=NodeSpecialization.IMPLEMENT)
    events = [
        {"event": "session.start"},
        {"event": "text.delta", "delta": "thinking..."},
        {"event": "text.done", "text": "thinking..."},
        {"event": "session.end", "reason": "max_steps"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.confidence == 0.7, "produced output → softer penalty"
    assert result.data.get("hit_step_limit") is True


def test_step_limit_with_no_output_penalised_harder():
    """Hit step limit AND produced nothing → 0.3 (worse than soft penalty)."""
    g = PlanGraph.new("intent")
    node = _node(g, goal="dead loop", spec=NodeSpecialization.IMPLEMENT)
    events = [
        {"event": "session.start"},
        {"event": "session.end", "reason": "max_steps"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.confidence == 0.3
    assert result.data.get("hit_step_limit") is True


def test_tool_errors_lower_confidence():
    g = PlanGraph.new("intent")
    node = _node(g, goal="task", spec=NodeSpecialization.IMPLEMENT)
    events = [
        {"event": "session.start"},
        {"event": "tool.call", "tool_name": "bash", "args": {"command": "x"}},
        {"event": "tool.result", "is_error": True},
        {"event": "tool.call", "tool_name": "bash", "args": {"command": "y"}},
        {"event": "tool.result", "is_error": True},
        {"event": "text.done", "text": "recovered"},
        {"event": "session.end"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert 0.5 <= result.confidence <= 0.7  # 2 errors → 0.7 per the table


def test_step_limit_via_error_event_treated_as_done_not_blocked():
    """Session emits `error` with 'step limit' message when it hits its budget.

    Before the fix, the runner mapped that to BLOCKED + crashed=True. The right
    behavior is DONE + low confidence — the specialist ran out of room but
    didn't crash, downstream work should still proceed.
    """
    g = PlanGraph.new("intent")
    node = _node(g, goal="big task", spec=NodeSpecialization.EXPLORE)
    events = [
        {"event": "session.start"},
        {"event": "step.start"},
        {"event": "tool.call", "tool_name": "glob"},
        {"event": "step.end"},
        {"event": "error", "message": "Reached 8 step limit \u2014 use /clear to reset"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.status == NodeStatus.DONE, "step limit should not BLOCK the node"
    # The fake events include a tool.call → produced_output=True → soft 0.7 penalty
    assert result.confidence == 0.7
    assert result.data.get("hit_step_limit") is True


def test_allowlist_denials_dont_inflate_error_count():
    """A specialist that bumps into the allowlist gets denials, not errors —
    confidence should stay clean for the work that *did* succeed."""
    g = PlanGraph.new("intent")
    node = _node(g, goal="x", spec=NodeSpecialization.PLAN)
    events = [
        {"event": "session.start"},
        # Three tool denials — would have tanked confidence before the fix
        {"event": "tool.call", "tool_name": "file_write"},
        {"event": "tool.result", "is_error": True, "denied": True, "output": "Tool 'file_write' is not in this session's allowlist..."},
        {"event": "tool.call", "tool_name": "file_edit"},
        {"event": "tool.result", "is_error": True, "denied": True, "output": "Tool 'file_edit' is not in this session's allowlist..."},
        {"event": "tool.call", "tool_name": "bash"},
        {"event": "tool.result", "is_error": True, "denied": True, "output": "Tool 'bash' is not in this session's allowlist..."},
        # Then a clean planner output
        {"event": "text.done", "text": '```json\n{"subgoals":[{"goal":"x","specialization":"implement"}]}\n```'},
        {"event": "session.end"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.confidence == 1.0, "denials shouldn't tank confidence"
    assert result.subgoals  # parsed cleanly


def test_planner_repairs_malformed_envelope_with_constrained_output():
    g = PlanGraph.new("intent")
    node = _node(g, goal="plan it", spec=NodeSpecialization.PLAN)
    events = [
        {"event": "text.done", "text": "I would split this into implementation work."},
        {"event": "session.end"},
    ]
    runner, fake_run = _make_runner(events)
    runner.backend.generate_structured.return_value = {
        "subgoals": [{
            "goal": "implement it",
            "specialization": "implement",
            "depends_on": [],
        }],
    }

    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)

    assert result.subgoals[0]["goal"] == "implement it"
    assert result.confidence == 1.0
    assert result.data["structured_output_repaired"] is True


def test_real_error_still_marks_blocked():
    """Non-step-limit error events keep the BLOCKED behavior — those are real crashes."""
    g = PlanGraph.new("intent")
    node = _node(g, goal="x", spec=NodeSpecialization.IMPLEMENT)
    events = [
        {"event": "session.start"},
        {"event": "error", "message": "Backend API returned 500"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.status == NodeStatus.BLOCKED


def test_session_crash_marks_blocked():
    g = PlanGraph.new("intent")
    node = _node(g, goal="x", spec=NodeSpecialization.IMPLEMENT)
    events = [
        {"event": "session.start"},
        {"event": "error", "message": "exploded"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.status == NodeStatus.BLOCKED
    assert result.confidence == 0.0


def test_runner_exception_translates_to_blocked():
    g = PlanGraph.new("intent")
    node = _node(g, goal="x", spec=NodeSpecialization.IMPLEMENT)
    runner, _ = _make_runner()

    def crashing_run(self, user_msg, **kwargs):
        raise RuntimeError("simulated crash")
        yield  # so it's still a generator

    with patch("resonant_client.orchestration.runner.Session.run", crashing_run):
        result = runner(node, g)
    assert result.status == NodeStatus.BLOCKED
    assert "simulated" in result.summary.lower() or "exception" in result.summary.lower()


def test_cancel_before_run_returns_abandoned():
    g = PlanGraph.new("intent")
    node = _node(g, goal="x", spec=NodeSpecialization.IMPLEMENT)
    cancel = threading.Event()
    cancel.set()
    runner, fake_run = _make_runner(cancel_event=cancel)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.status == NodeStatus.ABANDONED
    assert result.confidence == 0.0


# ── Plan specialist → subgoals parsing ─────────────────────────────────


PLAN_PROMPT = '''Here is the plan.
```json
{
  "subgoals": [
    {"goal": "research prefers-color-scheme", "specialization": "research"},
    {"goal": "add CSS vars", "specialization": "implement", "depends_on": [0]},
    {"goal": "verify in browser", "specialization": "verify", "depends_on": [1]}
  ]
}
```
Done.'''


def test_plan_specialist_parses_subgoals():
    g = PlanGraph.new("ship dark mode")
    node = _node(g, goal="decompose", spec=NodeSpecialization.PLAN)
    events = [
        {"event": "text.delta", "delta": PLAN_PROMPT},
        {"event": "text.done", "text": PLAN_PROMPT},
        {"event": "session.end"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert len(result.subgoals) == 3
    goals = [sg["goal"] for sg in result.subgoals]
    assert goals == ["research prefers-color-scheme", "add CSS vars", "verify in browser"]
    # depends_on indices preserved
    assert result.subgoals[1]["depends_on"] == [0]


def test_plan_specialist_parse_failure_tempers_confidence():
    g = PlanGraph.new("intent")
    node = _node(g, goal="decompose", spec=NodeSpecialization.PLAN)
    events = [
        {"event": "text.done", "text": "I tried but couldn't decompose."},
        {"event": "session.end"},
    ]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.subgoals == []
    assert result.confidence <= 0.5  # soft ceiling — work happened, just couldn't parse


def test_plan_specialist_takes_last_fenced_block():
    """If the model emits an example fence first then the real one, last wins."""
    g = PlanGraph.new("intent")
    node = _node(g, goal="decompose", spec=NodeSpecialization.PLAN)
    text = '''Here's a similar example:
```json
{"subgoals": [{"goal": "old example", "specialization": "explore"}]}
```
But for our case:
```json
{"subgoals": [{"goal": "real one", "specialization": "implement"}]}
```'''
    events = [{"event": "text.done", "text": text}, {"event": "session.end"}]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert len(result.subgoals) == 1
    assert result.subgoals[0]["goal"] == "real one"


# ── Verify specialist → verdict parsing ────────────────────────────────


def test_verify_specialist_parses_pass_verdict():
    g = PlanGraph.new("intent")
    node = _node(g, goal="check it", spec=NodeSpecialization.VERIFY)
    text = '''All tests pass.
```json
{"verdict": "pass", "findings": []}
```'''
    events = [{"event": "text.done", "text": text}, {"event": "session.end"}]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.verdict == "pass"
    assert result.findings == []
    assert result.status == NodeStatus.DONE


def test_verify_specialist_parses_revise_with_findings():
    g = PlanGraph.new("intent")
    node = _node(g, goal="check it", spec=NodeSpecialization.VERIFY)
    text = '''```json
{"verdict": "revise", "findings": ["bug A", "bug B"]}
```'''
    events = [{"event": "text.done", "text": text}, {"event": "session.end"}]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.verdict == "revise"
    assert result.findings == ["bug A", "bug B"]


def test_verify_specialist_falls_back_to_prose_for_pass():
    g = PlanGraph.new("intent")
    node = _node(g, goal="check it", spec=NodeSpecialization.VERIFY)
    text = "I ran the tests. Verdict: pass — nothing else to do."
    events = [{"event": "text.done", "text": text}, {"event": "session.end"}]
    runner, fake_run = _make_runner(events)
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        result = runner(node, g)
    assert result.verdict == "pass"


# ── Tool allowlist propagation ─────────────────────────────────────────


def test_runner_passes_filtered_tools_to_session():
    """Explore specialist should only see read-only tools."""
    g = PlanGraph.new("intent")
    node = _node(g, goal="read", spec=NodeSpecialization.EXPLORE)

    captured: dict = {}

    def fake_init(self, backend, **kwargs):
        captured["allowed_tools"] = kwargs.get("allowed_tools")
        # Skip real init machinery
        self.backend = backend
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.conversation_history = []
        self._cancel_event = threading.Event()
        self.project_path = None
        self._allowed_tools = kwargs.get("allowed_tools")

    def fake_run(self, user_msg, **kwargs):
        yield {"event": "text.done", "text": "ok"}
        yield {"event": "session.end"}

    runner, _ = _make_runner()
    with patch("resonant_client.orchestration.runner.Session.__init__", fake_init), \
         patch("resonant_client.orchestration.runner.Session.run", fake_run):
        runner(node, g)

    allowed = captured["allowed_tools"]
    assert allowed is not None
    names = {t["function"]["name"] for t in allowed}
    # Explore profile is read-only — no shell/edits
    assert "file_write" not in names
    assert "file_edit" not in names
    assert "bash" not in names
    assert "file_read" in names


def test_runner_attaches_workspace_sandbox_to_specialist_session():
    """Full-auto specialists must still be confined to the intent project."""
    g = PlanGraph.new("intent")
    node = _node(g, goal="implement", spec=NodeSpecialization.IMPLEMENT)
    captured = {}

    def fake_run(self, user_msg, **kwargs):
        captured["sandbox"] = self.sandbox
        captured["policy"] = self.execution_policy
        yield {"event": "text.done", "text": "ok"}
        yield {"event": "session.end"}

    runner, _ = _make_runner()
    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        runner(node, g)

    assert captured["sandbox"].enabled is True
    assert os.path.basename(captured["sandbox"].project_path) == "proj"
    assert captured["policy"] is not None


# ── Dependency context propagation ─────────────────────────────────────


def test_dep_summaries_passed_as_context():
    """A node depending on a completed parent should have its goal+summary visible in the system prompt."""
    g = PlanGraph.new("intent")
    parent = _node(g, goal="research X", spec=NodeSpecialization.RESEARCH)
    child = _node(g, goal="implement X based on research", spec=NodeSpecialization.IMPLEMENT, deps=[parent.id])
    g.mark_done(parent.id, result={"summary": "X uses prefers-color-scheme media query"}, confidence=1.0)

    captured: dict = {}

    def fake_init(self, backend, **kwargs):
        captured["role_instructions"] = kwargs.get("role_instructions")
        captured["prompt_role"] = kwargs.get("prompt_role")
        self.backend = backend
        self._cancel_event = threading.Event()
        self.project_path = None

    def fake_run(self, user_msg, **kwargs):
        yield {"event": "text.done", "text": "ok"}
        yield {"event": "session.end"}

    runner, _ = _make_runner()
    with patch("resonant_client.orchestration.runner.Session.__init__", fake_init), \
         patch("resonant_client.orchestration.runner.Session.run", fake_run):
        runner(child, g)

    sys_prompt = captured["role_instructions"]
    assert captured["prompt_role"] == "specialist"
    assert "research X" in sys_prompt
    assert "prefers-color-scheme" in sys_prompt


# ── Audit logger pass-through ─────────────────────────────────────────


def test_audit_logger_called_on_each_tool_call():
    g = PlanGraph.new("intent")
    node = _node(g, goal="x", spec=NodeSpecialization.IMPLEMENT)
    events = [
        {"event": "tool.call", "tool_name": "bash", "args": {"command": "pwd"}},
        {"event": "tool.result", "output": "/x"},
        {"event": "tool.call", "tool_name": "file_read", "args": {"path": "x"}},
        {"event": "tool.result", "output": "..."},
        {"event": "session.end"},
    ]
    captured: list = []
    runner, fake_run = _make_runner(events)
    runner.audit_logger = lambda **kw: captured.append(kw)

    with patch("resonant_client.orchestration.runner.Session.run", fake_run):
        runner(node, g)

    assert len(captured) == 2
    assert captured[0]["tool_name"] == "bash"
    assert captured[1]["tool_name"] == "file_read"
    assert all(c["node_id"] == node.id for c in captured)

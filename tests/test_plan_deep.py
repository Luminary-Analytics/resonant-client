"""Tests for v0.5.1a2 PLAN_DEEP specialist + v0.5.4a1 unconditional
default.

PLAN_DEEP is the research-first planner introduced to make
deepseek-v4-pro:cloud usable for autonomous missions. The v0.5.0
GA smoke showed pro consistently fails the strict "emit JSON
immediately" contract of PLAN — pro wants to read the codebase
first, and the strict prompt treated that exploration as malformed
output. PLAN_DEEP makes the exploration explicit, with the JSON
envelope as the required FINAL phase.

v0.5.4a1: the per-tier `PLANNER_BY_TIER` routing was removed.
Autonomous missions now use PLAN_DEEP unconditionally (it's a
strict superset of PLAN, so flash works fine under it too, and
adding new models no longer requires editing a routing dict).

These tests pin:
- PLAN_DEEP is registered in the specialist registry with the
  right tool allowlist + step budget
- The prompt explicitly invites exploration BEFORE the JSON
  envelope (matching pro's instinctive flow)
- The prompt still REQUIRES the JSON envelope as final output
- The autonomous-session module exposes `_DEFAULT_PLANNER_SPEC`
  pinned to PLAN_DEEP
- IntentService.start_intent accepts the planner override
"""
from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from resonant_client.gui.autonomous_session import _DEFAULT_PLANNER_SPEC
from resonant_client.orchestration.plan_graph import NodeSpecialization
from resonant_client.orchestration.specialists import (
    SPECIALISTS,
    SpecialistProfile,
    get_specialist,
)


# ── Specialist registration ────────────────────────────────────────────


class TestPlanDeepRegistration:
    def test_plan_deep_in_node_specialization_enum(self):
        assert NodeSpecialization.PLAN_DEEP == "plan_deep"
        assert "plan_deep" in NodeSpecialization.ALL
        # Old PLAN still there
        assert NodeSpecialization.PLAN == "plan"
        assert "plan" in NodeSpecialization.ALL

    def test_plan_deep_is_registered(self):
        assert NodeSpecialization.PLAN_DEEP in SPECIALISTS
        profile = get_specialist(NodeSpecialization.PLAN_DEEP)
        assert isinstance(profile, SpecialistProfile)
        assert profile.name == "plan_deep"

    def test_same_tool_allowlist_as_plan(self):
        # PLAN_DEEP doesn't get any extra tools — it's the SAME
        # set as PLAN (read-only + await_user). The difference is
        # ONLY the prompt + step budget.
        plan = get_specialist(NodeSpecialization.PLAN)
        deep = get_specialist(NodeSpecialization.PLAN_DEEP)
        assert plan.tool_allowlist == deep.tool_allowlist

    def test_larger_step_budget(self):
        # Deep planner gets more headroom for exploration. PLAN
        # is 8; PLAN_DEEP should be larger (16 in the v0.5.1a2
        # implementation).
        plan = get_specialist(NodeSpecialization.PLAN)
        deep = get_specialist(NodeSpecialization.PLAN_DEEP)
        assert deep.max_steps > plan.max_steps
        assert deep.max_steps >= 12  # generous headroom

    def test_no_edit_or_exec_tools(self):
        # The user could mistakenly grant pro file_edit or bash;
        # explicitly pin that the deep planner stays read-only.
        deep = get_specialist(NodeSpecialization.PLAN_DEEP)
        assert "file_edit" not in deep.tool_allowlist
        assert "file_write" not in deep.tool_allowlist
        assert "bash" not in deep.tool_allowlist


class TestPlanDeepPromptInvariants:
    """Pin the prompt's behavioral promises without locking exact
    wording. Future prompt edits stay safe as long as the key
    invariants survive."""

    @pytest.fixture
    def prompt(self) -> str:
        return get_specialist(NodeSpecialization.PLAN_DEEP).system_block

    def test_two_phase_structure_explicit(self, prompt: str):
        # The prompt must clearly mark the two phases — exploration
        # then plan. Without this structure, pro reverts to its
        # tool-call-as-text failure mode.
        lower = prompt.lower()
        assert "phase 1" in lower or "explore" in lower
        assert "phase 2" in lower or "plan" in lower

    def test_explicitly_invites_exploration(self, prompt: str):
        # The whole point — pro must be told it's OK (encouraged
        # even) to read files first.
        lower = prompt.lower()
        for term in ("file_read", "glob", "grep", "explore"):
            assert term in lower, f"missing exploration term: {term}"

    def test_json_envelope_still_required(self, prompt: str):
        # Even though we invite exploration, the JSON envelope is
        # the contract. Without this, the walker can't parse a plan.
        assert '"subgoals"' in prompt
        assert "```json" in prompt
        assert "REQUIRED" in prompt or "required" in prompt or "MUST" in prompt

    def test_explicitly_forbids_tool_call_as_output(self, prompt: str):
        # The exact failure mode pro hit in v0.5.0 GA smoke. The
        # prompt should call this out so pro doesn't fall back
        # into it.
        lower = prompt.lower()
        assert "tool_call" in lower or "<tool" in prompt

    def test_says_no_bash_or_file_edit(self, prompt: str):
        # Reinforcing the spec at prompt-level so pro doesn't try
        # to execute work itself.
        lower = prompt.lower()
        for term in ("bash", "file_edit", "file_write"):
            assert term in lower, f"missing forbidden-tool mention: {term}"

    def test_format_reminder_present(self, prompt: str):
        # DeepSeek tuning: prompt must end with a FORMAT REMINDER
        # block per the v0.4.8 (T2.3) pattern. JSON envelope at end.
        assert "FORMAT REMINDER" in prompt or "JSON" in prompt
        # Strict JSON mentioned
        lower = prompt.lower()
        assert "trailing comma" in lower or "single quote" in lower or "strict" in lower


# ── v0.5.4a1: unconditional PLAN_DEEP default ──────────────────────────


class TestDefaultPlannerSpec:
    """v0.5.4a1 removed the PLANNER_BY_TIER routing dict. Pin the
    new default so a casual change can't silently regress to PLAN
    for autonomous missions."""

    def test_default_planner_spec_is_plan_deep(self):
        # The constant other modules import to wire the planner.
        assert _DEFAULT_PLANNER_SPEC == NodeSpecialization.PLAN_DEEP

    def test_default_planner_spec_is_a_real_specialization(self):
        assert _DEFAULT_PLANNER_SPEC in NodeSpecialization.ALL

    def test_planner_by_tier_no_longer_exported(self):
        # If anyone re-adds the routing under the old name, this fails.
        # Keeping the test prevents accidental resurrection — search
        # for the symbol on import.
        import resonant_client.gui.autonomous_session as mod
        assert not hasattr(mod, "PLANNER_BY_TIER")
        assert not hasattr(mod, "planner_for_model")


# ── IntentService.start_intent override plumbing ───────────────────────


class TestIntentServicePlannerOverride:
    """IntentService.start_intent now takes a `planner_specialization`
    keyword that controls which planner the root node uses. We don't
    spin up a real backend here — just exercise the construction
    path + verify the root node's spec is what we asked for."""

    def _build_service(self):
        from resonant_client.orchestration.intent_service import IntentService
        # Stub backend — not used for this construction test
        backend = MagicMock()
        backend.model = "deepseek-v4-pro:cloud"
        return IntentService(
            project_path="/tmp/fake",
            backend=backend,
            all_tools=[],
            project_instructions="",
            settings=None,
            on_event=None,
        )

    def test_default_planner_is_plan(self, tmp_path, monkeypatch):
        # Patch save_graph + log_decision so we don't write anywhere,
        # and patch threading.Thread.start so the worker doesn't run.
        import resonant_client.orchestration.intent_service as mod

        monkeypatch.setattr(mod, "save_graph", lambda *a, **k: None)
        monkeypatch.setattr(mod, "log_decision", lambda *a, **k: None)
        monkeypatch.setattr("threading.Thread.start", lambda self: None)

        svc = self._build_service()
        svc.project_path = str(tmp_path)

        intent_id = svc.start_intent("test goal")
        graph = svc.get_graph(intent_id)
        assert graph is not None
        roots = [n for n in graph.nodes.values() if n.parent_id is None]
        assert len(roots) == 1
        assert roots[0].specialization == NodeSpecialization.PLAN

    def test_planner_override_routes_root_to_specified_spec(
        self, tmp_path, monkeypatch
    ):
        import resonant_client.orchestration.intent_service as mod

        monkeypatch.setattr(mod, "save_graph", lambda *a, **k: None)
        monkeypatch.setattr(mod, "log_decision", lambda *a, **k: None)
        monkeypatch.setattr("threading.Thread.start", lambda self: None)

        svc = self._build_service()
        svc.project_path = str(tmp_path)

        intent_id = svc.start_intent(
            "test goal",
            planner_specialization=NodeSpecialization.PLAN_DEEP,
        )
        graph = svc.get_graph(intent_id)
        roots = [n for n in graph.nodes.values() if n.parent_id is None]
        assert roots[0].specialization == NodeSpecialization.PLAN_DEEP

    def test_unknown_planner_raises_value_error(
        self, tmp_path, monkeypatch
    ):
        import resonant_client.orchestration.intent_service as mod

        monkeypatch.setattr(mod, "save_graph", lambda *a, **k: None)
        monkeypatch.setattr(mod, "log_decision", lambda *a, **k: None)

        svc = self._build_service()
        svc.project_path = str(tmp_path)

        with pytest.raises(ValueError, match="unknown planner"):
            svc.start_intent("test", planner_specialization="not-a-spec")

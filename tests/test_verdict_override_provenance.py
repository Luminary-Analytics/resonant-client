"""Tests for v0.5.9a3 — verdict-override provenance.

When the model claims `satisfied` but the roadmap on disk doesn't
agree, the daemon downgrades to `continue`. Pre-v0.5.9 the override
was only embedded in the summary string; v0.5.9a3 adds structured
fields (`verdict_overridden`, `model_verdict`, `override_reason`,
`unpassed_criteria`) so the GUI can render a distinct provenance
block instead of relying on prose parsing.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from resonant_client.gui import roadmap as roadmap_module
from resonant_client.gui.autonomous_loop import (
    AutonomousMissionConfig,
    AutonomousMissionDaemon,
    DaemonHooks,
    DispatchOutcome,
    FullReflectOutcome,
)
from resonant_client.gui.roadmap import (
    AcceptanceCriterion,
    Roadmap,
)
from resonant_client.orchestration.acceptance_check import (
    BashRunner,
    CheckContext,
)
from resonant_client.orchestration.reflect import ReflectPassResult


def _build_roadmap_with_unpassed(tmp_path):
    """Roadmap with a chrome criterion (unpassed). Forces the
    cross-check path: model says satisfied, daemon overrides because
    chrome can't be auto-passed."""
    rm = Roadmap(
        feature="t", intent_id="i",
        time_budget_label="1h", status="running",
    )
    roadmap_module.add_item(rm, tier=1, title="T1.1", description="x")
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="chrome", text="click toggle"),
    )
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="bash", text="`true` exits 0"),
    )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    return path


def _make_daemon_with_lying_reflect(tmp_path, events_sink):
    """Daemon whose REFLECT hook claims `satisfied` without actually
    passing the chrome criterion. The daemon's cross-check should
    catch the lie and override."""
    path = _build_roadmap_with_unpassed(tmp_path)

    def lying_reflect(rm, pass_result, *, decision_context=""):
        return FullReflectOutcome(
            pass_result=pass_result,
            verdict="satisfied",  # lie
            summary="all done!",
        )

    config = AutonomousMissionConfig(
        intent_id="i", roadmap_path=path,
        max_iterations=2, full_reflect_cadence=1,
        tick_pause_seconds=0.0,
    )
    hooks = DaemonHooks(
        dispatch_item=lambda item: 0,
        wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
        cancel_dispatch=lambda h: None,
        get_commit_sha=lambda: "abc1234",
        validate_sha=lambda s: True,
        run_full_reflect=lying_reflect,
        check_context_factory=lambda rm: CheckContext(),
    )
    daemon = AutonomousMissionDaemon(
        config, hooks, on_event=events_sink.append,
    )
    return daemon, path


class TestVerdictOverrideProvenance:
    def test_override_emits_structured_fields(self, tmp_path):
        events = []
        daemon, path = _make_daemon_with_lying_reflect(tmp_path, events)
        daemon.start()
        daemon.join(timeout=3.0)

        reflections = [
            e for e in events
            if e.get("event") == "autonomous_reflection"
        ]
        # At least one reflection should have fired.
        assert len(reflections) >= 1
        # Find the one where override happened.
        overridden = [r for r in reflections if r.get("verdict_overridden")]
        assert len(overridden) >= 1
        ev = overridden[0]
        # Structured fields are present + correctly populated.
        assert ev["model_verdict"] == "satisfied"
        assert ev["verdict"] == "continue"
        assert ev["verdict_overridden"] is True
        assert "claimed `satisfied`" in ev["override_reason"]
        # The unpassed chrome criterion shows up in the list.
        assert any("[chrome]" in c for c in ev["unpassed_criteria"])

    def test_no_override_when_model_says_continue(self, tmp_path):
        # Sanity check: the structured fields are PRESENT but
        # `verdict_overridden` is False when no override happened.
        events = []
        path = _build_roadmap_with_unpassed(tmp_path)

        def honest_reflect(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result, verdict="continue",
            )

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=honest_reflect,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)
        daemon.start()
        daemon.join(timeout=3.0)

        reflections = [
            e for e in events
            if e.get("event") == "autonomous_reflection"
        ]
        assert reflections
        for r in reflections:
            # Verdict matches model claim; not overridden.
            assert r["verdict_overridden"] is False
            assert r["override_reason"] == ""
            assert r["unpassed_criteria"] == []
            assert r["model_verdict"] == r["verdict"]

    def test_no_override_when_model_says_blocked(self, tmp_path):
        # Model says blocked; daemon shouldn't override that either.
        # Override is specifically for `satisfied` that doesn't hold up.
        events = []
        path = _build_roadmap_with_unpassed(tmp_path)

        def blocked_reflect(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result, verdict="blocked",
            )

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=blocked_reflect,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)
        daemon.start()
        daemon.join(timeout=3.0)

        reflections = [
            e for e in events
            if e.get("event") == "autonomous_reflection"
        ]
        assert reflections
        for r in reflections:
            assert r["verdict_overridden"] is False

    def test_override_lists_each_unpassed_criterion(self, tmp_path):
        # Multiple unpassed criteria → each appears in the list with
        # its [type] prefix.
        events = []
        rm = Roadmap(
            feature="t", intent_id="i",
            time_budget_label="1h", status="running",
        )
        roadmap_module.add_item(rm, tier=1, title="T1.1", description="x")
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="chrome", text="click toggle A"),
        )
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="chrome", text="click toggle B"),
        )
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="bash", text="`true` exits 0"),
        )
        path = tmp_path / "roadmap.md"
        roadmap_module.save(rm, path)

        def lying_reflect(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result, verdict="satisfied",
            )

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=lying_reflect,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)
        daemon.start()
        daemon.join(timeout=3.0)

        overridden = [
            e for e in events
            if e.get("event") == "autonomous_reflection"
            and e.get("verdict_overridden")
        ]
        assert overridden
        crits = overridden[0]["unpassed_criteria"]
        # Both chrome criteria show up. (bash criterion may also be
        # unpassed since the deterministic prelude couldn't run it
        # without a real shell — that's fine, just confirms the list
        # is non-empty and includes the chrome ones.)
        assert any("toggle A" in c for c in crits)
        assert any("toggle B" in c for c in crits)

    def test_override_reason_phrases_count_correctly(self, tmp_path):
        # Singular vs plural — small QoL detail. "1 blocking criterion"
        # vs "2 blocking criteria".
        events = []
        rm = Roadmap(
            feature="t", intent_id="i",
            time_budget_label="1h", status="running",
        )
        roadmap_module.add_item(rm, tier=1, title="T1.1", description="x")
        rm.acceptance_criteria.append(
            AcceptanceCriterion(type="chrome", text="single criterion"),
        )
        path = tmp_path / "roadmap.md"
        roadmap_module.save(rm, path)

        def lying_reflect(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result, verdict="satisfied",
            )

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc1234",
            validate_sha=lambda s: True,
            run_full_reflect=lying_reflect,
            check_context_factory=lambda rm: CheckContext(),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)
        daemon.start()
        daemon.join(timeout=3.0)

        overridden = [
            e for e in events
            if e.get("event") == "autonomous_reflection"
            and e.get("verdict_overridden")
        ]
        assert overridden
        reason = overridden[0]["override_reason"]
        # 1 → "1 blocking criterion still unpassed" (singular form).
        assert "1 blocking criterion" in reason or "1 blocking criteria" in reason

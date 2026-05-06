"""Tests for v0.6.0 GA — skills wired into the autonomous mission daemon.

Verifies the two integration hooks added to DaemonHooks:
- `extract_skill_hook` fires at verdict=satisfied (and ONLY then).
- `queue_curation_hook` fires at terminal-state satisfied transitions.

Both are best-effort: hook exceptions don't break the daemon loop.

Uses the same daemon-with-stub-hooks pattern as the existing
test_autonomous_pause / test_human_decision_park files.
"""
from __future__ import annotations

import threading
import time
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
from resonant_client.orchestration.acceptance_check import CheckContext


def _build_roadmap_with_chrome(tmp_path):
    """Roadmap with a chrome criterion forces the model-session
    branch in _run_full_reflect."""
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


def _build_roadmap_bash_only(tmp_path, n_items=1):
    """Roadmap with only bash criteria — uses the deterministic
    branch (no model session)."""
    rm = Roadmap(
        feature="t", intent_id="i",
        time_budget_label="1h", status="running",
    )
    for i in range(1, n_items + 1):
        roadmap_module.add_item(rm, tier=1, title=f"T1.{i}", description="x")
    rm.acceptance_criteria.append(
        AcceptanceCriterion(type="bash", text="`true` exits 0"),
    )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)
    return path


# ── extract_skill_hook ────────────────────────────────────────────────


class TestExtractSkillHookFires:
    def test_fires_on_verdict_satisfied(self, tmp_path):
        # Use a bash-only roadmap whose criterion will pass via the
        # deterministic prelude. That makes is_converged()=True so the
        # daemon doesn't override "satisfied" → extract fires.
        path = _build_roadmap_bash_only(tmp_path)
        events = []
        extract_calls = []

        def reflect_hook(rm, pass_result, *, decision_context=""):
            # Not actually called for bash-only path — the deterministic
            # branch handles it.
            return FullReflectOutcome(
                pass_result=pass_result, verdict="satisfied",
            )

        def extract_hook(**kwargs):
            extract_calls.append(kwargs)

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=reflect_hook,
            check_context_factory=lambda rm: CheckContext(),
            extract_skill_hook=extract_hook,
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=events.append)
        rm = roadmap_module.load(path)
        # Pre-pass the bash criterion so is_converged()=True without
        # actually running bash. The cross-check looks at criterion
        # .passed flags, which are roadmap-side state.
        for c in rm.acceptance_criteria:
            c.passed = True
        # Mark all items as checked so the cross-check has nothing
        # to flag (the RoadmapItem field is `checked`, not `completed`).
        for it in rm.items:
            it.checked = True
        roadmap_module.save(rm, path)

        outcome = daemon._run_full_reflect(rm)
        assert outcome.verdict == "satisfied"

        # Hook fired with the iter context.
        assert len(extract_calls) == 1
        call = extract_calls[0]
        assert call["intent_id"] == "i"
        assert call["outcome_verdict"] == "satisfied"
        assert call["verdict_overridden"] is False

    def test_does_not_fire_on_continue_verdict(self, tmp_path):
        path = _build_roadmap_with_chrome(tmp_path)
        extract_calls = []

        def reflect_hook(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="continue",  # not satisfied
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
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=reflect_hook,
            check_context_factory=lambda rm: CheckContext(),
            extract_skill_hook=lambda **kw: extract_calls.append(kw),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        rm = roadmap_module.load(path)
        daemon._run_full_reflect(rm)

        assert extract_calls == []

    def test_does_not_fire_when_verdict_overridden(self, tmp_path):
        # Model says satisfied but daemon overrides to continue
        # (chrome criterion still unpassed) — extract should NOT fire.
        path = _build_roadmap_with_chrome(tmp_path)
        extract_calls = []

        def reflect_hook(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result,
                verdict="satisfied",  # lies; chrome criterion unpassed
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
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=reflect_hook,
            check_context_factory=lambda rm: CheckContext(),
            extract_skill_hook=lambda **kw: extract_calls.append(kw),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        rm = roadmap_module.load(path)
        daemon._run_full_reflect(rm)

        # Verdict was overridden — extract skipped.
        assert extract_calls == []

    def test_no_hook_means_no_call(self, tmp_path):
        # Defensive: when extract_skill_hook is None, the daemon
        # behaves as before — no attempt to call. Point is that
        # _run_full_reflect doesn't blow up with no hook configured.
        path = _build_roadmap_with_chrome(tmp_path)

        def reflect_hook(rm, pass_result, *, decision_context=""):
            return FullReflectOutcome(
                pass_result=pass_result, verdict="satisfied",
                summary="done",
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
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=reflect_hook,
            check_context_factory=lambda rm: CheckContext(),
            # extract_skill_hook=None (default)
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        rm = roadmap_module.load(path)
        # Should NOT raise. (Verdict may be overridden because chrome
        # criterion can't auto-pass — irrelevant to this test; we
        # care that the no-hook path doesn't blow up.)
        daemon._run_full_reflect(rm)

    def test_hook_exception_swallowed(self, tmp_path):
        # Extract hook raising must NOT propagate up to the daemon.
        # Use bash-only roadmap with pre-passed criteria so the verdict
        # stays satisfied (NOT overridden) and the hook actually runs.
        path = _build_roadmap_bash_only(tmp_path)

        def boom(**kwargs):
            raise RuntimeError("simulated extractor crash")

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=1, full_reflect_cadence=1,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="satisfied",
            ),
            check_context_factory=lambda rm: CheckContext(),
            extract_skill_hook=boom,
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        rm = roadmap_module.load(path)
        for c in rm.acceptance_criteria:
            c.passed = True
        for it in rm.items:
            it.checked = True
        roadmap_module.save(rm, path)
        # Daemon's _run_full_reflect should complete normally even
        # though the hook raised.
        outcome = daemon._run_full_reflect(rm)
        assert outcome.verdict == "satisfied"


# ── queue_curation_hook ──────────────────────────────────────────────


class TestQueueCurationHook:
    def test_fires_on_satisfied_terminal(self, tmp_path):
        path = _build_roadmap_bash_only(tmp_path)
        curate_calls = []

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=2, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
            queue_curation_hook=lambda path: curate_calls.append(path),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        # Drive _emit_stop directly with reason=satisfied.
        daemon._emit_stop("satisfied", "all done")

        assert len(curate_calls) == 1
        # Hook receives the project path (parent of roadmap dir).
        assert isinstance(curate_calls[0], str)

    def test_does_not_fire_on_user_pause(self, tmp_path):
        path = _build_roadmap_bash_only(tmp_path)
        curate_calls = []

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=2, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
            queue_curation_hook=lambda path: curate_calls.append(path),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        daemon._emit_stop("user_pause", "user clicked pause")
        # User pause is not a "satisfied" state; curator should not fire.
        assert curate_calls == []

    def test_does_not_fire_on_blocked(self, tmp_path):
        path = _build_roadmap_bash_only(tmp_path)
        curate_calls = []

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=2, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
            queue_curation_hook=lambda path: curate_calls.append(path),
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        daemon._emit_stop("blocked", "stuck")
        assert curate_calls == []

    def test_hook_exception_swallowed(self, tmp_path):
        # queue_curation_hook raising should NOT block the terminal
        # event sequence — the daemon is shutting down anyway.
        path = _build_roadmap_bash_only(tmp_path)

        def boom(project_path):
            raise RuntimeError("curator queue exploded")

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=2, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
            queue_curation_hook=boom,
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        # Should NOT raise.
        daemon._emit_stop("satisfied", "all done")

    def test_no_hook_is_no_op(self, tmp_path):
        path = _build_roadmap_bash_only(tmp_path)

        config = AutonomousMissionConfig(
            intent_id="i", roadmap_path=path,
            max_iterations=2, full_reflect_cadence=999,
            tick_pause_seconds=0.0,
        )
        hooks = DaemonHooks(
            dispatch_item=lambda item: 0,
            wait_for_dispatch=lambda h: DispatchOutcome(success=True, handle=h),
            cancel_dispatch=lambda h: None,
            get_commit_sha=lambda: "abc",
            validate_sha=lambda s: True,
            run_full_reflect=lambda rm, pr: FullReflectOutcome(
                pass_result=pr, verdict="continue",
            ),
            check_context_factory=lambda rm: CheckContext(),
            # queue_curation_hook=None (default)
        )
        daemon = AutonomousMissionDaemon(config, hooks, on_event=lambda e: None)
        # No hook → no error, no surprise behavior.
        daemon._emit_stop("satisfied", "ok")

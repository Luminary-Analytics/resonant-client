"""The mission's waiting policy.

A mission blocks for two genuinely different reasons and they need opposite
recoveries, so these are two settings under one policy rather than one number:

  * parked on a person  -> proceed with the nominated option; the work is fine
  * a sub-mission grinding -> cancel it; the sub-mission IS the problem

These tests pin that distinction, the budget-derived stall ceiling, and the
shared expiry vocabulary both waits report through.
"""

from __future__ import annotations

from pathlib import Path

from resonant_client.gui.autonomous_loop import (
    AutonomousMissionConfig,
    WaitPolicy,
)


def _config(**overrides) -> AutonomousMissionConfig:
    return AutonomousMissionConfig(
        intent_id="i1", roadmap_path=Path("roadmap.md"), **overrides,
    )


# ── The stall ceiling scales with the mission ───────────────────────────


def test_no_budget_keeps_the_historical_one_hour_ceiling():
    """Full-auto has no length to scale against, so the default stands."""
    assert WaitPolicy.derive_stall_ceiling(None) == 3600.0
    assert WaitPolicy.derive_stall_ceiling(0) == 3600.0


def test_a_short_mission_gets_a_shorter_ceiling():
    """A fixed hour let one sub-task consume a whole 1h mission."""
    assert WaitPolicy.derive_stall_ceiling(3600) == 1800.0


def test_a_long_mission_gets_a_longer_ceiling():
    """A fixed hour killed legitimately long work on a multi-day run — and
    each kill counts toward the failed-streak limit, so two stop the mission."""
    assert WaitPolicy.derive_stall_ceiling(172800) == 14400.0
    assert WaitPolicy.derive_stall_ceiling(14400) == 7200.0


def test_the_derived_ceiling_stays_within_sane_bounds():
    # Absurdly short budget must not produce a ceiling that trips instantly.
    assert WaitPolicy.derive_stall_ceiling(60) == 900.0
    # Absurdly long budget must not effectively disable the guard.
    assert WaitPolicy.derive_stall_ceiling(10_000_000) == 14400.0


# ── Config derivation ───────────────────────────────────────────────────


def test_an_unset_ceiling_is_derived_from_the_budget():
    assert _config(time_budget_seconds=172800).dispatch_timeout_seconds == 14400.0


def test_an_explicit_ceiling_is_respected():
    config = _config(time_budget_seconds=172800, dispatch_timeout_seconds=42.0)
    assert config.dispatch_timeout_seconds == 42.0


def test_an_explicit_none_still_disables_the_ceiling():
    """Distinguishing "said nothing" from "said no ceiling" is why the
    sentinel exists; None must keep meaning disabled."""
    assert _config(dispatch_timeout_seconds=None).dispatch_timeout_seconds is None


# ── The two waits stay distinct ─────────────────────────────────────────


def test_the_policy_exposes_both_waits_separately():
    config = _config(time_budget_seconds=3600, decision_timeout_seconds=600.0)
    policy = config.wait_policy

    assert policy.human_seconds == 600.0
    assert policy.dispatch_seconds == 1800.0


def test_a_human_deadline_does_not_bound_a_grinding_sub_mission():
    """Unifying these into one number would be wrong: waiting 15 minutes for
    an answer says nothing about how long a build may legitimately take."""
    policy = _config(
        time_budget_seconds=172800, decision_timeout_seconds=900.0,
    ).wait_policy

    assert policy.human_seconds == 900.0
    assert policy.dispatch_seconds == 14400.0
    assert policy.human_seconds != policy.dispatch_seconds


def test_waiting_for_a_person_indefinitely_still_guards_against_stalls():
    """"Wait for me" is a statement about decisions, not about hung work."""
    policy = _config(time_budget_seconds=3600).wait_policy

    assert policy.human_seconds is None
    assert policy.dispatch_seconds == 1800.0


def test_describe_reports_both_waits():
    assert _config(
        time_budget_seconds=3600, decision_timeout_seconds=60.0,
    ).wait_policy.describe() == {
        "human_seconds": 60.0,
        "dispatch_seconds": 1800.0,
    }

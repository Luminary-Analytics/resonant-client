"""End-to-end wiring for the per-run park deadline.

The daemon can bound how long it waits on a human decision, but that bound is
only useful if the value the user picks at launch actually reaches the daemon —
and survives a resume. This pins the whole path:

    launch card -> mission_dispatch_autonomous -> roadmap on disk
                -> AutonomousMissionConfig.decision_timeout_seconds

Storing it on the roadmap rather than passing it at spawn is the load-bearing
decision: `resume_autonomous_mission` rebuilds the config from the persisted
roadmap, so anything not written to disk silently reverts to waiting forever
after a crash — exactly the failure the deadline exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

from resonant_client.gui import roadmap as roadmap_module
from resonant_client.gui.autonomous_session import (
    build_roadmap_from_spec,
    parse_time_budget,
)
from resonant_client.gui.roadmap import Roadmap

# Mirrors the rigorous-grill output format the real parser expects; see
# tests/test_autonomous_session.py, which pins the same shape.
_SPEC_MD = """\
## Final spec

**Refined intent:** Build a counter web component.

**Time budget:** 4h

**Acceptance criteria:**
- `[bash]` `npm run build` exits 0
- `[bash]` `npx tsc --noEmit` exits 0
- `[chrome]` Counter button increments via DOM event
- `[vision]` Counter rendered in centered position
"""


# ── Roadmap persistence ─────────────────────────────────────────────────


def test_decision_timeout_round_trips_through_save_and_load(tmp_path: Path):
    rm = Roadmap(
        feature="test",
        intent_id="i1",
        time_budget_label="4h",
        decision_timeout_label="30m",
        status="running",
    )
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)

    reloaded = roadmap_module.load(path)

    assert reloaded.decision_timeout_label == "30m"
    assert reloaded.time_budget_label == "4h"


def test_absent_decision_timeout_is_not_written(tmp_path: Path):
    """Roadmaps written before this feature must not gain a stray line."""
    rm = Roadmap(feature="test", intent_id="i1", time_budget_label="4h")
    path = tmp_path / "roadmap.md"
    roadmap_module.save(rm, path)

    assert "Decision timeout" not in path.read_text(encoding="utf-8")
    assert roadmap_module.load(path).decision_timeout_label == ""


def test_decision_timeout_survives_repeated_rewrites(tmp_path: Path):
    """REFLECT rewrites the roadmap every pass; the deadline must persist."""
    path = tmp_path / "roadmap.md"
    roadmap_module.save(
        Roadmap(feature="f", intent_id="i1", decision_timeout_label="1h"), path,
    )

    for _ in range(3):
        roadmap_module.save(roadmap_module.load(path), path)

    assert roadmap_module.load(path).decision_timeout_label == "1h"


def test_decision_timeout_is_independent_of_the_time_budget(tmp_path: Path):
    """Two different durations on one roadmap must not bleed into each other."""
    path = tmp_path / "roadmap.md"
    roadmap_module.save(
        Roadmap(
            feature="f",
            intent_id="i1",
            time_budget_label="24h",
            decision_timeout_label="15m",
        ),
        path,
    )

    reloaded = roadmap_module.load(path)

    assert reloaded.time_budget_label == "24h"
    assert reloaded.decision_timeout_label == "15m"


# ── Launch -> disk ──────────────────────────────────────────────────────


def test_the_launch_choice_is_written_to_the_roadmap(tmp_path: Path):
    """What the user picks at launch has to survive to disk."""
    rm, path = build_roadmap_from_spec(
        feature="counter",
        intent_id="i1",
        spec_markdown=_SPEC_MD,
        project_path=str(tmp_path),
        decision_timeout_label="30m",
    )

    assert rm.decision_timeout_label == "30m"
    assert roadmap_module.load(path).decision_timeout_label == "30m"


def test_omitting_the_launch_choice_leaves_the_mission_unbounded(tmp_path: Path):
    rm, path = build_roadmap_from_spec(
        feature="counter",
        intent_id="i1",
        spec_markdown=_SPEC_MD,
        project_path=str(tmp_path),
    )

    assert rm.decision_timeout_label == ""
    assert parse_time_budget(roadmap_module.load(path).decision_timeout_label) is None


# ── Label -> seconds ────────────────────────────────────────────────────


def test_launch_card_labels_parse_to_the_expected_seconds():
    """Every preset the launch card offers must be understood."""
    assert parse_time_budget("15m") == 900.0
    assert parse_time_budget("30m") == 1800.0
    assert parse_time_budget("1h") == 3600.0
    assert parse_time_budget("4h") == 14400.0


def test_the_wait_for_me_preset_means_no_deadline():
    """The card sends "" for "Wait for me"; that must stay unbounded."""
    assert parse_time_budget("") is None


# ── Spawn config ────────────────────────────────────────────────────────


def _config_for(roadmap: Roadmap, path: Path):
    """Rebuild the config the way _spawn_autonomous_daemon does."""
    from resonant_client.gui.autonomous_loop import AutonomousMissionConfig

    return AutonomousMissionConfig(
        intent_id=roadmap.intent_id,
        roadmap_path=path,
        time_budget_seconds=parse_time_budget(roadmap.time_budget_label),
        decision_timeout_seconds=parse_time_budget(roadmap.decision_timeout_label),
    )


def test_a_persisted_deadline_reaches_the_daemon_config(tmp_path: Path):
    path = tmp_path / "roadmap.md"
    roadmap_module.save(
        Roadmap(
            feature="f", intent_id="i1",
            time_budget_label="4h", decision_timeout_label="30m",
        ),
        path,
    )

    config = _config_for(roadmap_module.load(path), path)

    assert config.decision_timeout_seconds == 1800.0
    assert config.time_budget_seconds == 14400.0


def test_a_resumed_mission_keeps_its_deadline(tmp_path: Path):
    """The reason this lives on the roadmap at all."""
    path = tmp_path / "roadmap.md"
    roadmap_module.save(
        Roadmap(feature="f", intent_id="i1", decision_timeout_label="1h"), path,
    )

    # A resume reads the roadmap back off disk rather than being handed the
    # value the user originally picked.
    resumed = _config_for(roadmap_module.load(path), path)

    assert resumed.decision_timeout_seconds == 3600.0


def test_a_mission_started_without_a_deadline_waits_forever(tmp_path: Path):
    path = tmp_path / "roadmap.md"
    roadmap_module.save(Roadmap(feature="f", intent_id="i1"), path)

    config = _config_for(roadmap_module.load(path), path)

    assert config.decision_timeout_seconds is None

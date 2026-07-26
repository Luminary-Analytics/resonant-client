"""Tests for v0.6.0a3 — deterministic skill curator.

The curator periodically archives stale agent-created skills,
respecting the `created_by` provenance gate + `pinned` exemption
established in v0.6.0a1. Pure deterministic logic, no model calls.

Covered:
- should_run_curation: rate limiter (24h default, paused-flag respect)
- read_state / write_state: round-trip
- run_curation: archives stale, retains fresh, respects all the
  provenance gates, dry_run mode, REPORT.md content
- _format_report_md: contains expected sections
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from resonant_client.orchestration.skill_curator import (
    _curator_root,
    _state_file,
    read_state,
    run_curation,
    should_run_curation,
    write_state,
)
from resonant_client.orchestration.skills import (
    Skill,
    save_skill,
    skill_dir,
)


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    project = tmp_path / "fakeproj"
    project.mkdir()
    return project


def _seed_skill(
    *,
    project_path,
    skill_id: str,
    created_by: str = "agent",
    pinned: bool = False,
    last_used_days_ago: float = 0.0,
    fail_count: int = 0,
    success_count: int = 1,
) -> Skill:
    """Helper: create + persist a project-scoped skill with the
    given provenance / staleness shape."""
    last_used = time.time() - (last_used_days_ago * 86400) if last_used_days_ago else time.time()
    s = Skill(
        id=skill_id,
        name=skill_id.replace("-", " ").title(),
        description=f"Test skill {skill_id}",
        scope="project",
        triggers=[skill_id],
        created_by=created_by,
        pinned=pinned,
        success_count=success_count,
        fail_count=fail_count,
        last_used_at=last_used,
    )
    save_skill(s, project_path=str(project_path))
    return s


# ── State tracking ────────────────────────────────────────────────────


class TestStatePersistence:
    def test_read_state_missing_file_returns_empty(self, state_home, project_dir):
        assert read_state(str(project_dir)) == {}

    def test_write_then_read_round_trips(self, state_home, project_dir):
        write_state(str(project_dir), {"last_run_at_epoch": 12345.6, "run_count": 3})
        loaded = read_state(str(project_dir))
        assert loaded["last_run_at_epoch"] == 12345.6
        assert loaded["run_count"] == 3

    def test_write_creates_curator_root(self, state_home, project_dir):
        # Curator root doesn't exist yet.
        root = _curator_root(str(project_dir))
        assert not root.exists()
        write_state(str(project_dir), {"x": 1})
        assert root.exists()
        assert _state_file(str(project_dir)).exists()

    def test_corrupt_state_file_returns_empty(self, state_home, project_dir):
        path = _state_file(str(project_dir))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json{{{", encoding="utf-8")
        assert read_state(str(project_dir)) == {}


# ── Rate limiter ──────────────────────────────────────────────────────


class TestShouldRunCuration:
    def test_fresh_project_returns_true(self, state_home, project_dir):
        # No state → never run before → run.
        assert should_run_curation(str(project_dir)) is True

    def test_recent_run_returns_false(self, state_home, project_dir):
        # 1 hour ago — well under the 24h default.
        write_state(str(project_dir), {"last_run_at_epoch": time.time() - 3600})
        assert should_run_curation(str(project_dir)) is False

    def test_old_run_returns_true(self, state_home, project_dir):
        # 25 hours ago — past the threshold.
        write_state(str(project_dir), {"last_run_at_epoch": time.time() - (25 * 3600)})
        assert should_run_curation(str(project_dir)) is True

    def test_paused_state_returns_false_even_when_overdue(self, state_home, project_dir):
        write_state(str(project_dir), {
            "last_run_at_epoch": time.time() - (100 * 3600),
            "paused": True,
        })
        assert should_run_curation(str(project_dir)) is False

    def test_custom_interval(self, state_home, project_dir):
        # Tighter custom interval — 1 hour, 30 minutes ago last run.
        write_state(str(project_dir), {"last_run_at_epoch": time.time() - 1800})
        assert should_run_curation(
            str(project_dir), min_hours_between_runs=1.0,
        ) is False
        # Looser — 0.1 hour interval.
        assert should_run_curation(
            str(project_dir), min_hours_between_runs=0.1,
        ) is True


# ── Provenance gate enforcement ───────────────────────────────────────


class TestProvenanceGate:
    def test_archives_stale_agent_skill(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="stale-agent-x",
            created_by="agent", last_used_days_ago=100,
        )
        report = run_curation(str(project_dir))
        archived = report.archived()
        assert len(archived) == 1
        assert archived[0].skill_id == "stale-agent-x"

    def test_retains_fresh_agent_skill(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="fresh-agent-x",
            created_by="agent", last_used_days_ago=5,
        )
        report = run_curation(str(project_dir))
        assert len(report.archived()) == 0
        assert len(report.retained()) == 1

    def test_does_not_touch_bundled_skill(self, state_home, project_dir):
        # 200 days unused — would be deprecated if it were agent-created.
        _seed_skill(
            project_path=project_dir, skill_id="stale-bundled",
            created_by="bundled", last_used_days_ago=200,
        )
        report = run_curation(str(project_dir))
        # Curator never sees it because list_skills_filtered with
        # created_by="agent" excludes it.
        assert report.skills_reviewed == 0
        assert report.archived() == []
        # Sanity: skill is still on disk.
        assert skill_dir("stale-bundled", scope="project",
                         project_path=str(project_dir)).exists()

    def test_does_not_touch_user_skill(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="stale-user",
            created_by="user", last_used_days_ago=200,
        )
        report = run_curation(str(project_dir))
        assert report.skills_reviewed == 0
        assert skill_dir("stale-user", scope="project",
                         project_path=str(project_dir)).exists()

    def test_does_not_touch_pinned_agent_skill(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="pinned-old",
            created_by="agent", pinned=True, last_used_days_ago=200,
        )
        report = run_curation(str(project_dir))
        # Pinned filter excludes it from the candidate list.
        assert report.skills_reviewed == 0
        # Sanity: skill still on disk, not archived.
        assert skill_dir("pinned-old", scope="project",
                         project_path=str(project_dir)).exists()


# ── Archival reasons ──────────────────────────────────────────────────


class TestArchivalReasons:
    def test_unused_days_reason(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="unused",
            created_by="agent", last_used_days_ago=100,
        )
        report = run_curation(str(project_dir))
        action = report.archived()[0]
        assert "unused" in action.reason
        # Details capture the actual age.
        assert action.details["unused_days_actual"] >= 99
        assert action.details["unused_days_actual"] <= 101

    def test_high_fail_rate_reason(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="failing",
            created_by="agent",
            success_count=2, fail_count=10,  # 83% fail rate
        )
        report = run_curation(str(project_dir))
        action = report.archived()[0]
        assert "fail rate" in action.reason
        assert action.details["fail_rate"] > 0.5

    def test_below_min_uses_not_archived_for_fail_rate(self, state_home, project_dir):
        # 50% fail rate but only 4 uses — below the min_uses threshold,
        # so doesn't trigger archival.
        _seed_skill(
            project_path=project_dir, skill_id="few-uses",
            created_by="agent",
            success_count=2, fail_count=2,
            last_used_days_ago=5,  # fresh
        )
        report = run_curation(str(project_dir))
        assert report.archived() == []


# ── Reports ───────────────────────────────────────────────────────────


class TestReports:
    def test_run_creates_report_files(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="x",
            created_by="agent", last_used_days_ago=100,
        )
        report = run_curation(str(project_dir))
        run_dir = Path(report.state_dir)
        assert run_dir.exists()
        assert (run_dir / "run.json").exists()
        assert (run_dir / "REPORT.md").exists()

    def test_run_json_round_trips(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="rj",
            created_by="agent", last_used_days_ago=100,
        )
        report = run_curation(str(project_dir))
        run_json = json.loads(
            (Path(report.state_dir) / "run.json").read_text(encoding="utf-8"),
        )
        assert run_json["skills_reviewed"] == 1
        assert run_json["actions"][0]["kind"] == "archive"
        assert run_json["actions"][0]["skill_id"] == "rj"

    def test_report_md_includes_archived_section(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="archived-skill",
            created_by="agent", last_used_days_ago=100,
        )
        report = run_curation(str(project_dir))
        md = (Path(report.state_dir) / "REPORT.md").read_text(encoding="utf-8")
        assert "Archived (1)" in md
        assert "archived-skill" in md

    def test_report_md_includes_retained_section(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="kept",
            created_by="agent", last_used_days_ago=5,
        )
        report = run_curation(str(project_dir))
        md = (Path(report.state_dir) / "REPORT.md").read_text(encoding="utf-8")
        assert "Retained (1)" in md
        assert "kept" in md

    def test_empty_project_report_is_clean(self, state_home, project_dir):
        report = run_curation(str(project_dir))
        md = (Path(report.state_dir) / "REPORT.md").read_text(encoding="utf-8")
        assert "No curator-touchable skills" in md


# ── State updates after run ───────────────────────────────────────────


class TestStateUpdatesAfterRun:
    def test_state_records_run_count_and_timestamp(self, state_home, project_dir):
        # Run twice, run_count should increment.
        before = time.time()
        run_curation(str(project_dir))
        first_state = read_state(str(project_dir))
        assert first_state["run_count"] == 1
        assert first_state["last_run_at_epoch"] >= before

        run_curation(str(project_dir))
        second_state = read_state(str(project_dir))
        assert second_state["run_count"] == 2

    def test_dry_run_does_not_update_state(self, state_home, project_dir):
        # Pre-state should remain untouched.
        write_state(str(project_dir), {"run_count": 5, "last_run_at_epoch": 100.0})
        run_curation(str(project_dir), dry_run=True)
        state = read_state(str(project_dir))
        assert state["run_count"] == 5
        assert state["last_run_at_epoch"] == 100.0


# ── Dry-run mode ──────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_does_not_archive(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="should-stay",
            created_by="agent", last_used_days_ago=100,
        )
        report = run_curation(str(project_dir), dry_run=True)
        # Report still records the would-be archival.
        assert len(report.archived()) == 1
        assert report.archived()[0].skill_id == "should-stay"
        # But the skill is still on disk in its original location.
        assert skill_dir("should-stay", scope="project",
                         project_path=str(project_dir)).exists()

    def test_dry_run_does_not_write_report_files(self, state_home, project_dir):
        _seed_skill(
            project_path=project_dir, skill_id="x",
            created_by="agent", last_used_days_ago=100,
        )
        report = run_curation(str(project_dir), dry_run=True)
        run_dir = Path(report.state_dir)
        # state_dir was set in the report but no files were written.
        assert not (run_dir / "run.json").exists()
        assert not (run_dir / "REPORT.md").exists()


# ── Configurable thresholds ───────────────────────────────────────────


class TestThresholdsConfigurable:
    def test_short_unused_days_threshold_archives_more(self, state_home, project_dir):
        # 40 days unused. Default is 90, so retained. With threshold=30,
        # it archives.
        _seed_skill(
            project_path=project_dir, skill_id="x",
            created_by="agent", last_used_days_ago=40,
        )
        default_report = run_curation(str(project_dir), dry_run=True)
        assert default_report.archived() == []

        # Re-seed (the dry-run didn't move it but state-write doesn't
        # touch the skill itself either).
        tight_report = run_curation(
            str(project_dir),
            unused_days_archive=30,
            dry_run=True,
        )
        assert len(tight_report.archived()) == 1

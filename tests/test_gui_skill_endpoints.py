"""Tests for v0.6.2a3 — GUI skill endpoints (WS surface).

The Skills sidebar + detail modal are driven by four WebSocket commands:
- skill_list / skill_view / skill_pin_toggle / skill_archive

Each handler in app.py is a thin wrapper around the existing skills.py
public API plus a JSON-projection step. Tests target the projection
helpers (`_skill_list_payload`, `_skill_view_payload`) since the
WebSocket plumbing itself is exercised by the live GUI.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from resonant_client.gui.app import _skill_list_payload, _skill_view_payload
from resonant_client.orchestration.skills import (
    Skill,
    save_skill,
)


@pytest.fixture
def state_home(tmp_path, monkeypatch):
    home = tmp_path / "state-home"
    home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(home))
    return home


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "fakeproj"
    p.mkdir()
    return p


def _seed(
    *,
    skill_id: str,
    name: str | None = None,
    description: str = "",
    scope: str = "global",
    project_path=None,
    created_by: str = "agent",
    pinned: bool = False,
    last_used_days_ago: float = 0.0,
    success_count: int = 1,
    procedure_md: str = "",
):
    last_used = (
        time.time() - (last_used_days_ago * 86400)
        if last_used_days_ago else time.time()
    )
    s = Skill(
        id=skill_id,
        name=name or skill_id.replace("-", " ").title(),
        description=description or f"Test skill {skill_id}",
        scope=scope,
        created_by=created_by,
        pinned=pinned,
        last_used_at=last_used,
        success_count=success_count,
    )
    save_skill(
        s,
        procedure_md=procedure_md or f"# {skill_id}\n\nProcedure body.",
        project_path=str(project_path) if project_path else None,
    )
    return s


# ── _skill_list_payload ─────────────────────────────────────────────


class TestSkillListPayload:
    def test_empty_state_returns_empty_list(self, state_home):
        out = _skill_list_payload()
        assert out == {"event": "skill_list", "skills": []}

    def test_each_skill_row_has_required_fields(self, state_home):
        _seed(skill_id="alpha", description="An alpha skill")
        out = _skill_list_payload()
        assert out["event"] == "skill_list"
        assert len(out["skills"]) == 1
        row = out["skills"][0]
        # Contract: every row has these fields, all JSON-safe.
        for key in (
            "id", "name", "description", "scope", "created_by",
            "pinned", "deprecated", "success_count", "fail_count",
            "last_used_at", "version",
        ):
            assert key in row, f"missing {key} in row {row}"
        assert isinstance(row["pinned"], bool)
        assert isinstance(row["deprecated"], bool)
        assert isinstance(row["success_count"], int)
        assert isinstance(row["last_used_at"], float)

    def test_pinned_skills_float_to_top(self, state_home):
        _seed(skill_id="not-pinned", pinned=False)
        _seed(skill_id="zzz-pinned", pinned=True)  # alpha-late but pinned
        out = _skill_list_payload()
        ids = [r["id"] for r in out["skills"]]
        assert ids[0] == "zzz-pinned"

    def test_unpinned_sort_by_recent_use(self, state_home):
        _seed(skill_id="ancient", last_used_days_ago=30, pinned=False)
        _seed(skill_id="recent", last_used_days_ago=0, pinned=False)
        _seed(skill_id="old", last_used_days_ago=10, pinned=False)
        out = _skill_list_payload()
        ids = [r["id"] for r in out["skills"]]
        assert ids == ["recent", "old", "ancient"]

    def test_include_deprecated_flag_passes_through(
        self, state_home, monkeypatch
    ):
        # Seed an agent skill with last_used 100 days ago — auto-deprecated
        # by `Skill.is_deprecated`. Without `include_deprecated=True` the
        # filter excludes it.
        _seed(skill_id="stale", last_used_days_ago=100, pinned=False)
        out_default = _skill_list_payload(include_deprecated=False)
        assert all(r["id"] != "stale" for r in out_default["skills"])
        out_full = _skill_list_payload(include_deprecated=True)
        assert any(r["id"] == "stale" for r in out_full["skills"])

    def test_project_scoped_skill_visible_when_project_path_supplied(
        self, state_home, project_dir
    ):
        _seed(
            skill_id="proj-only", scope="project", project_path=project_dir,
        )
        # No project path → not visible.
        out_no_proj = _skill_list_payload()
        assert all(r["id"] != "proj-only" for r in out_no_proj["skills"])
        # With project path → visible.
        out_proj = _skill_list_payload(project_path=str(project_dir))
        assert any(r["id"] == "proj-only" for r in out_proj["skills"])


# ── _skill_view_payload ─────────────────────────────────────────────


class TestSkillViewPayload:
    def test_unknown_skill_returns_none_with_error(self, state_home):
        out = _skill_view_payload("does-not-exist")
        assert out["event"] == "skill_view_data"
        assert out["skill"] is None
        assert "not found" in out["error"]

    def test_existing_skill_returns_full_data(self, state_home):
        _seed(
            skill_id="alpha",
            name="Alpha Skill",
            description="A test skill description",
            procedure_md="# Alpha Skill\n\nDo this then that.",
        )
        out = _skill_view_payload("alpha")
        assert out["event"] == "skill_view_data"
        s = out["skill"]
        assert s is not None
        assert s["id"] == "alpha"
        assert s["name"] == "Alpha Skill"
        assert s["description"] == "A test skill description"
        # Procedure body included in payload (no second round-trip).
        assert "Do this then that" in s["procedure_md"]
        # All required fields present.
        assert s["scope"] == "global"
        assert s["pinned"] is False
        assert isinstance(s["triggers"], list)

    def test_view_includes_pinned_state(self, state_home):
        _seed(skill_id="pinned-x", pinned=True)
        out = _skill_view_payload("pinned-x")
        assert out["skill"]["pinned"] is True

    def test_view_for_project_scoped_skill(self, state_home, project_dir):
        _seed(
            skill_id="proj-x", scope="project", project_path=project_dir,
            procedure_md="# Project skill body",
        )
        out = _skill_view_payload("proj-x", project_path=str(project_dir))
        assert out["skill"] is not None
        assert "Project skill body" in out["skill"]["procedure_md"]

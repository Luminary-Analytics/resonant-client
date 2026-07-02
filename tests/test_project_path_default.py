"""
Tests for v0.3.3 project-path sanitization.

When the bundled exe is launched via Start Menu shortcut, Windows sets
`cwd = C:\\Program Files\\Resonant Client`. Pre-v0.3.3 ProjectManager()
took that as the project path silently → permission-denied storms when
the agent tried to write into the install dir (Bug #25).

`_safe_default_project_path` resolves through:
  1. cwd, IF cwd is user-writable AND not a system/install location
  2. most-recent-project from recent_projects.json (filtered to existing)
  3. ~/Documents/Resonant Projects (created on demand)
  4. ~/.resonant/workspace (last-resort fallback)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from resonant_client.gui import sessions as sessions_mod
from resonant_client.gui.sessions import (
    ProjectManager,
    _is_unsafe_cwd,
    _safe_default_project_path,
)


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    """Point ~/.resonant at a tmp dir + neuter Path.home() so we don't
    touch the real Documents folder during tests."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    fake_resonant = fake_home / ".resonant"
    monkeypatch.setattr(sessions_mod, "_RESONANT_DIR", fake_resonant)
    monkeypatch.setattr(sessions_mod, "_PROJECTS_DIR", fake_resonant / "projects")
    monkeypatch.setattr(sessions_mod.Path, "home", staticmethod(lambda: fake_home))
    return fake_home


class TestIsUnsafeCwd:
    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific paths")
    def test_program_files_is_unsafe(self):
        assert _is_unsafe_cwd("C:\\Program Files\\Resonant Client") is True
        assert _is_unsafe_cwd("C:\\Program Files (x86)\\Foo") is True

    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific paths")
    def test_windows_dir_is_unsafe(self):
        assert _is_unsafe_cwd("C:\\Windows\\System32") is True
        assert _is_unsafe_cwd("C:\\ProgramData\\Foo") is True

    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific paths")
    def test_user_dir_is_safe(self):
        assert _is_unsafe_cwd("C:\\Users\\rich\\Dev\\my-game") is False
        assert _is_unsafe_cwd("D:\\Repos\\my-project") is False

    @pytest.mark.skipif(os.name != "nt", reason="Windows-specific case-folding")
    def test_case_insensitive_match_on_windows(self):
        # Windows paths often arrive with mixed case from os.getcwd().
        assert _is_unsafe_cwd("c:\\program files\\foo") is True
        assert _is_unsafe_cwd("C:\\PROGRAM FILES\\foo") is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-specific paths")
    def test_applications_unsafe_on_posix(self):
        assert _is_unsafe_cwd("/Applications/MyApp.app") is True
        assert _is_unsafe_cwd("/usr/bin/foo") is True

    @pytest.mark.skipif(os.name == "nt", reason="POSIX-specific paths")
    def test_user_home_safe_on_posix(self):
        assert _is_unsafe_cwd("/home/user/dev/project") is False

    def test_empty_path_is_unsafe(self):
        # An empty string isn't a valid project — we should fall through
        # to the safe-default resolution.
        assert _is_unsafe_cwd("") is True


class TestSafeDefaultProjectPath:
    def test_returns_writable_cwd_when_safe(self, tmp_path, monkeypatch, isolated_home):
        # When cwd is writable and not a system dir, it wins (preserves
        # the existing "launch from terminal in repo root" workflow).
        proj = tmp_path / "myrepo"
        proj.mkdir()
        monkeypatch.chdir(proj)
        assert _safe_default_project_path() == str(proj)

    def test_falls_back_when_cwd_is_install_dir(self, monkeypatch, isolated_home):
        # Simulate the bundled-exe-from-Start-Menu case: cwd looks like
        # an install dir. Should NOT return cwd.
        monkeypatch.setattr(os, "getcwd", lambda: ("C:\\Program Files\\Resonant Client"
                                                    if os.name == "nt"
                                                    else "/Applications/Resonant.app"))
        # No recent projects → falls through to ~/Documents/Resonant Projects.
        result = _safe_default_project_path()
        assert "Resonant Projects" in result or ".resonant" in result
        # And whichever path is chosen, it must NOT be the install dir.
        if os.name == "nt":
            assert "Program Files" not in result
        else:
            assert not result.startswith("/Applications/")

    def test_uses_most_recent_project_when_install_cwd(self, monkeypatch, isolated_home, tmp_path):
        # cwd is unsafe AND the user has a recent project on disk →
        # prefer the recent one over the Documents fallback. The recent
        # entry uses a fake NON-temp path (with isdir patched) because
        # real tmp_path dirs live under pytest-of-*, which the recents
        # loop now filters as test-fixture pollution.
        good_project = ("D:\\Repos\\old_project" if os.name == "nt"
                        else "/home/user/dev/old_project")

        recents = isolated_home / ".resonant" / "recent_projects.json"
        recents.parent.mkdir(parents=True, exist_ok=True)
        recents.write_text(json.dumps([
            {"path": good_project, "name": "old_project", "last_used": 0},
        ]))
        real_isdir = os.path.isdir
        monkeypatch.setattr(
            os.path, "isdir",
            lambda p: True if p == good_project else real_isdir(p),
        )
        monkeypatch.setattr(os, "getcwd", lambda: ("C:\\Program Files\\Resonant Client"
                                                    if os.name == "nt"
                                                    else "/Applications/X"))
        result = _safe_default_project_path()
        assert result == good_project

    def test_recents_skip_pytest_fixture_dirs(self, monkeypatch, isolated_home, tmp_path):
        # Tests that call set_project can leave pytest temp paths in the
        # live recents file; the recents loop must skip them even though
        # they exist on disk (they vanish a few pytest runs later).
        polluted = tmp_path / "polluted_project"
        polluted.mkdir()
        good_project = ("D:\\Repos\\real_project" if os.name == "nt"
                        else "/home/user/dev/real_project")

        recents = isolated_home / ".resonant" / "recent_projects.json"
        recents.parent.mkdir(parents=True, exist_ok=True)
        recents.write_text(json.dumps([
            {"path": str(polluted), "name": "polluted_project", "last_used": 1},
            {"path": good_project, "name": "real_project", "last_used": 0},
        ]))
        real_isdir = os.path.isdir
        monkeypatch.setattr(
            os.path, "isdir",
            lambda p: True if p == good_project else real_isdir(p),
        )
        monkeypatch.setattr(os, "getcwd", lambda: ("C:\\Program Files\\Resonant Client"
                                                    if os.name == "nt"
                                                    else "/Applications/X"))
        assert _safe_default_project_path() == good_project

    def test_explicit_resonant_checkout_restores_from_recents(self, monkeypatch, isolated_home):
        # Dogfooding case: the user explicitly opened the Resonant repo
        # and it sits in recents. A normal desktop launch (cwd = install
        # dir, NOT the checkout) must restore it — the resonant-source
        # veto only applies to the dev-launch cwd default.
        checkout = ("D:\\Repos\\resonant-checkout" if os.name == "nt"
                    else "/home/user/dev/resonant-checkout")

        recents = isolated_home / ".resonant" / "recent_projects.json"
        recents.parent.mkdir(parents=True, exist_ok=True)
        recents.write_text(json.dumps([
            {"path": checkout, "name": "resonant-checkout", "last_used": 0},
        ]))
        real_isdir = os.path.isdir
        monkeypatch.setattr(
            os.path, "isdir",
            lambda p: True if p == checkout else real_isdir(p),
        )
        monkeypatch.setattr(
            sessions_mod, "_looks_like_resonant_source",
            lambda p: str(p) == checkout,
        )
        monkeypatch.setattr(os, "getcwd", lambda: ("C:\\Program Files\\Resonant Client"
                                                    if os.name == "nt"
                                                    else "/Applications/X"))
        assert _safe_default_project_path() == checkout

    def test_dev_launch_from_checkout_skips_resonant_recents(self, monkeypatch, isolated_home):
        # Dev-server launch FROM the checkout: cwd is vetoed as resonant
        # source, and the recents loop must not hand the same checkout
        # straight back — otherwise the veto is a no-op on any machine
        # whose recents already contain the repo.
        checkout = ("D:\\Repos\\resonant-checkout" if os.name == "nt"
                    else "/home/user/dev/resonant-checkout")
        other = ("D:\\Repos\\other_project" if os.name == "nt"
                 else "/home/user/dev/other_project")

        recents = isolated_home / ".resonant" / "recent_projects.json"
        recents.parent.mkdir(parents=True, exist_ok=True)
        recents.write_text(json.dumps([
            {"path": checkout, "name": "resonant-checkout", "last_used": 1},
            {"path": other, "name": "other_project", "last_used": 0},
        ]))
        real_isdir = os.path.isdir
        monkeypatch.setattr(
            os.path, "isdir",
            lambda p: True if p in (checkout, other) else real_isdir(p),
        )
        monkeypatch.setattr(
            sessions_mod, "_looks_like_resonant_source",
            lambda p: str(p) == checkout or p == os.getcwd(),
        )
        monkeypatch.setattr(os, "getcwd", lambda: checkout)
        assert _safe_default_project_path() == other

    def test_skips_recent_projects_that_no_longer_exist(self, monkeypatch, isolated_home, tmp_path):
        # Stale recent_projects.json — pointing at a since-deleted dir
        # — must not be returned. Falls through to Documents.
        recents = isolated_home / ".resonant" / "recent_projects.json"
        recents.parent.mkdir(parents=True, exist_ok=True)
        recents.write_text(json.dumps([
            {"path": str(tmp_path / "missing"), "name": "missing", "last_used": 0},
        ]))
        monkeypatch.setattr(os, "getcwd", lambda: ("C:\\Program Files\\Resonant Client"
                                                    if os.name == "nt"
                                                    else "/Applications/X"))
        result = _safe_default_project_path()
        assert "missing" not in result

    def test_creates_documents_resonant_projects_when_needed(self, monkeypatch, isolated_home):
        monkeypatch.setattr(os, "getcwd", lambda: ("C:\\Program Files\\Resonant Client"
                                                    if os.name == "nt"
                                                    else "/usr/bin"))
        result = _safe_default_project_path()
        # The fallback must exist on disk after the call (so subsequent
        # ProjectManager() calls don't fail when creating sessions/).
        assert os.path.isdir(result)


class TestProjectManagerUsesSafeDefault:
    def test_explicit_path_still_wins(self, tmp_path, isolated_home):
        # Backwards-compat: when caller passes a path, it must be honored
        # even if it's outside the safe-default zones.
        explicit = tmp_path / "explicit"
        explicit.mkdir()
        pm = ProjectManager(str(explicit))
        assert pm.project_path == str(explicit)

    def test_unsafe_cwd_does_not_become_project(self, monkeypatch, isolated_home):
        # The Bug #25 regression test: when cwd is the install dir,
        # ProjectManager() must NOT take it.
        monkeypatch.setattr(os, "getcwd", lambda: ("C:\\Program Files\\Resonant Client"
                                                    if os.name == "nt"
                                                    else "/Applications/Resonant.app"))
        pm = ProjectManager()
        if os.name == "nt":
            assert "Program Files" not in pm.project_path
        else:
            assert not pm.project_path.startswith("/Applications/")

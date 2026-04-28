"""
Tests for resonant_client/engine/git_tools.py

Each test uses a tmp_path git repo so the suite is fully isolated from the
host repo. Skipped on machines without a `git` executable on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from resonant_client.engine import git_tools


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _require_git():
    if shutil.which("git") is None:
        pytest.skip("git executable not on PATH")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialize a fresh git repo with a deterministic identity."""
    cwd = tmp_path / "repo"
    cwd.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(cwd), check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(cwd), check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(cwd), check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=str(cwd), check=True)
    return cwd


def _commit(cwd: Path, filename: str, content: str, message: str) -> str:
    (cwd / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", "--", filename], cwd=str(cwd), check=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=str(cwd), check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()
    return sha


# ── git_status ──────────────────────────────────────────────────────────


class TestGitStatus:
    def test_not_a_repo(self, tmp_path):
        data = git_tools.git_status(tmp_path)
        assert data["error"] == "not a git repository"

    def test_clean_repo(self, repo):
        _commit(repo, "a.txt", "hello\n", "initial")
        data = git_tools.git_status(repo)
        assert data["branch"] == "main"
        assert data["clean"] is True
        assert data["staged"] == []
        assert data["unstaged"] == []
        assert data["untracked"] == []

    def test_unstaged_modification(self, repo):
        _commit(repo, "a.txt", "hello\n", "initial")
        (repo / "a.txt").write_text("hello world\n", encoding="utf-8")
        data = git_tools.git_status(repo)
        assert data["clean"] is False
        assert any(s["path"] == "a.txt" and s["status"] == "M" for s in data["unstaged"])

    def test_staged_addition(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        (repo / "b.txt").write_text("new\n", encoding="utf-8")
        subprocess.run(["git", "add", "b.txt"], cwd=str(repo), check=True)
        data = git_tools.git_status(repo)
        assert any(s["path"] == "b.txt" and s["status"] == "A" for s in data["staged"])

    def test_untracked(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        (repo / "stray.txt").write_text("hi\n", encoding="utf-8")
        data = git_tools.git_status(repo)
        assert "stray.txt" in data["untracked"]


# ── git_diff ────────────────────────────────────────────────────────────


class TestGitDiff:
    def test_no_changes(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        data = git_tools.git_diff(repo)
        assert data["files"] == []
        assert data["total_additions"] == 0
        assert data["total_deletions"] == 0

    def test_unstaged_diff(self, repo):
        _commit(repo, "a.txt", "one\ntwo\nthree\n", "init")
        (repo / "a.txt").write_text("one\nTWO\nthree\nfour\n", encoding="utf-8")
        data = git_tools.git_diff(repo)
        assert len(data["files"]) == 1
        f = data["files"][0]
        assert f["path"].endswith("a.txt")
        # Modified one line + added one line: 2 additions, 1 deletion
        assert f["additions"] == 2
        assert f["deletions"] == 1
        assert data["total_additions"] == 2
        assert data["total_deletions"] == 1
        # Hunks should have header + lines
        assert any(h["header"].startswith("@@") for h in f["hunks"])

    def test_staged_diff(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        (repo / "a.txt").write_text("x\ny\n", encoding="utf-8")
        subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
        unstaged = git_tools.git_diff(repo, staged=False)
        staged = git_tools.git_diff(repo, staged=True)
        assert unstaged["files"] == []
        assert len(staged["files"]) == 1


# ── git_commit ──────────────────────────────────────────────────────────


class TestGitCommit:
    def test_requires_message(self, repo):
        data = git_tools.git_commit(repo, "")
        assert "error" in data

    def test_nothing_staged(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        data = git_tools.git_commit(repo, "empty")
        assert "nothing staged" in data.get("error", "")

    def test_stages_and_commits(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        (repo / "b.txt").write_text("new file\n", encoding="utf-8")
        data = git_tools.git_commit(repo, "Add b", paths=["b.txt"])
        assert "error" not in data
        assert len(data["commit_sha"]) == 40
        assert data["short_sha"] == data["commit_sha"][:7]
        assert data["message"] == "Add b"

    def test_multi_line_message(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        (repo / "b.txt").write_text("hi\n", encoding="utf-8")
        msg = "Subject line\n\nBody paragraph with details."
        data = git_tools.git_commit(repo, msg, paths=["b.txt"])
        assert "error" not in data
        # The commit message should contain both lines
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout
        assert "Subject line" in log
        assert "Body paragraph" in log


# ── git_branch_create ──────────────────────────────────────────────────


class TestGitBranchCreate:
    def test_creates_and_checks_out(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        data = git_tools.git_branch_create(repo, "feature/test")
        assert "error" not in data
        assert data["branch"] == "feature/test"
        # Confirm we are now on that branch
        out = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert out == "feature/test"

    def test_refuses_existing(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        git_tools.git_branch_create(repo, "feature/test")
        data = git_tools.git_branch_create(repo, "feature/test")
        assert "already exists" in data.get("error", "")

    def test_unknown_from_ref(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        data = git_tools.git_branch_create(repo, "bad-ref-branch", from_ref="nonexistent-sha")
        assert "unknown ref" in data.get("error", "")

    def test_requires_branch_name(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        data = git_tools.git_branch_create(repo, "")
        assert "branch name is required" in data.get("error", "")


# ── git_log ─────────────────────────────────────────────────────────────


class TestGitLog:
    def test_empty_repo(self, repo):
        data = git_tools.git_log(repo)
        assert data["commits"] == []

    def test_returns_commits(self, repo):
        _commit(repo, "a.txt", "x\n", "first")
        _commit(repo, "b.txt", "y\n", "second")
        data = git_tools.git_log(repo, limit=10)
        assert len(data["commits"]) == 2
        assert data["commits"][0]["subject"] == "second"
        assert data["commits"][1]["subject"] == "first"
        for c in data["commits"]:
            assert len(c["sha"]) == 40
            assert len(c["short_sha"]) == 7
            assert c["author_email"] == "test@example.com"

    def test_respects_limit(self, repo):
        for i in range(5):
            _commit(repo, f"f{i}.txt", "x\n", f"commit-{i}")
        data = git_tools.git_log(repo, limit=3)
        assert len(data["commits"]) == 3

    def test_clamps_limit(self, repo):
        _commit(repo, "a.txt", "x\n", "first")
        data = git_tools.git_log(repo, limit=0)  # < 1 should clamp to 1
        assert "error" not in data


# ── exec_* (ToolResult wrappers) ────────────────────────────────────────


class TestExecWrappers:
    def test_exec_status(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        result = git_tools.exec_git_status({"cwd": str(repo)}, start=0.0)
        assert result.is_error is False
        assert "main" in result.output
        assert result.metadata["clean"] is True

    def test_exec_diff(self, repo):
        _commit(repo, "a.txt", "one\n", "init")
        (repo / "a.txt").write_text("two\n", encoding="utf-8")
        result = git_tools.exec_git_diff({"cwd": str(repo)}, start=0.0)
        assert result.is_error is False
        assert "additions" in result.output

    def test_exec_commit_success(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        (repo / "b.txt").write_text("hi\n", encoding="utf-8")
        result = git_tools.exec_git_commit(
            {"cwd": str(repo), "message": "feat: b", "paths": ["b.txt"]}, start=0.0,
        )
        assert result.is_error is False
        assert result.metadata["short_sha"]
        assert "feat: b" in result.output

    def test_exec_branch(self, repo):
        _commit(repo, "a.txt", "x\n", "init")
        result = git_tools.exec_git_branch_create(
            {"cwd": str(repo), "branch": "topic/a"}, start=0.0,
        )
        assert result.is_error is False
        assert "topic/a" in result.output

    def test_exec_log(self, repo):
        _commit(repo, "a.txt", "x\n", "first")
        _commit(repo, "b.txt", "y\n", "second")
        result = git_tools.exec_git_log({"cwd": str(repo), "limit": 5}, start=0.0)
        assert result.is_error is False
        assert "first" in result.output
        assert "second" in result.output


# ── Tool registration smoke test ───────────────────────────────────────


class TestToolRegistration:
    def test_all_git_tools_registered(self):
        from resonant_client.engine import tools as tools_mod
        names = {t["function"]["name"] for t in tools_mod.AGENT_TOOLS}
        for n in ["git_status", "git_diff", "git_commit", "git_branch_create", "git_log"]:
            assert n in names, f"git tool '{n}' not registered in AGENT_TOOLS"

    def test_dispatch_routes_git_tools(self, repo):
        # execute_tool should dispatch git_status without error
        from resonant_client.engine.tools import execute_tool
        _commit(repo, "a.txt", "x\n", "init")
        result = execute_tool("git_status", {"cwd": str(repo)})
        assert result.is_error is False
        assert "main" in result.output

    def test_read_only_classification(self):
        from resonant_client.engine.sandbox import READ_ONLY_TOOLS
        assert "git_status" in READ_ONLY_TOOLS
        assert "git_diff" in READ_ONLY_TOOLS
        assert "git_log" in READ_ONLY_TOOLS
        # Mutating tools should NOT be read-only
        assert "git_commit" not in READ_ONLY_TOOLS
        assert "git_branch_create" not in READ_ONLY_TOOLS

    def test_icons_exist(self):
        from resonant_client.engine.tools import TOOL_ICONS
        for n in ["git_status", "git_diff", "git_commit", "git_branch_create", "git_log"]:
            assert n in TOOL_ICONS, f"icon missing for {n}"

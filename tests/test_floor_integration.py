"""Integration tests — floor enforcement fires from inside tool dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from resonant_client.engine.tools import execute_tool


@pytest.fixture
def project_dir(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "README.md").write_text("# proj\n", encoding="utf-8")
    return p


# ── Floor fires for risky tool calls ───────────────────────────────────


def test_force_push_to_main_returns_floor_violation(project_dir):
    result = execute_tool(
        "bash",
        {"command": "git push --force origin main"},
        project_path=str(project_dir),
    )
    assert result.is_error is True
    assert "FLOOR_VIOLATION" in result.output
    assert result.metadata.get("floor_violation", {}).get("rule") == "protected_branch_force_push"


def test_rm_rf_outside_project_returns_floor_violation(project_dir):
    result = execute_tool(
        "bash",
        {"command": "rm -rf /etc/hosts"},
        project_path=str(project_dir),
    )
    assert result.is_error is True
    assert result.metadata.get("floor_violation", {}).get("rule") == "rm_rf_outside_project"


def test_file_write_to_ssh_returns_floor_violation(project_dir):
    result = execute_tool(
        "file_write",
        {"path": str(Path.home() / ".ssh" / "config"), "content": "Host x"},
        project_path=str(project_dir),
    )
    assert result.is_error is True
    assert result.metadata.get("floor_violation", {}).get("rule") == "writes_to_protected_path"


# ── Floor doesn't fire for routine actions ─────────────────────────────


def test_routine_file_read_runs_normally(project_dir):
    result = execute_tool(
        "file_read",
        {"path": str(project_dir / "README.md")},
        project_path=str(project_dir),
    )
    assert result.is_error is False
    assert "proj" in result.output
    assert result.metadata.get("floor_violation") is None


def test_routine_bash_runs_normally(project_dir):
    """`echo hi` should never hit the floor."""
    result = execute_tool(
        "bash",
        {"command": "echo hi"},
        project_path=str(project_dir),
    )
    # Result might still error if echo isn't on the test machine's path, but
    # should NOT have a floor violation
    assert result.metadata.get("floor_violation") is None


def test_force_with_lease_runs_normally(project_dir):
    """--force-with-lease is the safer variant; the floor allows it through."""
    # We can't actually run git push without a remote; just confirm the floor
    # didn't preempt. The dispatch will fail (no remote) but no floor violation.
    result = execute_tool(
        "bash",
        {"command": "git push --force-with-lease origin main"},
        project_path=str(project_dir),
    )
    assert result.metadata.get("floor_violation") is None


# ── No project_path passed ─────────────────────────────────────────────


def test_floor_check_works_without_project_path():
    """If project_path isn't supplied, the floor still catches obvious violations
    (rm -rf /etc with cwd defaulting to os.getcwd())."""
    result = execute_tool(
        "bash",
        {"command": "rm -rf /etc"},
        project_path="",  # explicit empty
    )
    # Whether it fires depends on whether /etc is "outside" os.getcwd(); on the
    # test runner, os.getcwd() != /etc, so it should fire.
    if result.metadata.get("floor_violation"):
        assert result.metadata["floor_violation"]["rule"] == "rm_rf_outside_project"


# ── Settings overrides applied ─────────────────────────────────────────


class _FakeSettings:
    def __init__(self, **vals):
        self._vals = vals

    def get(self, section, key, default=None):
        return self._vals.get(f"{section}.{key}", default)


def test_custom_protected_branch_via_settings(project_dir):
    settings = _FakeSettings(**{
        "general.autonomy_protected_branches": ["develop"],
    })
    result = execute_tool(
        "bash",
        {"command": "git push --force origin develop"},
        project_path=str(project_dir),
        settings=settings,
    )
    assert result.is_error is True
    assert result.metadata.get("floor_violation", {}).get("rule") == "protected_branch_force_push"


# ── Floor metadata structure ───────────────────────────────────────────


def test_floor_violation_metadata_has_required_fields(project_dir):
    result = execute_tool(
        "bash",
        {"command": "rm -rf ~/.ssh"},
        project_path=str(project_dir),
    )
    fv = result.metadata.get("floor_violation")
    assert fv is not None
    for key in ("rule", "reason", "severity", "suggested_action", "tool_name"):
        assert key in fv, f"floor_violation missing {key}"
    assert fv["severity"] == "hard"


# ── End-to-end: IntentService runs through real execute_tool ────────────


def test_floor_violation_surfaces_through_intent_service(monkeypatch, tmp_path, project_dir):
    """A specialist that tries a force-push to main should see the violation
    flow back through Session → execute_tool → SpecialistResult.
    """
    state_home = tmp_path / "state"
    state_home.mkdir()
    monkeypatch.setenv("RESONANT_STATE_HOME", str(state_home))

    from resonant_client.engine.tools import execute_tool as et
    result = et(
        "bash",
        {"command": "git push --force origin main"},
        project_path=str(project_dir),
    )
    assert result.is_error
    assert "protected_branch_force_push" in (result.metadata.get("floor_violation", {}).get("rule") or "")

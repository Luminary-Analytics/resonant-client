"""Irreversibility-floor checks — what pauses for approval and what just runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from resonant_client.orchestration import (
    AutonomySettings,
    DEFAULT_BUDGET_USD_MAX,
    DEFAULT_PROTECTED_BRANCHES,
    FloorViolation,
    check_floor,
)


# Helper: run a check with explicit defaults
def _run(tool, args=None, project="/tmp/proj"):
    return check_floor(tool_name=tool, args=args or {}, project_path=project, settings=None)


# ── Force push to protected branch ──────────────────────────────────────


def test_force_push_to_main_is_floor_violation():
    v = _run("bash", {"command": "git push --force origin main"})
    assert v is not None
    assert v.rule == "protected_branch_force_push"
    assert "main" in v.reason


def test_force_push_to_master_is_floor_violation():
    v = _run("bash", {"command": "git push -f origin master"})
    assert v is not None
    assert v.rule == "protected_branch_force_push"


def test_force_push_to_release_branch_is_protected():
    """release/* is protected via prefix."""
    v = _run("bash", {"command": "git push --force origin release/1.2"})
    assert v is not None
    assert v.rule == "protected_branch_force_push"


def test_force_with_lease_is_allowed():
    """--force-with-lease is the safer variant; let it through."""
    v = _run("bash", {"command": "git push --force-with-lease origin main"})
    assert v is None


def test_normal_push_to_main_is_allowed():
    """Plain push to main (no --force) is allowed — the floor is for irreversibility."""
    v = _run("bash", {"command": "git push origin main"})
    assert v is None


def test_force_push_to_feature_branch_is_allowed():
    v = _run("bash", {"command": "git push --force origin feature/dark-mode"})
    assert v is None


# ── rm -rf outside the project ──────────────────────────────────────────


def test_rm_rf_outside_project_is_floor_violation(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    v = check_floor(
        tool_name="bash",
        args={"command": "rm -rf /etc/something"},
        project_path=str(project),
        settings=None,
    )
    assert v is not None
    assert v.rule == "rm_rf_outside_project"


def test_rm_rf_inside_project_is_allowed(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    (project / "build").mkdir()
    v = check_floor(
        tool_name="bash",
        args={"command": "rm -rf build/"},
        project_path=str(project),
        settings=None,
    )
    assert v is None


def test_rm_with_relative_path_resolves_against_project(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    # Relative path → resolves inside the project, allowed
    v = check_floor(
        tool_name="bash",
        args={"command": "rm -r dist"},
        project_path=str(project),
        settings=None,
    )
    assert v is None


def test_rm_without_recursive_flag_does_not_trigger_rule():
    """`rm foo.txt` is mundane; only -rf-style invocations are gated."""
    v = _run("bash", {"command": "rm foo.txt"})
    assert v is None


# ── Writes to OS-config dirs ───────────────────────────────────────────


def test_file_write_to_ssh_config_is_floor_violation():
    v = _run("file_write", {"path": str(Path.home() / ".ssh" / "config"),
                            "content": "Host x"})
    assert v is not None
    assert v.rule == "writes_to_protected_path"


def test_file_edit_to_aws_credentials_is_floor_violation():
    v = _run("file_edit", {"path": str(Path.home() / ".aws" / "credentials"),
                            "old_text": "x", "new_text": "y"})
    assert v is not None
    assert v.rule == "writes_to_protected_path"


def test_normal_project_file_write_is_allowed(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    v = check_floor(
        tool_name="file_write",
        args={"path": str(project / "src" / "main.py"), "content": "print(1)"},
        project_path=str(project),
        settings=None,
    )
    assert v is None


# ── Destructive SQL ─────────────────────────────────────────────────────


def test_drop_table_on_prod_db_is_floor_violation():
    v = _run("bash", {"command": 'psql prod_db -c "DROP TABLE users"'})
    assert v is not None
    assert v.rule == "destructive_sql_non_test"


def test_drop_table_on_test_db_is_allowed():
    """The 'test' marker keyword sidesteps the rule — it's the agreed escape hatch."""
    v = _run("bash", {"command": 'psql test_db -c "DROP TABLE users"'})
    assert v is None


def test_truncate_on_prod_is_floor_violation():
    v = _run("bash", {"command": 'mysql -u root prod -e "TRUNCATE TABLE orders"'})
    assert v is not None
    assert v.rule == "destructive_sql_non_test"


# ── External message sending ────────────────────────────────────────────


def test_slack_send_is_floor_violation():
    v = _run("slack_send_message", {"channel": "#general", "text": "hi"})
    assert v is not None
    assert v.rule == "external_message_send"


def test_email_send_is_floor_violation():
    v = _run("email_send", {"to": "x@example.com", "subject": "y", "body": "z"})
    assert v is not None
    assert v.rule == "external_message_send"


def test_browser_click_post_on_twitter_is_flagged():
    v = _run("browser_click", {"text": "Post", "url": "https://twitter.com/compose"})
    assert v is not None
    assert v.rule == "external_message_send"


def test_browser_click_send_on_unrelated_site_allowed():
    v = _run("browser_click", {"text": "Send query", "url": "https://docs.example.com/page"})
    assert v is None


# ── Budget cap ──────────────────────────────────────────────────────────


def test_budget_check_inert_when_no_running_spend_supplied():
    """Budget rule is a no-op until the orchestrator threads through running spend."""
    v = _run("bash", {"command": "echo hi"})
    assert v is None  # no _running_spend_usd in args


def test_budget_check_fires_at_cap():
    v = check_floor(
        tool_name="bash",
        args={"command": "echo hi", "_running_spend_usd": DEFAULT_BUDGET_USD_MAX + 0.01},
        project_path="/tmp",
        settings=None,
    )
    assert v is not None
    assert v.rule == "budget_exceeded"


def test_budget_check_passes_when_under_cap():
    v = check_floor(
        tool_name="bash",
        args={"command": "echo hi", "_running_spend_usd": DEFAULT_BUDGET_USD_MAX - 0.01},
        project_path="/tmp",
        settings=None,
    )
    assert v is None


# ── Settings overrides ─────────────────────────────────────────────────


class _FakeSettings:
    """Minimal SettingsManager-like double for from_settings."""
    def __init__(self, **vals):
        self._vals = vals

    def get(self, section, key, default=None):
        return self._vals.get(f"{section}.{key}", default)


def test_custom_protected_branches_via_settings():
    settings = _FakeSettings(**{
        "general.autonomy_protected_branches": ["develop", "staging"],
    })
    v = check_floor(
        tool_name="bash",
        args={"command": "git push --force origin develop"},
        project_path="/tmp/proj",
        settings=settings,
    )
    assert v is not None
    assert v.rule == "protected_branch_force_push"


def test_custom_budget_via_settings():
    settings = _FakeSettings(**{"general.budget_usd_max": 1.00})
    v = check_floor(
        tool_name="bash",
        args={"command": "echo hi", "_running_spend_usd": 1.50},
        project_path="/tmp",
        settings=settings,
    )
    assert v is not None
    assert v.rule == "budget_exceeded"


# ── Sanity: routine actions never fire any rule ────────────────────────


def test_routine_actions_pass_through():
    """Normal agent flow shouldn't get prompted."""
    routines = [
        ("file_read", {"path": "src/main.py"}),
        ("file_write", {"path": "src/new.py", "content": "x"}),  # inside project
        ("bash", {"command": "npm install"}),
        ("bash", {"command": "pytest tests/"}),
        ("bash", {"command": "git status"}),
        ("bash", {"command": "git commit -m 'fix'"}),
        ("bash", {"command": "git push origin feature/x"}),
        ("glob", {"pattern": "**/*.py"}),
        ("grep", {"pattern": "TODO"}),
    ]
    for tool, args in routines:
        v = check_floor(tool_name=tool, args=args, project_path="/tmp/proj", settings=None)
        assert v is None, f"routine action {tool} should pass: got {v}"

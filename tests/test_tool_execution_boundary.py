"""Security regressions for the workspace tool-execution boundary."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from resonant_client.engine.sandbox import PathSandbox, SandboxViolation
from resonant_client.engine.session import Session, ToolBoundaryViolation
from resonant_client.engine.tools import ToolResult, _exec_batch, _exec_grep


class _Backend:
    name = "ollama"
    model = "test"


def _session(project: Path) -> Session:
    session = Session(_Backend(), auto_approve=True)
    session.project_path = str(project)
    session.sandbox = PathSandbox(str(project))
    return session


def test_grep_pattern_is_not_rewritten_as_a_project_path(tmp_path):
    prepared = _session(tmp_path)._prepare_workspace_tool_args(
        "grep", {"pattern": "needle"}
    )

    assert prepared["pattern"] == "needle"
    assert prepared["path"] == os.path.normcase(os.path.realpath(tmp_path))


@pytest.mark.parametrize("tool_name", ["glob", "grep"])
def test_search_root_cannot_escape_sandbox(tmp_path, tool_name):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    with pytest.raises(SandboxViolation):
        _session(project)._prepare_workspace_tool_args(
            tool_name, {"pattern": "x", "path": str(outside)}
        )


def test_absolute_glob_prefix_cannot_escape_sandbox(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    with pytest.raises(SandboxViolation):
        _session(project)._prepare_workspace_tool_args(
            "glob", {"pattern": str(outside / "**" / "*.py")}
        )


def test_relative_glob_traversal_cannot_escape_sandbox(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()

    with pytest.raises(SandboxViolation):
        _session(project)._prepare_workspace_tool_args(
            "glob", {"pattern": "../outside/*.py"}
        )


def test_grep_file_glob_cannot_supply_a_path(tmp_path):
    with pytest.raises(ToolBoundaryViolation):
        _session(tmp_path)._prepare_workspace_tool_args(
            "grep", {"pattern": "x", "glob": "../outside/*.py"}
        )


def test_git_cwd_defaults_to_session_working_directory(tmp_path):
    prepared = _session(tmp_path)._prepare_workspace_tool_args("git_status", {})
    assert prepared["cwd"] == os.path.normcase(os.path.realpath(tmp_path))


def test_batch_rejects_mutating_child_before_execution(tmp_path):
    with pytest.raises(ToolBoundaryViolation):
        _session(tmp_path)._prepare_workspace_tool_args(
            "batch",
            {"calls": [{"name": "file_write", "arguments": {"path": "x", "content": "x"}}]},
        )


def test_batch_rejects_child_missing_from_specialist_allowlist(tmp_path):
    session = _session(tmp_path)
    session._allowed_tools = [{"function": {"name": "file_read"}}]

    with pytest.raises(ToolBoundaryViolation):
        session._prepare_workspace_tool_args(
            "batch",
            {"calls": [{"name": "grep", "arguments": {"pattern": "x"}}]},
        )


def test_batch_forwards_project_context_to_safe_children(tmp_path):
    settings = object()
    calls = [{"name": "file_read", "arguments": {"path": str(tmp_path / "x")}}]

    with patch(
        "resonant_client.engine.tools.execute_tool",
        return_value=ToolResult("ok"),
    ) as execute:
        result = _exec_batch(
            {"calls": calls},
            time.time(),
            project_path=str(tmp_path),
            settings=settings,
        )

    assert not result.is_error
    assert execute.call_args.kwargs["project_path"] == str(tmp_path)
    assert execute.call_args.kwargs["settings"] is settings


def test_grep_uses_argv_without_a_shell(tmp_path):
    hostile = 'needle" & echo injected & "'
    with patch(
        "resonant_client.engine.tools._run_subprocess_with_cancel",
        return_value=(0, b"", b"", False),
    ) as run:
        _exec_grep(
            {"pattern": hostile, "path": str(tmp_path)},
            time.time(),
        )

    command = run.call_args.args[0]
    assert isinstance(command, list)
    assert run.call_args.kwargs["shell"] is False
    assert any(hostile in arg for arg in command)


def test_symlink_target_outside_project_is_blocked(tmp_path):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    link = project / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(SandboxViolation):
        PathSandbox(str(project)).validate_path(str(link / "payload.txt"))

"""Security regressions for the workspace tool-execution boundary."""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from resonant_client.engine.sandbox import PathSandbox, SandboxViolation
from resonant_client.engine.session import Session, ToolBoundaryViolation
from resonant_client.engine.tools import (
    ToolResult,
    _build_grep_command,
    _exec_batch,
    _exec_grep,
)


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


# ---------------------------------------------------------------------------
# grep backend selection
# ---------------------------------------------------------------------------


def _with_ripgrep(path):
    return patch("resonant_client.engine.tools._ripgrep_executable", return_value=path)


def test_grep_prefers_ripgrep_when_available():
    with _with_ripgrep("/usr/bin/rg"):
        command = _build_grep_command("needle", "src", "")

    assert command[0] == "/usr/bin/rg"
    assert command[-2:] == ["--", "src"]
    # Dotfile directories like .github/ are working files, not noise.
    assert "--hidden" in command
    assert command[command.index("--glob") + 1] == "!.git/"


def test_grep_passes_file_glob_through_to_ripgrep():
    with _with_ripgrep("/usr/bin/rg"):
        command = _build_grep_command("needle", "src", "*.py")

    globs = [command[i + 1] for i, arg in enumerate(command) if arg == "--glob"]
    assert globs == ["!.git/", "*.py"]


def test_grep_pattern_starting_with_dash_is_not_read_as_a_flag():
    with _with_ripgrep("/usr/bin/rg"):
        command = _build_grep_command("--recursive", "src", "")

    # `-e` is what makes this safe; without it ripgrep would consume the
    # pattern as an option and search for nothing.
    assert command[command.index("-e") + 1] == "--recursive"


def test_grep_falls_back_to_findstr_on_windows_without_ripgrep():
    with _with_ripgrep(None), patch("resonant_client.engine.tools.sys.platform", "win32"):
        command = _build_grep_command("needle", "src", "")

    assert command[0] == "findstr"


def test_grep_falls_back_to_posix_grep_without_ripgrep():
    with _with_ripgrep(None), patch("resonant_client.engine.tools.sys.platform", "linux"):
        command = _build_grep_command("needle", "src", "*.py")

    assert command[0] == "grep"
    assert "--include=*.py" in command
    assert command[-2:] == ["needle", "src"]


def test_bundled_ripgrep_wins_over_whatever_is_on_path(tmp_path):
    """A packaged install ships a pinned, verified rg; it must be preferred so
    every user gets the same search behaviour."""
    from resonant_client.engine.tools import _ripgrep_executable

    bundle = tmp_path / "_internal"
    bundle.mkdir()
    name = "rg.exe" if os.name == "nt" else "rg"
    bundled = bundle / name
    bundled.write_text("", encoding="utf-8")

    _ripgrep_executable.cache_clear()
    try:
        with patch("resonant_client.engine.tools.sys._MEIPASS", str(bundle), create=True), \
             patch("resonant_client.engine.tools.shutil.which", return_value="/usr/bin/rg"):
            assert _ripgrep_executable() == str(bundled)
    finally:
        _ripgrep_executable.cache_clear()


def test_a_source_checkout_prefers_a_fetched_binary(tmp_path):
    """A developer who ran the fetch script gets the same rg the bundle ships,
    so `grep` behaves identically here and in a packaged install."""
    from resonant_client.engine.tools import _ripgrep_executable

    vendored = tmp_path / "vendored"
    vendored.mkdir()
    name = "rg.exe" if os.name == "nt" else "rg"
    (vendored / name).write_text("", encoding="utf-8")

    _ripgrep_executable.cache_clear()
    try:
        with patch("resonant_client.engine.tools._VENDORED_RIPGREP_DIR", vendored), \
             patch("resonant_client.engine.tools.shutil.which", return_value="/usr/bin/rg"):
            assert _ripgrep_executable() == str(vendored / name)
    finally:
        _ripgrep_executable.cache_clear()


def test_a_source_checkout_without_a_fetched_binary_uses_path(tmp_path):
    """`sys._MEIPASS` only exists inside a bundle; nothing else should change.

    The vendored directory is patched to an empty one rather than left to the
    real repo, so this asserts the same thing whether or not the developer
    running it has fetched ripgrep.
    """
    from resonant_client.engine.tools import _ripgrep_executable

    _ripgrep_executable.cache_clear()
    try:
        with patch("resonant_client.engine.tools._VENDORED_RIPGREP_DIR", tmp_path / "absent"), \
             patch("resonant_client.engine.tools.shutil.which", return_value="/usr/bin/rg"):
            assert _ripgrep_executable() == "/usr/bin/rg"
    finally:
        _ripgrep_executable.cache_clear()


def test_a_bundle_without_ripgrep_still_falls_back(tmp_path):
    """Belt and braces: the bundle policy gate should prevent this, but a
    missing binary must degrade rather than crash the tool."""
    from resonant_client.engine.tools import _ripgrep_executable

    empty = tmp_path / "_internal"
    empty.mkdir()

    _ripgrep_executable.cache_clear()
    try:
        with patch("resonant_client.engine.tools.sys._MEIPASS", str(empty), create=True), \
             patch("resonant_client.engine.tools._VENDORED_RIPGREP_DIR", tmp_path / "absent"), \
             patch("resonant_client.engine.tools.shutil.which", return_value=None):
            assert _ripgrep_executable() is None
    finally:
        _ripgrep_executable.cache_clear()


def test_grep_finds_real_matches_through_the_selected_backend(tmp_path):
    """End-to-end against whatever backend this machine actually has."""
    (tmp_path / "hit.py").write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    (tmp_path / "miss.py").write_text("gamma = 3\n", encoding="utf-8")

    result = _exec_grep({"pattern": "beta", "path": str(tmp_path)}, time.time())

    assert not result.is_error
    assert "hit.py" in result.output
    assert "miss.py" not in result.output


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

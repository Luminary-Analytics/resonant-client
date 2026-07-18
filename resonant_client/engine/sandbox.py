"""
Path Sandboxing for Resonant Agent Sessions.

Ensures file operations stay within the project directory.
Inspired by Codex CLI's approach: more autonomy = tighter sandbox.
"""

from __future__ import annotations

import os
from typing import Optional

# Tools classified as read-only (safe even in suggest mode)
READ_ONLY_TOOLS = frozenset({
    "file_read", "glob", "grep",
    "skill_view",
    "computer_screenshot",
    "git_status", "git_diff", "git_log",
    "monitors_list", "clipboard_read", "process_list",
    "accessibility_tree", "screen_diff",
})

# Tools that write files (need auto-edit or higher)
FILE_WRITE_TOOLS = frozenset({
    "file_write", "file_edit",
})

# Tools that execute commands (need full-auto or explicit approval)
EXEC_TOOLS = frozenset({
    "bash", "batch",
})


class SandboxViolation(Exception):
    """Raised when a tool tries to access a path outside the sandbox."""

    def __init__(self, path: str, sandbox_root: str):
        self.path = path
        self.sandbox_root = sandbox_root
        super().__init__(
            f"Sandbox violation: '{path}' is outside project directory '{sandbox_root}'"
        )


class PathSandbox:
    """
    Validates that file operations stay within the project directory.

    When enabled, all file paths are checked before tool execution.
    Relative paths are resolved against the project root.
    Absolute paths must fall within the project tree.

    The sandbox also classifies tools by their risk level for use
    with the three-tier autonomy model.
    """

    def __init__(
        self,
        project_path: str,
        allowed_dirs: Optional[list[str]] = None,
        enabled: bool = True,
    ):
        # Resolve symlinks/junctions at the boundary, not only lexical ``..``
        # components.  ``abspath`` alone lets ``project/link/file`` escape when
        # ``link`` points outside the project.
        self.project_path = self._canonical_path(project_path)
        self.allowed_dirs = [
            self._canonical_path(d)
            for d in (allowed_dirs or [])
        ]
        self.enabled = enabled

    def validate_path(self, path: str) -> str:
        """
        Validate and resolve a file path.

        - Relative paths are resolved against project_path
        - Absolute paths must be within project_path or allowed_dirs
        - Returns the resolved absolute path
        - Raises SandboxViolation if path escapes the sandbox
        """
        if not self.enabled:
            return path

        # Resolve the path
        if os.path.isabs(path):
            resolved = self._canonical_path(path)
        else:
            resolved = self._canonical_path(os.path.join(self.project_path, path))

        # Check if path is within allowed boundaries
        if self._is_within_bounds(resolved):
            return resolved

        raise SandboxViolation(path, self.project_path)

    def validate_bash_cwd(self, cwd: str) -> str:
        """Validate that a bash working directory is within the sandbox."""
        if not self.enabled:
            return cwd

        resolved = self._canonical_path(cwd)
        if self._is_within_bounds(resolved):
            return resolved

        raise SandboxViolation(cwd, self.project_path)

    def _is_within_bounds(self, resolved_path: str) -> bool:
        """Check if a resolved path falls within project_path or allowed_dirs."""
        candidate = self._canonical_path(resolved_path)
        for root in (self.project_path, *self.allowed_dirs):
            try:
                if os.path.commonpath((candidate, root)) == root:
                    return True
            except ValueError:
                # Different Windows drives have no common path.
                continue
        return False

    @staticmethod
    def _canonical_path(path: str) -> str:
        """Return a normalized, symlink-aware path for boundary checks."""
        return os.path.normcase(os.path.realpath(os.path.abspath(path)))

    def validate_glob_pattern(
        self,
        pattern: str,
        *,
        base_path: Optional[str] = None,
    ) -> str:
        """Validate the non-wildcard prefix of a glob pattern.

        Both absolute patterns and relative ``..`` traversal can replace or
        escape the independently validated base directory.  Resolve the static
        portion of the effective pattern and keep it within the sandbox.
        """
        if not self.enabled or not pattern:
            return pattern

        effective_pattern = pattern
        if not os.path.isabs(effective_pattern):
            effective_pattern = os.path.join(base_path or self.project_path, pattern)

        normalized_pattern = os.path.normpath(effective_pattern)
        parts = normalized_pattern.split(os.sep)
        prefix_parts: list[str] = []
        for part in parts:
            if any(char in part for char in ("*", "?", "[")):
                break
            prefix_parts.append(part)
        prefix = os.sep.join(prefix_parts) or os.path.dirname(normalized_pattern)
        # Preserve the root/drive when splitting an absolute path.
        drive, _ = os.path.splitdrive(normalized_pattern)
        if drive and not os.path.isabs(prefix):
            prefix = drive + os.sep + prefix
        elif normalized_pattern.startswith(os.sep) and not prefix.startswith(os.sep):
            prefix = os.sep + prefix
        self.validate_path(prefix)
        return pattern

    @staticmethod
    def is_read_only_tool(tool_name: str) -> bool:
        """Check if a tool is read-only (safe for suggest mode)."""
        return tool_name in READ_ONLY_TOOLS

    @staticmethod
    def is_file_write_tool(tool_name: str) -> bool:
        """Check if a tool writes files (needs auto-edit or higher)."""
        return tool_name in FILE_WRITE_TOOLS

    @staticmethod
    def is_exec_tool(tool_name: str) -> bool:
        """Check if a tool executes commands (needs full-auto or explicit approval)."""
        return tool_name in EXEC_TOOLS

    def __repr__(self) -> str:
        return f"PathSandbox(project_path={self.project_path!r}, enabled={self.enabled})"

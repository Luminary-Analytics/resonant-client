"""
Path Sandboxing for Resonant Agent Sessions.

Ensures file operations stay within the project directory.
Inspired by Codex CLI's approach: more autonomy = tighter sandbox.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Tools classified as read-only (safe even in suggest mode)
READ_ONLY_TOOLS = frozenset({
    "file_read", "glob", "grep",
    "browser_read", "browser_screenshot", "browser_js",
    "browser_scroll", "browser_hover", "browser_wait", "browser_back", "browser_tabs",
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
        self.project_path = os.path.normpath(os.path.abspath(project_path))
        self.allowed_dirs = [
            os.path.normpath(os.path.abspath(d))
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
            resolved = os.path.normpath(os.path.abspath(path))
        else:
            resolved = os.path.normpath(os.path.join(self.project_path, path))

        # Check if path is within allowed boundaries
        if self._is_within_bounds(resolved):
            return resolved

        raise SandboxViolation(path, self.project_path)

    def validate_bash_cwd(self, cwd: str) -> str:
        """Validate that a bash working directory is within the sandbox."""
        if not self.enabled:
            return cwd

        resolved = os.path.normpath(os.path.abspath(cwd))
        if self._is_within_bounds(resolved):
            return resolved

        raise SandboxViolation(cwd, self.project_path)

    def _is_within_bounds(self, resolved_path: str) -> bool:
        """Check if a resolved path falls within project_path or allowed_dirs."""
        # Normalize for comparison (case-insensitive on Windows)
        norm = resolved_path.lower() if os.name == "nt" else resolved_path
        project_norm = self.project_path.lower() if os.name == "nt" else self.project_path

        # Check project directory
        if norm == project_norm or norm.startswith(project_norm + os.sep):
            return True

        # Check additional allowed directories
        for allowed in self.allowed_dirs:
            allowed_norm = allowed.lower() if os.name == "nt" else allowed
            if norm == allowed_norm or norm.startswith(allowed_norm + os.sep):
                return True

        return False

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

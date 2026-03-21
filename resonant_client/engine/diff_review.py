"""
Diff Review Engine for Resonant.

Generates rich diff previews for tool calls that modify files,
enabling users to review changes before they're applied.
"""

import difflib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class DiffHunk:
    """A single contiguous change in a diff."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[str]  # Prefixed with +, -, or space
    context: str = ""  # Brief description of the change


@dataclass
class DiffReview:
    """A complete diff review for a tool call."""
    tool_name: str
    file_path: str = ""
    action: str = ""  # "create", "edit", "delete", "execute"
    risk_level: str = "low"  # "low", "medium", "high"
    summary: str = ""
    hunks: list[DiffHunk] = field(default_factory=list)
    old_content: str = ""
    new_content: str = ""
    unified_diff: str = ""
    warnings: list[str] = field(default_factory=list)
    command: str = ""  # For bash commands

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "file_path": self.file_path,
            "action": self.action,
            "risk_level": self.risk_level,
            "summary": self.summary,
            "hunks": [
                {
                    "old_start": h.old_start,
                    "old_count": h.old_count,
                    "new_start": h.new_start,
                    "new_count": h.new_count,
                    "lines": h.lines,
                    "context": h.context,
                }
                for h in self.hunks
            ],
            "unified_diff": self.unified_diff,
            "warnings": self.warnings,
            "command": self.command,
        }


# Dangerous bash patterns
_DANGEROUS_PATTERNS = [
    ("rm -rf", "Recursive force delete"),
    ("rm -f", "Force delete"),
    ("rmdir", "Remove directory"),
    ("> /dev/", "Write to device file"),
    ("dd if=", "Low-level disk write"),
    ("mkfs", "Format filesystem"),
    ("chmod 777", "World-writable permissions"),
    ("curl | sh", "Pipe remote script to shell"),
    ("curl | bash", "Pipe remote script to shell"),
    ("wget -O - | sh", "Pipe remote script to shell"),
    ("eval ", "Dynamic code execution"),
    ("sudo ", "Elevated privileges"),
    (":(){:|:&};:", "Fork bomb"),
    ("git push --force", "Force push (destructive)"),
    ("git reset --hard", "Hard reset (loses changes)"),
    ("DROP TABLE", "SQL table deletion"),
    ("DROP DATABASE", "SQL database deletion"),
    ("TRUNCATE ", "SQL data deletion"),
    ("format ", "Disk format"),
    ("shutdown", "System shutdown"),
    ("reboot", "System reboot"),
    ("kill -9", "Force kill process"),
    ("pkill", "Kill processes by name"),
]

# File patterns that are risky to modify
_SENSITIVE_FILES = [
    ".env", ".env.local", ".env.production",
    "credentials", "secrets", "token",
    ".ssh/", ".gpg/", ".gnupg/",
    "id_rsa", "id_ed25519",
    "passwd", "shadow",
    ".git/config", ".gitconfig",
]


def generate_review(tool_name: str, tool_args: dict, project_path: str = "") -> Optional[DiffReview]:
    """Generate a diff review for a tool call.

    Args:
        tool_name: Name of the tool being called
        tool_args: Tool arguments dict
        project_path: Current project path for resolving relative paths

    Returns:
        DiffReview object, or None if no review is needed
    """
    if tool_name == "file_edit":
        return _review_file_edit(tool_args, project_path)
    elif tool_name == "file_write":
        return _review_file_write(tool_args, project_path)
    elif tool_name == "bash":
        return _review_bash(tool_args)
    elif tool_name == "file_read":
        return None  # Read-only, no review needed
    elif tool_name == "glob" or tool_name == "grep":
        return None  # Search-only
    else:
        # Unknown tool — basic review
        return DiffReview(
            tool_name=tool_name,
            action="execute",
            risk_level="medium",
            summary=f"Execute {tool_name}",
        )


def _review_file_edit(args: dict, project_path: str) -> DiffReview:
    """Review a file_edit operation."""
    file_path = str(args.get("path", args.get("file_path", "")) or "")
    old_text = str(args.get("old_text", args.get("old_string", "")) or "")
    new_text = str(args.get("new_text", args.get("new_string", "")) or "")

    review = DiffReview(
        tool_name="file_edit",
        file_path=file_path,
        action="edit",
        risk_level="low",
    )

    # Check for sensitive files
    _check_sensitive_path(file_path, review)

    # Try to read the actual file for full context
    full_path = _resolve_path(file_path, project_path)
    try:
        if full_path and os.path.exists(full_path):
            with open(full_path, "rb") as f:
                raw = f.read()
            original = raw.decode("utf-8", errors="replace")

            # Normalise old_text/new_text line endings to match file
            if "\r\n" in original and "\r\n" not in old_text:
                old_text = old_text.replace("\n", "\r\n")
                new_text = new_text.replace("\n", "\r\n")
            elif "\r\n" not in original and "\r\n" in old_text:
                old_text = old_text.replace("\r\n", "\n")
                new_text = new_text.replace("\r\n", "\n")

            # Apply the edit to generate the new version
            if old_text in original:
                modified = original.replace(old_text, new_text, 1)
                review.old_content = original
                review.new_content = modified

                # Generate unified diff
                old_lines = original.splitlines(keepends=True)
                new_lines = modified.splitlines(keepends=True)
                diff = difflib.unified_diff(
                    old_lines, new_lines,
                    fromfile=f"a/{file_path}",
                    tofile=f"b/{file_path}",
                    n=3,
                )
                diff_text = "".join(diff)
                review.unified_diff = diff_text
                review.hunks = _parse_hunks(diff_text)

                # Summary
                added = sum(1 for h in review.hunks for l in h.lines if l.startswith("+"))
                removed = sum(1 for h in review.hunks for l in h.lines if l.startswith("-"))
                review.summary = f"Edit {file_path}: +{added} -{removed} lines"
            else:
                review.summary = f"Edit {file_path} (old_text not found in file)"
                review.warnings.append("The text to replace was not found in the file")
                review.risk_level = "medium"
        else:
            # File doesn't exist yet
            review.summary = f"Edit {file_path} (file not found)"
            review.warnings.append("File does not exist")
    except Exception as e:
        review.summary = f"Edit {file_path}"
        review.warnings.append(f"Could not read file: {e}")

    # Fallback: just diff old_text vs new_text
    if not review.unified_diff and old_text and new_text:
        old_lines = old_text.splitlines(keepends=True)
        new_lines = new_text.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, n=3)
        review.unified_diff = "".join(diff)
        review.hunks = _parse_hunks(review.unified_diff)
        if not review.summary:
            review.summary = f"Edit {file_path}"

    return review


def _review_file_write(args: dict, project_path: str) -> DiffReview:
    """Review a file_write (create/overwrite) operation."""
    file_path = str(args.get("path", args.get("file_path", "")) or "")
    content = str(args.get("content", "") or "")

    review = DiffReview(
        tool_name="file_write",
        file_path=file_path,
        new_content=content,
    )

    _check_sensitive_path(file_path, review)

    full_path = _resolve_path(file_path, project_path)
    if full_path and os.path.exists(full_path):
        # Overwrite — show diff against existing content
        review.action = "overwrite"
        review.risk_level = "medium"
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                original = f.read()
            review.old_content = original

            old_lines = original.splitlines(keepends=True)
            new_lines = content.splitlines(keepends=True)
            diff = difflib.unified_diff(
                old_lines, new_lines,
                fromfile=f"a/{file_path}",
                tofile=f"b/{file_path}",
                n=3,
            )
            review.unified_diff = "".join(diff)
            review.hunks = _parse_hunks(review.unified_diff)
            review.summary = f"Overwrite {file_path} ({len(content)} bytes)"
        except Exception:
            review.summary = f"Overwrite {file_path}"
    else:
        # New file
        review.action = "create"
        review.risk_level = "low"
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        review.summary = f"Create {file_path} ({lines} lines)"
        # Show as all-additions diff
        new_lines = content.splitlines(keepends=True)
        diff = difflib.unified_diff(
            [], new_lines,
            fromfile="/dev/null",
            tofile=f"b/{file_path}",
            n=0,
        )
        review.unified_diff = "".join(diff)
        review.hunks = _parse_hunks(review.unified_diff)

    return review


def _review_bash(args: dict) -> DiffReview:
    """Review a bash command."""
    command = str(args.get("command", "") or "")

    review = DiffReview(
        tool_name="bash",
        action="execute",
        command=command,
        risk_level="low",
        summary=f"Run: {command[:100]}{'...' if len(command) > 100 else ''}",
    )

    # Check for dangerous patterns
    cmd_lower = command.lower()
    for pattern, description in _DANGEROUS_PATTERNS:
        if pattern.lower() in cmd_lower:
            review.risk_level = "high"
            review.warnings.append(f"⚠ {description}: contains '{pattern}'")

    # Medium risk for commands that modify things
    if review.risk_level == "low":
        modify_patterns = [
            "mv ", "cp ", "mkdir ", "touch ", "chmod ", "chown ",
            "pip install", "npm install", "apt install", "brew install",
            "git commit", "git merge", "git rebase", "git checkout",
        ]
        for pattern in modify_patterns:
            if pattern in cmd_lower:
                review.risk_level = "medium"
                break

    return review


def _check_sensitive_path(file_path: str, review: DiffReview):
    """Check if a file path is sensitive and add warnings."""
    path_lower = file_path.lower()
    for pattern in _SENSITIVE_FILES:
        if pattern in path_lower:
            review.warnings.append(f"⚠ Sensitive file: matches pattern '{pattern}'")
            review.risk_level = "high"
            break


def _resolve_path(file_path: str, project_path: str) -> Optional[str]:
    """Resolve a file path relative to the project."""
    if not file_path:
        return None
    if os.path.isabs(file_path):
        return file_path
    if project_path:
        return os.path.join(project_path, file_path)
    return os.path.join(os.getcwd(), file_path)


def _parse_hunks(diff_text: str) -> list[DiffHunk]:
    """Parse a unified diff into DiffHunk objects."""
    import re
    hunks = []
    current_hunk = None

    for line in diff_text.splitlines():
        # Hunk header: @@ -old_start,old_count +new_start,new_count @@
        match = re.match(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)', line)
        if match:
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = DiffHunk(
                old_start=int(match.group(1)),
                old_count=int(match.group(2) or 1),
                new_start=int(match.group(3)),
                new_count=int(match.group(4) or 1),
                lines=[],
                context=match.group(5).strip(),
            )
            continue

        if current_hunk is not None:
            if line.startswith("+") or line.startswith("-") or line.startswith(" "):
                current_hunk.lines.append(line)

    if current_hunk:
        hunks.append(current_hunk)

    return hunks

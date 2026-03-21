"""
Git Worktree Manager for Resonant Engine.

Creates isolated git worktrees for sub-agents so they can work
on code without conflicting with the main working directory.
"""

import logging
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class WorktreeManager:
    """Manages git worktrees for isolated sub-agent sessions."""

    def __init__(self, project_path: str | Path | None = None):
        self._project_path = Path(project_path or os.getcwd())
        self._active: dict[str, dict] = {}  # worktree_id -> {path, branch}

    def _git(self, *args: str, cwd: str | Path | None = None) -> tuple[int, str]:
        """Run a git command."""
        try:
            result = subprocess.run(
                ["git"] + list(args),
                capture_output=True, text=True, timeout=30,
                cwd=str(cwd or self._project_path),
                shell=(sys.platform == "win32"),
            )
            return result.returncode, (result.stdout + result.stderr).strip()
        except Exception as e:
            return 1, str(e)

    def is_git_repo(self) -> bool:
        """Check if the project is a git repo."""
        rc, _ = self._git("rev-parse", "--git-dir")
        return rc == 0

    def create(self, branch_name: str = "") -> Optional[Path]:
        """Create a new worktree with an isolated branch.

        Args:
            branch_name: Optional branch name. If empty, generates one.

        Returns:
            Path to the worktree directory, or None on failure.
        """
        if not self.is_git_repo():
            logger.error("Not a git repository")
            return None

        wt_id = uuid.uuid4().hex[:8]
        if not branch_name:
            branch_name = f"resonant-wt-{wt_id}"

        # Worktree path: project/../.resonant-worktrees/<id>
        wt_dir = self._project_path.parent / ".resonant-worktrees" / wt_id
        wt_dir.parent.mkdir(parents=True, exist_ok=True)

        # Create worktree with new branch from current HEAD
        rc, output = self._git("worktree", "add", "-b", branch_name, str(wt_dir))
        if rc != 0:
            # Try without -b if branch already exists
            rc, output = self._git("worktree", "add", str(wt_dir), branch_name)
            if rc != 0:
                logger.error(f"Failed to create worktree: {output}")
                return None

        self._active[wt_id] = {
            "path": str(wt_dir),
            "branch": branch_name,
        }

        logger.info(f"Created worktree at {wt_dir} on branch {branch_name}")
        return wt_dir

    def merge(self, worktree_id: str, target_branch: str = "") -> str:
        """Merge a worktree's changes back to the main branch.

        Args:
            worktree_id: The worktree ID
            target_branch: Branch to merge into (default: current branch)

        Returns:
            Merge output or error message.
        """
        info = self._active.get(worktree_id)
        if not info:
            return f"Worktree {worktree_id} not found"

        branch = info["branch"]

        if not target_branch:
            rc, target_branch = self._git("branch", "--show-current")
            if rc != 0:
                return "Could not determine current branch"
            target_branch = target_branch.strip()

        # Merge the worktree branch
        rc, output = self._git("merge", branch, "--no-edit")
        if rc != 0:
            return f"Merge failed: {output}"

        # Clean up
        self.discard(worktree_id)
        return output

    def discard(self, worktree_id: str) -> bool:
        """Remove a worktree and its branch."""
        info = self._active.pop(worktree_id, None)
        if not info:
            return False

        wt_path = info["path"]
        branch = info["branch"]

        # Remove worktree
        rc, output = self._git("worktree", "remove", wt_path, "--force")
        if rc != 0:
            logger.warning(f"Failed to remove worktree: {output}")

        # Delete branch
        rc, output = self._git("branch", "-D", branch)
        if rc != 0:
            logger.warning(f"Failed to delete branch: {output}")

        return True

    def list_worktrees(self) -> list[dict]:
        """List all active worktrees."""
        rc, output = self._git("worktree", "list", "--porcelain")
        if rc != 0:
            return []

        worktrees = []
        current = {}
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[9:]}
            elif line.startswith("HEAD "):
                current["head"] = line[5:]
            elif line.startswith("branch "):
                current["branch"] = line[7:]
            elif line == "bare":
                current["bare"] = True
            elif line == "detached":
                current["detached"] = True

        if current:
            worktrees.append(current)

        return worktrees

    @property
    def active_worktrees(self) -> dict[str, dict]:
        return self._active

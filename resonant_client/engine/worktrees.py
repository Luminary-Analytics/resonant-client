"""Git-worktree isolation and serialized integration for writing agents."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from resonant_client.processes import background_process_kwargs

from .artifacts import project_state_dir


class WorktreeError(RuntimeError):
    pass


@dataclass(slots=True)
class WorktreeLease:
    agent_id: str
    path: str
    branch: str
    base_ref: str
    created_at: float
    status: str = "active"
    commit: str = ""
    changed_files: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorktreeManager:
    """Create isolated agent branches and merge them one at a time."""

    _integration_lock = threading.RLock()

    def __init__(self, project_path: str | Path, root: str | Path | None = None):
        self.project_path = Path(project_path).expanduser().resolve()
        self.root = Path(root) if root else project_state_dir(self.project_path) / "worktrees"
        self.root.mkdir(parents=True, exist_ok=True)
        self._git_dir = self._discover_git_dir()

    @property
    def available(self) -> bool:
        return self._git_dir is not None

    def create(self, agent_id: str, *, base_ref: str = "HEAD") -> WorktreeLease:
        if not self.available:
            raise WorktreeError("Worktree isolation requires a git repository")
        safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", agent_id).strip(".-") or "agent"
        digest = hashlib.sha256(f"{agent_id}:{time.time_ns()}".encode()).hexdigest()[:8]
        branch = f"resonant/agent/{safe_id}-{digest}"
        destination = (self.root / f"{safe_id}-{digest}").resolve()
        self._assert_under_root(destination)
        result = self._git("worktree", "add", "-b", branch, str(destination), base_ref, check=False)
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git worktree add failed")
        return WorktreeLease(
            agent_id=agent_id,
            path=str(destination),
            branch=branch,
            base_ref=base_ref,
            created_at=time.time(),
        )

    def finalize(
        self,
        lease: WorktreeLease,
        *,
        message: str = "",
    ) -> WorktreeLease:
        worktree = Path(lease.path).resolve()
        self._assert_under_root(worktree)
        changed = self._git_at(worktree, "status", "--porcelain=v1", "-z").stdout
        paths = self._porcelain_paths(changed)
        lease.changed_files = paths
        if not paths:
            lease.status = "unchanged"
            return lease
        self._git_at(worktree, "add", "-A", "--", ".")
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "Resonant Agent")
        env.setdefault("GIT_AUTHOR_EMAIL", "agent@resonant.local")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        commit_message = message or f"Resonant agent {lease.agent_id} handoff"
        self._git_at(worktree, "commit", "-m", commit_message, env=env)
        lease.commit = self._git_at(worktree, "rev-parse", "HEAD").stdout.strip()
        lease.status = "ready"
        return lease

    def integrate(
        self,
        lease: WorktreeLease,
        *,
        validation_commands: Iterable[str] = (),
    ) -> WorktreeLease:
        """Merge a finalized agent branch when the user's checkout is clean.

        Dirty main work is never stashed or reset.  The ready branch remains
        available for explicit review instead of risking user-owned changes.
        """
        if lease.status == "unchanged":
            return lease
        if not lease.commit:
            lease = self.finalize(lease)
        with self._integration_lock:
            dirty = self._git("status", "--porcelain=v1").stdout.strip()
            if dirty:
                lease.status = "awaiting_integration"
                return lease
            result = self._git(
                "merge", "--no-ff", "--no-edit", lease.branch, check=False,
            )
            if result.returncode != 0:
                self._git("merge", "--abort", check=False)
                lease.status = "conflict"
                raise WorktreeError(result.stderr.strip() or "Worktree merge conflicted")
            for command in validation_commands:
                completed = subprocess.run(
                    command,
                    cwd=self.project_path,
                    shell=True,
                    text=True,
                    capture_output=True,
                    check=False,
                    **background_process_kwargs(),
                )
                if completed.returncode != 0:
                    lease.status = "validation_failed"
                    raise WorktreeError(
                        f"Post-merge validation failed: {command}\n"
                        f"{completed.stdout}\n{completed.stderr}".strip()
                    )
            lease.status = "integrated"
        return lease

    def remove(self, lease: WorktreeLease, *, delete_branch: bool = False) -> None:
        target = Path(lease.path).resolve()
        self._assert_under_root(target)
        if target.exists():
            result = self._git("worktree", "remove", "--force", str(target), check=False)
            if result.returncode != 0 and target.exists():
                shutil.rmtree(target)
                self._git("worktree", "prune", check=False)
        if delete_branch and lease.status in {"unchanged", "integrated"}:
            self._git("branch", "-D", lease.branch, check=False)

    def diff(self, lease: WorktreeLease) -> str:
        target = lease.commit or lease.branch
        return self._git("diff", f"{lease.base_ref}...{target}", "--").stdout

    def _discover_git_dir(self) -> Path | None:
        result = self._git("rev-parse", "--git-dir", check=False)
        if result.returncode != 0:
            return None
        value = Path(result.stdout.strip())
        return (self.project_path / value).resolve() if not value.is_absolute() else value.resolve()

    def _assert_under_root(self, path: Path) -> None:
        if path != self.root and self.root not in path.parents:
            raise WorktreeError(f"Refused worktree path outside managed root: {path}")

    @staticmethod
    def _porcelain_paths(raw: str) -> list[str]:
        fields = [field for field in raw.split("\0") if field]
        paths: list[str] = []
        for field in fields:
            value = field[3:] if len(field) > 3 else field
            if value and value not in paths:
                paths.append(value)
        return paths

    def _git(self, *args: str, check: bool = True):
        return self._git_at(self.project_path, *args, check=check)

    @staticmethod
    def _git_at(
        cwd: Path,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ):
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **background_process_kwargs(),
        )
        if check and result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result

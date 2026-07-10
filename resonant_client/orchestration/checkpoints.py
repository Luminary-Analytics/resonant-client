"""Git-backed working-tree checkpoints for autonomous iterations."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


class CheckpointError(RuntimeError):
    pass


class IterationCheckpointStore:
    """Snapshot all tracked and untracked files without moving HEAD."""

    REF_ROOT = "refs/resonant/checkpoints"

    def __init__(self, project_path: str | Path) -> None:
        self.project_path = Path(project_path).resolve()
        git_dir = self._git("rev-parse", "--git-dir").stdout.strip()
        self.git_dir = (self.project_path / git_dir).resolve()

    def create(self, *, intent_id: str, iteration: int, item_id: str = "") -> dict:
        safe_intent = self._safe_component(intent_id or "mission")
        ref = f"{self.REF_ROOT}/{safe_intent}/{max(0, int(iteration)):04d}"
        message = f"Resonant checkpoint {intent_id} iter {iteration} {item_id}".strip()
        commit = self._snapshot_commit(message)
        self._git("update-ref", ref, commit)
        return {
            "ref": ref,
            "commit": commit,
            "intent_id": intent_id,
            "iteration": int(iteration),
            "item_id": item_id,
            "message": message,
        }

    def list(self) -> list[dict]:
        output = self._git(
            "for-each-ref",
            "--sort=-creatordate",
            "--format=%(refname)|%(objectname)|%(creatordate:iso8601)|%(subject)",
            self.REF_ROOT,
        ).stdout
        records = []
        for line in output.splitlines():
            if not line.strip():
                continue
            ref, commit, created, subject = (line.split("|", 3) + ["", "", "", ""])[:4]
            records.append({
                "ref": ref,
                "commit": commit,
                "created_at": created,
                "message": subject,
            })
        return records

    def compare(self, ref: str) -> dict:
        checkpoint = self._resolve_checkpoint(ref)
        current = self._snapshot_commit("Resonant transient checkpoint comparison")
        return {
            "ref": ref,
            "checkpoint": checkpoint,
            "current": current,
            "stat": self._git("diff", "--stat", checkpoint, current, "--").stdout,
            "name_status": self._git(
                "diff", "--name-status", checkpoint, current, "--"
            ).stdout,
        }

    def restore(self, ref: str) -> dict:
        """Restore a checkpoint and preserve current content on a recovery branch."""
        checkpoint = self._resolve_checkpoint(ref)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        recovery_commit = self._snapshot_commit(f"Resonant recovery before restoring {ref}")
        recovery_branch = f"resonant-recovery/{stamp}"
        suffix = 1
        while self._ref_exists(f"refs/heads/{recovery_branch}"):
            suffix += 1
            recovery_branch = f"resonant-recovery/{stamp}-{suffix}"
        self._git("update-ref", f"refs/heads/{recovery_branch}", recovery_commit)

        changed = self._git(
            "diff", "--name-only", "-z", checkpoint, recovery_commit, "--"
        ).stdout.split("\0")
        remove_after = []
        for rel in (value for value in changed if value):
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{checkpoint}:{rel}"],
                cwd=self.project_path,
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode != 0:
                remove_after.append(rel)

        self._git("read-tree", "--reset", "-u", checkpoint)
        for rel in remove_after:
            target = (self.project_path / rel).resolve()
            if self.project_path not in target.parents and target != self.project_path:
                raise CheckpointError(f"Refused to remove path outside project: {rel}")
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()

        head = self._head_commit()
        if head:
            self._git("reset", "--mixed", head)
        return {
            "ref": ref,
            "checkpoint": checkpoint,
            "recovery_branch": recovery_branch,
            "recovery_commit": recovery_commit,
            "changed_paths": [value for value in changed if value],
        }

    def _snapshot_commit(self, message: str) -> str:
        fd, index_name = tempfile.mkstemp(prefix="resonant-index-", dir=self.git_dir)
        os.close(fd)
        Path(index_name).unlink(missing_ok=True)
        env = os.environ.copy()
        env["GIT_INDEX_FILE"] = index_name
        env.setdefault("GIT_AUTHOR_NAME", "Resonant Client")
        env.setdefault("GIT_AUTHOR_EMAIL", "checkpoint@resonant.local")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        try:
            head = self._head_commit()
            if head:
                self._git("read-tree", head, env=env)
            else:
                self._git("read-tree", "--empty", env=env)
            self._git("add", "-A", "--", ".", env=env)
            tree = self._git("write-tree", env=env).stdout.strip()
            args = ["commit-tree", tree, "-m", message]
            if head:
                args.extend(("-p", head))
            return self._git(*args, env=env).stdout.strip()
        finally:
            Path(index_name).unlink(missing_ok=True)
            Path(f"{index_name}.lock").unlink(missing_ok=True)

    def _resolve_checkpoint(self, ref: str) -> str:
        if not ref.startswith(f"{self.REF_ROOT}/"):
            raise CheckpointError("Only Resonant iteration checkpoint refs can be used")
        result = self._git("rev-parse", "--verify", f"{ref}^{{commit}}", check=False)
        if result.returncode != 0:
            raise CheckpointError(f"Checkpoint not found: {ref}")
        return result.stdout.strip()

    def _head_commit(self) -> str:
        result = self._git("rev-parse", "--verify", "HEAD^{commit}", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _ref_exists(self, ref: str) -> bool:
        return self._git("show-ref", "--verify", "--quiet", ref, check=False).returncode == 0

    @staticmethod
    def _safe_component(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
        return safe[:80] or "mission"

    def _git(self, *args: str, env: dict | None = None, check: bool = True):
        result = subprocess.run(
            ["git", *args],
            cwd=self.project_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if check and result.returncode != 0:
            raise CheckpointError(result.stderr.strip() or f"git {' '.join(args)} failed")
        return result

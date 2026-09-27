"""Remove a finished team's writer worktrees and branches from the user's repository.

Writers work in Git worktrees under Lumi's runtime folder
(``~/.lumi/projects/<id>/swarm/worktrees``), on branches the team creates in
the user's repository: ``lumi/team-<writer>`` (``codex/swarm-writer-<writer>``
before the rebrand). While a team runs, waits for review or needs recovery,
they are its evidence and stay. Once the run has ended nothing needs the
writers' worktrees and branches: an applied change is already on the user's
branch, and a change nobody applied was dropped by the owner's decision
(Stop, or a failed team). So they are removed then, and at startup for teams
that ended earlier (the legacy branch prefix included). A stopped or failed
team's combined candidates go too; a completed team keeps them, because its
applied change stays inspectable (Inspect candidate reads that worktree).

This doesn't go through the integration's owned-effect protocol
(integration.py) on purpose. It runs only for runs whose supervisor can start
nothing more, touches only worktree folders under Lumi's runtime root and
branches with a team prefix that the run recorded, and is idempotent: an
interrupted cleanup is repeated at the next start.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from typing import Iterable

from ...processes import background_process_kwargs
from ..artifacts import project_state_dir
from .git_boundary import is_team_branch, trusted_git_executable
from .models import Conflict
from .store import SwarmStore

logger = logging.getLogger(__name__)

TERMINAL_STATES = ("completed", "cancelled", "failed")
# Ended without an applied change: their candidates are discarded too.
ABANDONED_STATES = ("cancelled", "failed")


def leftovers(store: SwarmStore, run_ids: Iterable[str] | None = None) -> tuple[list[str], list[str]]:
    """Worktree folders and writer branches recorded by teams that ended: ``(paths, branches)``."""
    selected = tuple(run_ids) if run_ids is not None else None
    if selected is not None and not selected:
        return [], []
    only = f" AND r.id IN ({','.join('?' for _ in selected)})" if selected else ""
    paths: list[str] = []
    branches: list[str] = []
    with store._connection() as connection:
        for row in connection.execute(
                f"SELECT w.path, w.manifest_json FROM writer_worktrees w JOIN runs r ON r.id=w.run_id "
                f"WHERE r.state IN ({','.join('?' for _ in TERMINAL_STATES)}){only}",
                (*TERMINAL_STATES, *(selected or ()))):
            paths.append(str(row[0]))
            try:
                branch = str(json.loads(row[1] or "{}").get("branch") or "")
            except (TypeError, ValueError):
                branch = ""
            if branch and is_team_branch(branch):
                branches.append(branch)
        for row in connection.execute(
                f"SELECT c.path FROM integration_candidates c JOIN runs r ON r.id=c.run_id "
                f"WHERE r.state IN ({','.join('?' for _ in ABANDONED_STATES)}){only}",
                (*ABANDONED_STATES, *(selected or ()))):
            paths.append(str(row[0]))
    return paths, branches


def clean_finished_teams(store: SwarmStore, workspace: str | Path, *, run_ids: Iterable[str] | None = None,
                         root: str | Path | None = None) -> dict[str, int]:
    """Remove ended teams' worktrees and writer branches; returns what was removed.

    ``run_ids`` limits it to those runs (they still have to have ended);
    without it every ended run in the store is checked.
    """
    removed = {"worktrees": 0, "branches": 0}
    paths, branches = leftovers(store, run_ids)
    if not paths and not branches:
        return removed
    project = Path(workspace).resolve()
    runtime_root = Path(root or project_state_dir(project) / "swarm" / "worktrees").resolve()
    existing = [Path(path) for path in paths if _under(Path(path), runtime_root) and Path(path).exists()]
    if not existing and not branches:
        return removed
    try:
        git = trusted_git_executable(project, runtime_root)
    except Conflict:
        return removed  # No Git now: nothing to ask it; the next start tries again.

    def run(*args: str) -> subprocess.CompletedProcess:
        # Inherited GIT_DIR/INDEX_FILE must not redirect the user's repository.
        environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
        environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
        return subprocess.run([git, "--no-pager", "-c", "core.fsmonitor=false", *args], cwd=project,
                              env=environment, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, check=False,
                              **background_process_kwargs())

    for path in existing:
        run("worktree", "remove", "--force", str(path))
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed["worktrees"] += 1
    if existing:
        run("worktree", "prune")
    for branch in dict.fromkeys(branches):
        if run("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
            continue
        if run("branch", "-D", branch).returncode == 0:
            removed["branches"] += 1
    if removed["worktrees"] or removed["branches"]:
        logger.info("Removed %d worktrees and %d branches of finished teams in %s",
                    removed["worktrees"], removed["branches"], project)
    return removed


def _under(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved != root and resolved.is_relative_to(root)

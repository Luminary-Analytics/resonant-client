"""Remove what an ended team leaves in the user's repository, and never anyone's work.

Writers work in Git worktrees under Lumi's runtime folder
(``~/.lumi/projects/<id>/swarm/worktrees``), on branches the team creates in
the user's repository: ``lumi/team-<writer>`` (``codex/swarm-writer-<writer>``
before the rebrand). Combined candidates are detached worktrees there too.
While a team runs, waits for review or needs recovery, all of it is the team's
evidence and stays.

Once a team has ended (completed, stopped or failed), a writer's worktree and
branch go when its change was applied (it is on the user's branch) or it made
none, and so does an unapplied combined candidate whose writers' changes were
all applied through another one. That happens when the team ends, and at each
start for teams that ended earlier (the legacy branch prefix included).

What may still be someone's work stays until the person chooses **Discard kept
work** in the Team panel (``discard=True``): the worktree and branch of a
writer whose change wasn't applied, or whose worktree may hold edits nobody
committed (a stopped writer, one sent back, one interrupted), and any other
combined candidate that wasn't applied. An applied combined candidate's
worktree stays too, so Inspect candidate keeps working, until Discard. Stopping
a team says what it keeps (``describe``); the panel shows how much disk the
kept folders take (``kept_work``, ``FolderSizes``), and Discard all kept work
does it for every ended team of the conversation (``kept_across``).

Never removed, not even by Discard: a branch whose tip isn't the commit the
team recorded for it (the writer's result, else its base). Someone committed
there, perhaps the person salvaging the team's work in the writer's
worktree, so it and that worktree are reported and left alone for good; the
person can remove that folder with **Remove folder** (``remove_left_worktree``),
and the branch and its commits stay. A branch checked out, being rebased or
bisected in another worktree stays until it isn't, at a later cleanup.

Nothing here runs ``git worktree remove`` or ``git worktree prune``: Git for
Windows follows a directory junction inside a worktree (an npm ``file:``
dependency) and deletes its target's files, and prune also forgets the
person's own worktrees whose folders are away. lumi/worktree_removal.py
unlinks junctions and links without following them and removes only that
worktree's own entry. Each branch goes right after its own worktree: whether
it is checked out, rebased or bisected anywhere is read again just before
``git update-ref -d`` deletes it against its recorded tip, so a checkout or a
commit that happened while its worktree went keeps it; a recorded tip that
isn't a whole commit id (Git would delete against an empty or all-zero one
unconditionally) is refused before Git runs (``_delete_branch``). Git runs through
lumi/safe_git.py (the installed Git, hooks and the other programs a
repository's settings name off; none in an untrusted project whose settings
name some), and the whole cleanup holds the repository lock every team step
takes (git_boundary.repository_lock).

This doesn't go through the integration's owned-effect protocol
(integration.py) on purpose: it runs only for runs whose supervisor can start
nothing more, touches only folders under Lumi's runtime root and branches the
run recorded, and is idempotent. What it removed, and what it left and why,
is recorded as a run event (``leftovers_removed``, ``kept_work_discarded``,
``left_worktree_removed``); the run's own records stay.
"""

from __future__ import annotations

from contextlib import nullcontext
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Callable, Iterable

from ... import safe_git
from ...git_support import missing_message
from ...worktree_removal import (folder_size, remove_tree, remove_worktree, repository_common_dir, same_path,
                                 worktree_common_dir)
from ..artifacts import project_state_dir
from .git_boundary import REPOSITORY_LOCK_NAME, git_error_line, is_team_branch, repository_lock
from .models import Conflict, Scope
from .store import SwarmStore

logger = logging.getLogger(__name__)

TERMINAL_STATES = ("completed", "cancelled", "failed")
REMOVED_EVENT = "leftovers_removed"
DISCARDED_EVENT = "kept_work_discarded"
LEFT_REMOVED_EVENT = "left_worktree_removed"
# How long cleanup waits for another team step (a check can run for 20
# minutes) to release the repository before trying again at the next start.
LOCK_SECONDS = 120.0


def _manifest(row: dict) -> dict:
    try:
        value = json.loads(row.get("manifest_json") or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def writer_branch(row: dict) -> str:
    """The team branch a writer recorded, or '' (only team branch names count)."""
    branch = str(_manifest(row).get("branch") or "")
    return branch if is_team_branch(branch) else ""


def recorded_tip(row: dict) -> str:
    """The commit a writer's branch should still point at: its result, else its base."""
    return str(row.get("result_revision") or row.get("base_revision") or "")


def classify(writers: Iterable[dict], candidates: Iterable[dict], applications: Iterable[dict]) -> dict[str, list[dict]]:
    """What an ended team's records mean for its leftovers.

    ``unused``: writers nothing needs (their change was applied, or they
    made none). ``kept``: writers that may hold work nobody applied.
    ``unused_candidates``: unapplied combined candidates whose writers' changes
    were all applied through another candidate. ``kept_candidates``: the
    other unapplied ones. ``applied_candidates``: kept for Inspect candidate
    until Discard.
    """
    writers, candidates = [dict(row) for row in writers], [dict(row) for row in candidates]
    applied = {row["id"] for row in candidates if row.get("state") == "applied"}
    applied |= {row["candidate_id"] for row in applications if row.get("state") == "applied"}

    def sources(candidate: dict) -> set[str]:
        return {str(source.get("id")) for source in _manifest(candidate).get("writers") or [] if isinstance(source, dict)}

    applied_writers = {writer for row in candidates if row["id"] in applied for writer in sources(row)}
    unused, kept = [], []
    for row in writers:
        result = row.get("result_revision") or ""
        # "creating" never got a worktree it could edit; "ready" committed
        # everything it changed, which is nothing when its result is its base.
        no_change = row.get("state") == "creating" or (
            row.get("state") == "ready" and (not result or result == row.get("base_revision")))
        (unused if row["id"] in applied_writers or no_change else kept).append(row)
    unapplied = [row for row in candidates if row["id"] not in applied]
    return {"unused": unused, "kept": kept,
            "unused_candidates": [row for row in unapplied if sources(row) and sources(row) <= applied_writers],
            "kept_candidates": [row for row in unapplied if not sources(row) or not sources(row) <= applied_writers],
            "applied_candidates": [row for row in candidates if row["id"] in applied]}


def _recorded(connection, run_id: str) -> dict[str, Any]:
    """What earlier cleanups of this run recorded: the writers and candidates they finished, what they left.

    A left branch's worktree the person removed since (``LEFT_REMOVED_EVENT``)
    is no longer named.
    """
    finished_writers: set[str] = set()
    finished_candidates: set[str] = set()
    left: dict[str, dict] = {}
    for row in connection.execute("SELECT kind, payload FROM events WHERE run_id=? AND kind IN (?,?,?) "
                                  "ORDER BY sequence", (run_id, REMOVED_EVENT, DISCARDED_EVENT, LEFT_REMOVED_EVENT)):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        if row["kind"] == LEFT_REMOVED_EVENT:
            item = left.get(str(payload.get("writer_id") or ""))
            if item is not None and item.pop("worktree", None):
                item["folder_removed"] = True
            continue
        finished_writers.update(str(value) for value in payload.get("writers") or [])
        finished_candidates.update(str(value) for value in payload.get("candidates") or [])
        for item in payload.get("left") or []:
            if isinstance(item, dict) and item.get("writer_id"):
                left[str(item["writer_id"])] = dict(item)
    return {"writers": finished_writers, "candidates": finished_candidates, "left": left}


def _ended_runs(store: SwarmStore, run_ids: Iterable[str] | None) -> dict[str, dict[str, Any]]:
    selected = tuple(run_ids) if run_ids is not None else None
    if selected is not None and not selected:
        return {}
    only = f" AND id IN ({','.join('?' for _ in selected)})" if selected else ""
    runs: dict[str, dict[str, Any]] = {}
    with store._connection() as connection:
        for run in connection.execute(
                f"SELECT id FROM runs WHERE state IN ({','.join('?' for _ in TERMINAL_STATES)}){only} ORDER BY rowid",
                (*TERMINAL_STATES, *(selected or ()))).fetchall():
            run_id = run["id"]
            writers = [dict(row) for row in connection.execute(
                "SELECT * FROM writer_worktrees WHERE run_id=? ORDER BY rowid", (run_id,))]
            candidates = [dict(row) for row in connection.execute(
                "SELECT * FROM integration_candidates WHERE run_id=? ORDER BY rowid", (run_id,))]
            if not writers and not candidates:
                continue
            applications = [dict(row) for row in connection.execute(
                "SELECT a.* FROM integration_applications a JOIN integration_candidates c ON c.id=a.candidate_id "
                "WHERE c.run_id=?", (run_id,))]
            runs[run_id] = {"plan": classify(writers, candidates, applications), "recorded": _recorded(connection, run_id)}
    return runs


class FolderSizes:
    """How much disk kept folders take, for the Team panel: measured in the background, remembered a while.

    ``get`` never waits: a writer's worktree can hold node_modules, which
    takes seconds to walk. It returns the size measured in the last
    ``FRESH_SECONDS`` (an older one while it is measured again), or None
    while the first measurement is queued. One thread measures, one folder at
    a time, links not followed (worktree_removal.folder_size).
    """

    FRESH_SECONDS = 600.0

    def __init__(self, measure: Callable[[str], int] = folder_size):
        self._measure = measure
        self._lock = threading.Lock()
        self._sizes: dict[str, tuple[int, float]] = {}
        self._queued: dict[str, str] = {}
        self._worker: threading.Thread | None = None

    def get(self, path: str) -> int | None:
        key = os.path.normcase(os.path.abspath(path))
        with self._lock:
            known = self._sizes.get(key)
            if (known is None or time.monotonic() - known[1] >= self.FRESH_SECONDS) and key not in self._queued:
                self._queued[key] = path
                if self._worker is None:
                    self._worker = threading.Thread(target=self._run, daemon=True, name="swarm-kept-sizes")
                    try:
                        self._worker.start()
                    except RuntimeError:  # no thread now: the next get tries again
                        self._worker = None
                        self._queued.pop(key, None)
            return known[0] if known is not None else None

    def _run(self) -> None:
        while True:
            with self._lock:
                if not self._queued:
                    self._worker = None
                    return
                key, path = next(iter(self._queued.items()))
            try:
                size = self._measure(path)
            except Exception:  # noqa: BLE001 - a folder that went away meanwhile: no size
                size = None
            with self._lock:
                self._queued.pop(key, None)
                if size is None:
                    self._sizes.pop(key, None)
                else:
                    self._sizes[key] = (size, time.monotonic())


class _Folders:
    """Kept folders' presence and sizes, and their total (``None`` sizes are still being measured)."""

    def __init__(self, sizes: Callable[[str], int | None] | None):
        self._sizes = sizes
        self.total = 0
        self.pending = False

    def __call__(self, path: str | None) -> dict[str, Any]:
        present = bool(path) and os.path.lexists(path)
        size = self._sizes(path) if present and self._sizes is not None else None
        if present:
            if size is None:
                self.pending = True
            else:
                self.total += size
        return {"folder": present, "size": size}

    def summary(self) -> dict[str, Any]:
        return {"size": self.total, "size_pending": self.pending}


def kept_work(store: SwarmStore, snapshot: dict[str, Any],
              sizes: Callable[[str], int | None] | None = None) -> dict[str, Any] | None:
    """What a team keeps in the repository until the person discards it, for the Team panel.

    From the run's records and events, without running Git: ``items`` are
    the kept writers (their branches) and unapplied combined candidates not
    yet discarded; ``applied`` the applied candidates whose worktrees stay
    for Inspect candidate; ``left`` branches Lumi won't delete (moved, or in
    another repository), with the ``worktree`` folder left with one until the
    person removes it. ``size`` is the disk all those folders take, from
    ``sizes`` (``size_pending`` while one is being measured). Discard
    applies once the team has ``ended``.
    """
    writers = snapshot.get("writer_worktrees") or []
    candidates = snapshot.get("integration_candidates") or []
    if not writers and not candidates:
        return None
    run_id = snapshot["run"]["id"]
    plan = classify(writers, candidates, snapshot.get("integration_applications") or [])
    with store._connection() as connection:
        recorded = _recorded(connection, run_id)
    folders = _Folders(sizes)
    items = []
    for row in plan["kept"]:
        if row["id"] in recorded["writers"]:
            continue
        items.append({"kind": "writer", "id": row["id"], "branch": writer_branch(row), "state": row.get("state"),
                      "changed": bool(row.get("result_revision")) and row.get("result_revision") != row.get("base_revision"),
                      **folders(row.get("path"))})
    for row in plan["kept_candidates"]:
        if row["id"] in recorded["candidates"]:
            continue
        items.append({"kind": "candidate", "id": row["id"], "state": row.get("state"), **folders(row.get("path"))})
    applied = [{"kind": "candidate", "id": row["id"], **folders(row.get("path"))}
               for row in plan["applied_candidates"]
               if row["id"] not in recorded["candidates"] and os.path.lexists(row.get("path") or "")]
    left = []
    for item in recorded["left"].values():
        if item.get("worktree") and os.path.lexists(item["worktree"]):
            item = {**item, **folders(item["worktree"])}
        else:
            item = {key: value for key, value in item.items() if key != "worktree"}
        left.append(item)
    return {"ended": snapshot["run"]["state"] in TERMINAL_STATES, "items": items, "applied": applied, "left": left,
            "applied_candidates": len(applied), **folders.summary()}


def kept_across(store: SwarmStore, scope: Scope, sizes: Callable[[str], int | None] | None = None) -> dict[str, Any]:
    """What Discard all kept work would remove: every ended team of a conversation (``scope``).

    ``teams`` that keep anything, their kept ``items`` (writer branches,
    unapplied candidates), ``applied`` candidates' worktrees, the ``run_ids``
    and the disk their folders take. Left branches and their folders aren't
    counted: Discard never removes them.
    """
    with store._connection() as connection:
        run_ids = [row["id"] for row in connection.execute(
            "SELECT id FROM runs WHERE tenant_id=? AND owner_id=? AND project_id=? AND session_id=? "
            f"AND state IN ({','.join('?' for _ in TERMINAL_STATES)}) ORDER BY rowid",
            (*scope.values(), *TERMINAL_STATES))]
    folders = _Folders(sizes)
    teams: list[str] = []
    items = applied = 0
    for run_id, run in _ended_runs(store, run_ids).items():
        plan, recorded = run["plan"], run["recorded"]
        kept = [row for row in plan["kept"] if row["id"] not in recorded["writers"]]
        kept += [row for row in plan["kept_candidates"] if row["id"] not in recorded["candidates"]]
        inspectable = [row for row in plan["applied_candidates"]
                       if row["id"] not in recorded["candidates"] and os.path.lexists(row.get("path") or "")]
        if not kept and not inspectable:
            continue
        teams.append(run_id)
        items += len(kept)
        applied += len(inspectable)
        for row in kept + inspectable:
            folders(row.get("path"))
    return {"teams": len(teams), "run_ids": teams, "items": items, "applied": applied, **folders.summary()}


def describe(kept: dict[str, Any] | None) -> str:
    """One sentence on what a stopped team keeps and how to discard it."""
    items = (kept or {}).get("items") or []
    branches = [item["branch"] or "a writer's worktree" for item in items if item["kind"] == "writer"]
    candidates = sum(1 for item in items if item["kind"] == "candidate")
    parts = []
    if branches:
        shown = ", ".join(branches[:3]) + (", …" if len(branches) > 3 else "")
        parts.append(f"{len(branches)} writer branch{'es' if len(branches) != 1 else ''} ({shown})")
    if candidates:
        parts.append(f"{candidates} combined candidate{'s' if candidates != 1 else ''}")
    if not parts:
        return ("Nothing this team changed is waiting to be applied, so its worktrees and branches are "
                "removed once it has stopped.")
    return (f"Its unapplied work stays in your repository until you discard it: {' and '.join(parts)}. "
            "Once the team has stopped, Review file changes offers Discard kept work.")


def _git(project: Path, *args: str) -> subprocess.CompletedProcess:
    """Git in the user's repository for cleanup; a failure, never an exception, when it can't run.

    lumi/safe_git.py: the installed Git with the repository's hooks, its
    fsmonitor and the other programs its settings name turned off, and none
    at all in an untrusted project whose settings name some (GitRefused).
    Git variables this process inherited (GIT_DIR, GIT_INDEX_FILE) don't
    redirect it, and it never prompts.
    """
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        return safe_git.run(project, *args, env=environment, timeout=60)
    except (safe_git.GitRefused, OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *args], 1, "", f"error: {exc}")


def _checked_out(worktree_list: str) -> dict[str, list[str]]:
    """Each branch ref checked out in a worktree, and where (``git worktree list --porcelain``)."""
    found: dict[str, list[str]] = {}
    folder = ""
    for line in worktree_list.splitlines():
        if line.startswith("worktree "):
            folder = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            found.setdefault(line[len("branch "):].strip(), []).append(folder)
    return found


def _rebasing_or_bisecting(common: Path) -> set[str]:
    """Branch refs a rebase or bisect in any worktree will come back to (git branch -D's own checks)."""
    used: set[str] = set()
    folders = [common]
    try:
        folders += [entry for entry in (common / "worktrees").iterdir() if entry.is_dir()]
    except OSError:
        pass
    for folder in folders:
        for name in ("rebase-merge/head-name", "rebase-apply/head-name", "BISECT_START"):
            try:
                value = (folder / name).read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if value:
                used.add(value if value.startswith("refs/") else f"refs/heads/{value}")
    return used


def _under(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved != root and resolved.is_relative_to(root)


def clean_finished_teams(store: SwarmStore, workspace: str | Path, *, run_ids: Iterable[str] | None = None,
                         root: str | Path | None = None, discard: bool = False,
                         lock_seconds: float = LOCK_SECONDS) -> dict[str, Any]:
    """Remove ended teams' leftovers nothing needs (with ``discard``, their kept work too); returns a report.

    ``run_ids`` limits it to those runs (they still have to have ended);
    without it every ended run in the store is checked. Discard also removes
    applied candidates' worktrees, which stay for Inspect candidate until
    then. The report counts removed ``worktrees`` and ``branches``, lists
    branches ``left`` (moved, in use) and what ``failed``, and says why
    nothing ran (``skipped``).
    """
    report: dict[str, Any] = {"worktrees": 0, "branches": 0, "left": [], "failed": [], "skipped": ""}
    runs = _ended_runs(store, run_ids)
    targets: list[tuple[str, str, dict]] = []
    for run_id, run in runs.items():
        plan, recorded = run["plan"], run["recorded"]
        writers = plan["unused"] + (plan["kept"] if discard else [])
        candidates = plan["unused_candidates"] + (plan["kept_candidates"] + plan["applied_candidates"] if discard else [])
        targets += [(run_id, "writer", row) for row in writers if row["id"] not in recorded["writers"]]
        targets += [(run_id, "candidate", row) for row in candidates if row["id"] not in recorded["candidates"]]
    if not targets:
        return report
    project = Path(workspace).resolve()
    runtime_root = Path(root or project_state_dir(project) / "swarm" / "worktrees").resolve()
    if safe_git.executable(project) is None:
        # No Git now: the next start tries again.
        report["skipped"] = missing_message("Removing what ended teams left needs")
        return report

    def run(*args: str) -> subprocess.CompletedProcess:
        return _git(project, *args)

    found = run("rev-parse", "--git-common-dir")
    if found.returncode != 0 or not found.stdout.strip():
        report["skipped"] = git_error_line(found.stderr) or "The project is no longer a Git repository."
        return report
    common = (project / found.stdout.strip()).resolve()
    try:
        with repository_lock(common / REPOSITORY_LOCK_NAME, timeout=lock_seconds):
            outcomes = _remove(run, common, runtime_root, targets, report)
    except (Conflict, OSError) as exc:
        report["skipped"] = str(exc)
        return report
    _record(store, outcomes, discard)
    if report["worktrees"] or report["branches"] or report["left"] or report["failed"]:
        logger.info("Team cleanup in %s: removed %d worktrees and %d branches; left %s; failed %s", project,
                    report["worktrees"], report["branches"], report["left"], report["failed"])
    return report


def _is_commit_id(value: str) -> bool:
    """Whether ``value`` is a whole commit id: 40 hex digits (64 in a SHA-256 repository), not all zero."""
    return (len(value) in (40, 64) and all(character in "0123456789abcdef" for character in value)
            and value.strip("0") != "")


def _delete_branch(run, ref: str, expected: str) -> subprocess.CompletedProcess:
    """``git update-ref -d <ref> <expected>``: delete the branch only if it still points at ``expected``.

    Git treats an empty or all-zero old value as no condition at all and
    deletes the branch whatever it points at, and an abbreviated id isn't
    the commit the team recorded, so anything but a whole commit id is
    refused here (ValueError) without running Git.
    """
    if not _is_commit_id(expected):
        raise ValueError(f"The team's record of {ref.removeprefix('refs/heads/')} isn't a whole commit id "
                         f"({expected!r}), so Lumi left the branch.")
    return run("update-ref", "-d", ref, expected)


def _tip(run, ref: str) -> str | None:
    """The commit a branch points at now, or None when there is no such branch; OSError when Git can't tell."""
    found = run("rev-parse", "--quiet", "--verify", ref)
    if found.returncode == 0:
        return found.stdout.strip()
    if found.stderr.strip():
        raise OSError(git_error_line(found.stderr) or f"Git couldn't read {ref}.")
    return None


def _in_use(run, common: Path, ref: str, own: Path) -> bool:
    """Whether another worktree has ``ref`` checked out, or a rebase or bisect will come back to it.

    ``git branch -d``'s own checks, which ``update-ref -d`` doesn't make.
    When Git can't list the worktrees, the branch counts as in use: the next
    cleanup looks again.
    """
    listing = run("worktree", "list", "--porcelain")
    if listing.returncode != 0:
        return True
    folders = _checked_out(listing.stdout).get(ref, ())
    return ref in _rebasing_or_bisecting(common) or any(not same_path(folder, own) for folder in folders)


def _remove(run, common: Path, runtime_root: Path, targets: list[tuple[str, str, dict]],
            report: dict[str, Any]) -> dict[str, dict[str, list]]:
    """Remove each target's worktree, then its branch right away; per-run outcomes.

    A writer's branch is read first. One that moved (someone committed
    there, perhaps in the writer's own worktree) or belongs to another
    repository isn't the team's any more: it and its worktree are left for
    good, Discard or not. Otherwise the worktree goes, and then the branch,
    unless it is checked out in another worktree or being rebased or bisected.
    That is read again just before ``update-ref -d``, since someone may have
    checked the branch out while its worktree went: such a branch stays until
    it is free, at a later cleanup. Deleting against the recorded tip keeps a
    branch a commit landed on meanwhile.
    """
    outcomes: dict[str, dict[str, list]] = {}

    def outcome(run_id: str) -> dict[str, list]:
        return outcomes.setdefault(run_id, {"writers": [], "candidates": [], "branches": [], "left": [], "failed": []})

    def failed(run_id: str, row: dict, kind: str, error: str) -> None:
        item = {"run_id": run_id, "id": row["id"], "kind": kind, "error": error}
        report["failed"].append(item)
        outcome(run_id)["failed"].append(item)

    def left(run_id: str, row: dict, branch: str, reason: str) -> None:
        item = {"run_id": run_id, "writer_id": row["id"], "branch": branch, "reason": reason}
        path = str(row.get("path") or "")
        if reason != "in_use" and path and os.path.lexists(path):
            item["worktree"] = path  # left with it, where the person can find it (and Remove it)
        report["left"].append(item)
        if reason != "in_use":  # in use now, perhaps not at the next start
            outcome(run_id)["left"].append(item)
            outcome(run_id)["writers"].append(row["id"])

    repo_key = hashlib.sha256(os.path.normcase(str(common)).encode()).hexdigest()
    for run_id, kind, row in targets:
        path = Path(row.get("path") or "")
        branch = writer_branch(row) if kind == "writer" else ""
        ref = f"refs/heads/{branch}"
        try:
            tip = _tip(run, ref) if branch else None
        except OSError as exc:
            failed(run_id, row, kind, str(exc))
            continue
        if tip is not None and row.get("repo_key") and row["repo_key"] != repo_key:
            left(run_id, row, branch, "another_repository")
            continue
        if tip is not None and tip != recorded_tip(row):
            left(run_id, row, branch, "moved")  # someone committed there: it isn't only the team's any more
            continue
        if str(path) and _under(path, runtime_root):
            existed = os.path.lexists(path)
            removal = remove_worktree(path, common_dir=common)
            if not removal.removed:
                failed(run_id, row, kind, removal.error)
                continue
            if existed:
                report["worktrees"] += 1
        elif str(path) and os.path.lexists(path):
            # Recorded outside Lumi's folder: never Lumi's to delete.
            failed(run_id, row, kind, "The worktree isn't in Lumi's folder, so Lumi left it.")
            continue
        if kind == "candidate":
            outcome(run_id)["candidates"].append(row["id"])
            continue
        if tip is None:
            outcome(run_id)["writers"].append(row["id"])  # no branch (any more): nothing else to do
            continue
        if _in_use(run, common, ref, path):
            left(run_id, row, branch, "in_use")
            continue
        try:
            deleted = _delete_branch(run, ref, recorded_tip(row))
        except ValueError as exc:
            failed(run_id, row, "writer", str(exc))
            continue
        if deleted.returncode == 0:
            report["branches"] += 1
            outcome(run_id)["branches"].append(branch)
            outcome(run_id)["writers"].append(row["id"])
            continue
        try:
            now = _tip(run, ref)
        except OSError:
            now = recorded_tip(row)
        if now is None:
            outcome(run_id)["writers"].append(row["id"])  # someone else deleted it meanwhile
        elif now != recorded_tip(row):
            left(run_id, row, branch, "moved")  # a commit landed on it meanwhile
        else:
            failed(run_id, row, "writer", git_error_line(deleted.stderr) or "git update-ref failed")
    return outcomes


def _record(store: SwarmStore, outcomes: dict[str, dict[str, list]], discard: bool) -> None:
    """A run event for each run whose leftovers changed: what went, what was left and why."""
    for run_id, outcome in outcomes.items():
        if not (outcome["writers"] or outcome["candidates"] or (discard and outcome["failed"])):
            continue
        try:
            with store._connection(write=True) as connection:
                store._event(connection, run_id, DISCARDED_EVENT if discard else REMOVED_EVENT, outcome)
        except Exception:  # noqa: BLE001 - the repository changed either way; the next cleanup re-checks
            logger.warning("Couldn't record the cleanup of team %s", run_id, exc_info=True)


def remove_left_worktree(store: SwarmStore, workspace: str | Path, run_id: str, writer_id: str, *,
                         root: str | Path | None = None, lock_seconds: float = LOCK_SECONDS) -> dict[str, str]:
    """Remove the worktree folder left with a branch Lumi won't delete: the person's **Remove**.

    Such a branch moved (someone committed there, perhaps in that very
    folder) or belongs to another repository, so cleanup and Discard leave it
    and its worktree for good. When the person no longer needs the folder,
    it goes like any team worktree (worktree_removal.remove_worktree: links
    inside unlinked, never followed, and only that worktree's own Git record),
    under the repository lock. The branch and its commits stay; what wasn't
    committed in the folder goes with it, which the panel asks the person to
    confirm. Raises Conflict when there is no such folder or it can't go.
    """
    with store._connection() as connection:
        item = _recorded(connection, run_id)["left"].get(writer_id)
    path = Path(item["worktree"]) if item and item.get("worktree") else None
    if path is None or not os.path.lexists(path):
        raise Conflict("Lumi keeps no folder for that branch any more; refresh the team")
    project = Path(workspace).resolve()
    runtime_root = Path(root or project_state_dir(project) / "swarm" / "worktrees").resolve()
    if not _under(path, runtime_root):
        raise Conflict("That folder isn't in Lumi's folder, so Lumi won't remove it")
    common = repository_common_dir(project)
    with repository_lock(common / REPOSITORY_LOCK_NAME, timeout=lock_seconds) if common else nullcontext():
        # Its own repository's record: another repository's, for a branch left for that reason.
        own = worktree_common_dir(path)
        if own is not None:
            removal = remove_worktree(path, common_dir=own)
            error = "" if removal.removed else removal.error
        else:
            try:
                remove_tree(path)
                error = ""
            except OSError as exc:
                error = f"{exc.strerror or exc}"
    if error:
        raise Conflict(f"Lumi couldn't remove {path}: {error}")
    removed = {"writer_id": writer_id, "branch": str(item.get("branch") or ""), "worktree": str(path)}
    with store._connection(write=True) as connection:
        store._event(connection, run_id, LEFT_REMOVED_EVENT, removed)
    return removed

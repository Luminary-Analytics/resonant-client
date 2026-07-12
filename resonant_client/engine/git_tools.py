"""
First-class git tools for the Resonant Engine.

Wraps `git` subprocess calls with structured output so the UI can render
file lists, diff hunks, commit cards, and log tables instead of raw stdout.

Safety rails (mirroring the project's git policy):
- NEVER pass --no-verify (do not skip hooks)
- NEVER pass --amend (always create new commits)
- NEVER inject Co-Authored-By lines automatically
- NEVER force-push (push isn't even exposed here)
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Optional

from resonant_client.processes import background_process_kwargs

from .tools import ToolResult


# ── Internal helpers ─────────────────────────────────────────────────────


def _run_git(args: list[str], cwd: Path | str, *, timeout: float = 30.0) -> tuple[int, str, str]:
    """Run `git <args>` in `cwd` without a shell. Returns (returncode, stdout, stderr)."""
    cmd = ["git"] + args
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            **background_process_kwargs(),
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"git timed out after {timeout}s"
    except FileNotFoundError:
        return -1, "", "git executable not found on PATH"


def _is_git_repo(cwd: Path | str) -> bool:
    rc, out, _ = _run_git(["rev-parse", "--git-dir"], cwd, timeout=5.0)
    return rc == 0 and bool(out.strip())


# ── Status ──────────────────────────────────────────────────────────────


def git_status(cwd: Path | str) -> dict:
    """
    Returns a structured snapshot of the working tree.

    {
        "branch": str | None,
        "ahead": int, "behind": int,
        "staged":   [{"path": str, "status": "M"|"A"|"D"|"R"|"C"|"U"}],
        "unstaged": [{"path": str, "status": "M"|"D"|"U"}],
        "untracked": [str, ...],
        "clean": bool,
    }
    """
    if not _is_git_repo(cwd):
        return {"error": "not a git repository", "branch": None, "staged": [], "unstaged": [], "untracked": [], "clean": True, "ahead": 0, "behind": 0}

    rc, out, err = _run_git(
        ["status", "--porcelain=v1", "-b", "--untracked-files=normal"],
        cwd,
    )
    if rc != 0:
        return {"error": err.strip() or "git status failed", "branch": None, "staged": [], "unstaged": [], "untracked": [], "clean": True, "ahead": 0, "behind": 0}

    branch: Optional[str] = None
    ahead = 0
    behind = 0
    staged: list[dict] = []
    unstaged: list[dict] = []
    untracked: list[str] = []

    for line in out.splitlines():
        if not line:
            continue
        if line.startswith("##"):
            # ## main...origin/main [ahead 1, behind 2]
            head = line[3:].strip()
            track_part = ""
            if " [" in head:
                head, track_part = head.split(" [", 1)
                track_part = track_part.rstrip("]")
            if "..." in head:
                branch = head.split("...", 1)[0].strip()
            else:
                branch = head.strip() or None
            for token in track_part.split(","):
                token = token.strip()
                if token.startswith("ahead "):
                    try:
                        ahead = int(token[6:])
                    except ValueError:
                        pass
                elif token.startswith("behind "):
                    try:
                        behind = int(token[7:])
                    except ValueError:
                        pass
            continue

        # Porcelain entries are: XY <path>
        if len(line) < 4:
            continue
        x, y, _sp, rest = line[0], line[1], line[2], line[3:]
        path = rest.strip().strip('"')

        if x == "?" and y == "?":
            untracked.append(path)
            continue
        if x != " ":
            staged.append({"path": path, "status": x})
        if y != " ":
            unstaged.append({"path": path, "status": y})

    clean = not (staged or unstaged or untracked)
    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "clean": clean,
    }


# ── Diff ────────────────────────────────────────────────────────────────


def git_diff(
    cwd: Path | str,
    *,
    staged: bool = False,
    paths: Optional[list[str]] = None,
) -> dict:
    """
    Returns structured diff for the working tree (or staged area).

    {
        "files": [{
            "path": str,
            "additions": int,
            "deletions": int,
            "hunks": [{"header": str, "lines": [str, ...]}],
        }],
        "total_additions": int,
        "total_deletions": int,
    }
    """
    if not _is_git_repo(cwd):
        return {"error": "not a git repository", "files": [], "total_additions": 0, "total_deletions": 0}

    args = ["diff", "--no-color"]
    if staged:
        args.append("--cached")
    if paths:
        args.append("--")
        args.extend(paths)

    rc, out, err = _run_git(args, cwd)
    if rc != 0:
        return {"error": err.strip() or "git diff failed", "files": [], "total_additions": 0, "total_deletions": 0}

    files: list[dict] = []
    current: Optional[dict] = None
    current_hunk: Optional[dict] = None
    total_additions = 0
    total_deletions = 0

    for line in out.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                if current_hunk is not None:
                    current["hunks"].append(current_hunk)
                    current_hunk = None
                files.append(current)
            # `diff --git a/path b/path`
            try:
                _, _, a_b = line.partition(" a/")
                a_path, _, b_path = a_b.partition(" b/")
                path = (b_path or a_path).strip()
            except Exception:
                path = ""
            current = {"path": path, "additions": 0, "deletions": 0, "hunks": []}
            continue
        if current is None:
            continue
        if line.startswith("@@"):
            if current_hunk is not None:
                current["hunks"].append(current_hunk)
            current_hunk = {"header": line, "lines": []}
            continue
        if line.startswith("+++") or line.startswith("---") or line.startswith("index ") or line.startswith("new file") or line.startswith("deleted file") or line.startswith("rename ") or line.startswith("similarity "):
            continue
        if current_hunk is not None:
            current_hunk["lines"].append(line)
            if line.startswith("+") and not line.startswith("+++"):
                current["additions"] += 1
                total_additions += 1
            elif line.startswith("-") and not line.startswith("---"):
                current["deletions"] += 1
                total_deletions += 1

    if current is not None:
        if current_hunk is not None:
            current["hunks"].append(current_hunk)
        files.append(current)

    return {
        "files": files,
        "total_additions": total_additions,
        "total_deletions": total_deletions,
    }


# ── Commit ──────────────────────────────────────────────────────────────


def git_commit(
    cwd: Path | str,
    message: str,
    *,
    paths: Optional[list[str]] = None,
) -> dict:
    """
    Stage `paths` (if given) and create a NEW commit. Never amends.

    Returns: {"commit_sha": str, "summary": str, "message": str}
    Or:      {"error": str}
    """
    if not _is_git_repo(cwd):
        return {"error": "not a git repository"}

    msg = (message or "").strip()
    if not msg:
        return {"error": "commit message is required"}

    if paths:
        rc, _, err = _run_git(["add", "--"] + list(paths), cwd)
        if rc != 0:
            return {"error": f"git add failed: {err.strip()}"}

    # Check if there's anything staged
    rc, staged_out, _ = _run_git(["diff", "--cached", "--name-only"], cwd)
    if rc != 0 or not staged_out.strip():
        return {"error": "nothing staged to commit (run with paths= to stage files first)"}

    # Use stdin to pass message — no shell quoting issues, supports multi-line.
    cmd = ["git", "commit", "--file=-"]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            input=msg,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            shell=False,
            **background_process_kwargs(),
        )
    except subprocess.TimeoutExpired:
        return {"error": "git commit timed out"}
    except FileNotFoundError:
        return {"error": "git executable not found on PATH"}

    if proc.returncode != 0:
        return {"error": f"git commit failed: {(proc.stderr or proc.stdout).strip()}"}

    rc, sha, _ = _run_git(["rev-parse", "HEAD"], cwd)
    sha = sha.strip() if rc == 0 else ""
    summary = (proc.stdout or "").strip().splitlines()
    summary_line = summary[0] if summary else ""

    return {
        "commit_sha": sha,
        "short_sha": sha[:7],
        "summary": summary_line,
        "message": msg,
    }


# ── Branch create ──────────────────────────────────────────────────────


def git_branch_create(
    cwd: Path | str,
    branch: str,
    *,
    from_ref: str = "HEAD",
) -> dict:
    """
    Create AND check out a new branch from `from_ref`. Refuses if branch exists.

    Returns: {"branch": str, "from_ref": str, "from_sha": str} or {"error": str}
    """
    if not _is_git_repo(cwd):
        return {"error": "not a git repository"}

    branch = (branch or "").strip()
    if not branch:
        return {"error": "branch name is required"}

    # Check existence first (don't surprise-clobber)
    rc, _, _ = _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd)
    if rc == 0:
        return {"error": f"branch '{branch}' already exists"}

    rc, sha, _ = _run_git(["rev-parse", from_ref], cwd)
    if rc != 0:
        return {"error": f"unknown ref: {from_ref}"}
    from_sha = sha.strip()

    rc, _, err = _run_git(["checkout", "-b", branch, from_ref], cwd)
    if rc != 0:
        return {"error": f"git checkout -b failed: {err.strip()}"}

    return {"branch": branch, "from_ref": from_ref, "from_sha": from_sha}


# ── Log ─────────────────────────────────────────────────────────────────


def git_log(
    cwd: Path | str,
    *,
    limit: int = 20,
    paths: Optional[list[str]] = None,
) -> dict:
    """
    Returns recent commits.

    {"commits": [{"sha", "short_sha", "author_name", "author_email", "date", "subject"}, ...]}
    """
    if not _is_git_repo(cwd):
        return {"error": "not a git repository", "commits": []}

    limit = max(1, min(int(limit or 20), 200))
    fmt = "%H%x1f%an%x1f%ae%x1f%aI%x1f%s"
    args = ["log", f"--pretty=format:{fmt}", "--date=iso", "-n", str(limit)]
    if paths:
        args.append("--")
        args.extend(paths)

    rc, out, err = _run_git(args, cwd)
    if rc != 0:
        return {"error": err.strip() or "git log failed", "commits": []}

    commits: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 5:
            continue
        sha, name, email, date, subject = parts
        commits.append({
            "sha": sha,
            "short_sha": sha[:7],
            "author_name": name,
            "author_email": email,
            "date": date,
            "subject": subject,
        })

    return {"commits": commits}


# ── Exec wrappers (ToolResult shape used by tools.execute_tool) ─────────


def _format_status(data: dict) -> str:
    if data.get("error"):
        return f"git status: {data['error']}"
    if data.get("clean"):
        branch = data.get("branch") or "(detached)"
        return f"On branch {branch} — clean working tree"
    lines = []
    branch = data.get("branch") or "(detached)"
    track = ""
    if data.get("ahead") or data.get("behind"):
        track = f" [ahead {data.get('ahead', 0)}, behind {data.get('behind', 0)}]"
    lines.append(f"On branch {branch}{track}")
    if data.get("staged"):
        lines.append(f"Staged ({len(data['staged'])}):")
        for s in data["staged"][:30]:
            lines.append(f"  {s['status']}  {s['path']}")
    if data.get("unstaged"):
        lines.append(f"Unstaged ({len(data['unstaged'])}):")
        for s in data["unstaged"][:30]:
            lines.append(f"  {s['status']}  {s['path']}")
    if data.get("untracked"):
        lines.append(f"Untracked ({len(data['untracked'])}):")
        for p in data["untracked"][:20]:
            lines.append(f"  ?? {p}")
    return "\n".join(lines)


def exec_git_status(args: dict, start: float) -> ToolResult:
    cwd = args.get("cwd") or "."
    data = git_status(cwd)
    return ToolResult(
        output=_format_status(data),
        is_error=bool(data.get("error")),
        elapsed=time.time() - start,
        metadata=data,
    )


def _format_diff(data: dict) -> str:
    if data.get("error"):
        return f"git diff: {data['error']}"
    if not data.get("files"):
        return "No changes."
    lines = [f"{data['total_additions']} additions, {data['total_deletions']} deletions across {len(data['files'])} file(s):"]
    for f in data["files"][:40]:
        lines.append(f"  {f['path']}  (+{f['additions']} -{f['deletions']})")
    return "\n".join(lines)


def exec_git_diff(args: dict, start: float) -> ToolResult:
    cwd = args.get("cwd") or "."
    staged = bool(args.get("staged", False))
    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    data = git_diff(cwd, staged=staged, paths=paths)
    return ToolResult(
        output=_format_diff(data),
        is_error=bool(data.get("error")),
        elapsed=time.time() - start,
        metadata=data,
    )


def exec_git_commit(args: dict, start: float) -> ToolResult:
    cwd = args.get("cwd") or "."
    message = args.get("message", "")
    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    data = git_commit(cwd, message, paths=paths)
    if data.get("error"):
        return ToolResult(f"git commit: {data['error']}", is_error=True, elapsed=time.time() - start, metadata=data)
    output = f"[{data['short_sha']}] {data['summary']}"
    return ToolResult(output=output, elapsed=time.time() - start, metadata=data)


def exec_git_branch_create(args: dict, start: float) -> ToolResult:
    cwd = args.get("cwd") or "."
    branch = args.get("branch", "")
    from_ref = args.get("from_ref", "HEAD")
    data = git_branch_create(cwd, branch, from_ref=from_ref)
    if data.get("error"):
        return ToolResult(f"git branch: {data['error']}", is_error=True, elapsed=time.time() - start, metadata=data)
    return ToolResult(
        output=f"Created and switched to branch '{data['branch']}' from {from_ref} ({data['from_sha'][:7]})",
        elapsed=time.time() - start,
        metadata=data,
    )


def _format_log(data: dict) -> str:
    if data.get("error"):
        return f"git log: {data['error']}"
    commits = data.get("commits", [])
    if not commits:
        return "No commits."
    lines = []
    for c in commits:
        date = (c.get("date") or "")[:10]
        lines.append(f"{c['short_sha']}  {date}  {c['author_name'][:20]:<20}  {c['subject']}")
    return "\n".join(lines)


def exec_git_log(args: dict, start: float) -> ToolResult:
    cwd = args.get("cwd") or "."
    limit = int(args.get("limit", 20))
    paths = args.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    data = git_log(cwd, limit=limit, paths=paths)
    return ToolResult(
        output=_format_log(data),
        is_error=bool(data.get("error")),
        elapsed=time.time() - start,
        metadata=data,
    )

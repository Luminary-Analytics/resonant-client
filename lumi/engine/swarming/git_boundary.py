"""Bounded Git reads and executable-filter suppression for trusted host effects."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import BinaryIO, Iterator

from ...executables import find_program
from ...processes import background_process_kwargs, close_windows_job, popen_in_kill_job
from .models import Conflict


# The file in $GIT_COMMON_DIR that every Lumi process locks while it changes
# the repository for a team (integration.py, cleanup.py): byte 0, exclusively.
REPOSITORY_LOCK_NAME = "sonn-swarm-integration.lock"


def open_repository_lock(path: Path) -> BinaryIO:
    """The repository lock file, holding at least the one byte its holders lock."""
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    return handle


def try_repository_lock(handle: BinaryIO) -> None:
    """Take the lock now, or raise OSError when another holder (any process) has it."""
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def release_repository_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def repository_lock(path: Path, *, timeout: float) -> Iterator[None]:
    """Hold the repository lock, waiting up to ``timeout`` seconds for another holder.

    SwarmIntegration._repository_lock is the same lock with its callers'
    extras (ending a wait early, reporting who waits).
    """
    handle = open_repository_lock(path)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                try_repository_lock(handle)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise Conflict(f"Another team step held this repository for {timeout:.0f} s") from exc
                time.sleep(0.05)
        try:
            yield
        finally:
            release_repository_lock(handle)
    finally:
        handle.close()


# Writer branches a team creates in the user's repository. Teams used the
# legacy prefix until the rebrand; cleanup (cleanup.py) removes both.
TEAM_BRANCH_PREFIX = "lumi/team-"
LEGACY_TEAM_BRANCH_PREFIXES = ("codex/swarm-writer-",)


def is_team_branch(name: str) -> bool:
    return name.startswith((TEAM_BRANCH_PREFIX, *LEGACY_TEAM_BRANCH_PREFIXES))


def git_error_line(stderr: str, limit: int = 200) -> str:
    """The line of Git's output that says what failed, short and on one line.

    Git often prints progress first ("Preparing worktree (new branch ...)")
    and the reason after ("fatal: '$GIT_DIR' too big"), so prefer its
    ``fatal:``/``error:`` line and fall back to the first line.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in lines:
        if line.lower().startswith(("fatal:", "error:")):
            return line[:limit]
    return lines[0][:limit] if lines else ""


# Where Git for Windows keeps the git.exe a git.cmd launcher runs, relative to
# the launcher's folder (cmd\git.cmd ran bin\git.exe or mingw64\bin\git.exe).
_WRAPPED_GIT = (("cmd", "git.exe"), ("bin", "git.exe"), ("mingw64", "bin", "git.exe"), ("mingw32", "bin", "git.exe"))


def trusted_git_executable(project: Path, runtime_root: Path) -> str:
    """Pin an absolute host binary without cwd or repository PATH shadowing.

    The shared resolver (lumi/executables.py) skips the working folder,
    relative PATH entries and both folders. The pinned binary is checked
    again here, where a home-folder project counts too. On Windows a
    ``git.cmd`` launcher counts as installed Git, as it does for
    git_support.git_available(); Lumi runs the git.exe it launches, since a
    command script would pass every argument through cmd.exe's parser. A
    launcher whose git.exe isn't where Git for Windows puts it is named in the
    refusal rather than reported as no Git at all.
    """
    def usable(candidate: Path) -> bool:
        return (not candidate.is_relative_to(project) and not candidate.is_relative_to(runtime_root)
                and candidate.is_file() and os.access(candidate, os.X_OK))

    def pinned(found: str | None) -> Path | None:
        if not found:
            return None
        try:
            candidate = Path(found).resolve(strict=True)
        except OSError:
            return None
        return candidate if usable(candidate) else None

    candidate = pinned(find_program("git", exclude=[project, runtime_root]))
    if candidate is not None:
        return str(candidate)
    launcher = pinned(find_program("git", exclude=[project, runtime_root], scripts=True)) if os.name == "nt" else None
    if launcher is not None and launcher.suffix.lower() == ".cmd":
        for parts in _WRAPPED_GIT:
            try:
                wrapped = launcher.parent.parent.joinpath(*parts).resolve(strict=True)
            except OSError:
                continue
            if usable(wrapped):
                return str(wrapped)
        from ...git_support import download_url, product_name

        raise Conflict(f"Writer teams run Git without a command shell, and the Git on this computer's PATH is a "
                       f"command script ({launcher}) whose git.exe Lumi can't find. Install {product_name()} "
                       f"from {download_url()}, or put its git.exe on PATH, and restart Lumi. "
                       "Read-only teams work without it.")
    if find_program("git", scripts=True) is None:
        # The usual case on a new computer: no Git at all. Read-only teams
        # never get here; only writers work in Git worktrees.
        from ...git_support import missing_message

        raise Conflict(missing_message("Writer teams need") + " Read-only teams work without it.")
    raise Conflict("Supervised integration requires a host Git executable outside the project and runtime worktrees")


def disabled_filter_options(configuration: bytes) -> tuple[str, ...]:
    """Disable executable attributes while preserving ordinary text normalization.

    Configuration values are never needed or copied into diagnostics. Discover
    names afresh in the actual worktree, including its conditional includes.
    """
    try:
        names = configuration.decode("utf-8").split("\0")
    except UnicodeError as exc:
        raise Conflict("Git configuration names cannot be inspected safely") from exc
    drivers = {name.rsplit(".", 1)[0] for name in names
               if name.startswith("filter.") and name.count(".") >= 2}
    return tuple(option for name in sorted(drivers) for option in
                 (f"{name}.clean=", f"{name}.smudge=", f"{name}.process=", f"{name}.required=false"))


def has_custom_merge_driver(configuration: bytes) -> bool:
    """External merge commands have no supervised execution contract."""
    return any(name.startswith("merge.") and name.endswith(".driver") and name.count(".") >= 2
               for name in configuration.decode("utf-8").split("\0"))


def git_bytes(integration, path: Path, args: tuple[str, ...], *, limit: int, deadline: float,
              overrides: tuple[str, ...] = ()) -> tuple[bytes, bool]:
    """Stop at the output cap instead of buffering an unbounded Git response."""
    if time.monotonic() >= deadline:
        raise Conflict("Git inspection exceeded its time limit")
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
                       GIT_NO_REPLACE_OBJECTS="1", GIT_WORK_TREE=str(path))
    command = [integration._git_executable, "--no-pager", "-c", f"core.hooksPath={integration._hooks}",
               "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "core.quotePath=false",
               *[value for option in overrides for value in ("-c", option)], *args]
    process, job = popen_in_kill_job(command, cwd=path, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
        **background_process_kwargs(new_process_group=True))
    reader = None
    complete = threading.Event()
    output = bytearray()
    failure = []
    truncated = False
    def drain():
        nonlocal truncated
        try:
            while len(output) <= limit:
                chunk = process.stdout.read(min(8192, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
            truncated = len(output) > limit
        except (OSError, ValueError):
            failure.append(True)
        finally:
            complete.set()
    try:
        reader = threading.Thread(target=drain, daemon=True, name="swarm-git-inspection")
        reader.start()
        if not complete.wait(max(0, deadline - time.monotonic())):
            raise Conflict("Git inspection exceeded its time limit")
        if failure:
            raise Conflict("Git inspection output could not be observed")
        if not truncated:
            try:
                code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as exc:
                raise Conflict("Git inspection exceeded its time limit") from exc
            if code:
                # stderr may include source text, configured commands or secrets.
                raise Conflict("Git could not inspect the exact stored input")
        return bytes(output[:limit]), truncated
    finally:
        if job:
            close_windows_job(job)
        elif os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if reader is not None:
            reader.join(timeout=1)
        process.stdout.close()

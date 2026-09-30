"""Bounded Git reads and executable-filter suppression for trusted host effects."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import threading
import time

from ...executables import find_program
from ...processes import background_process_kwargs, close_windows_job, popen_in_kill_job
from .models import Conflict


def trusted_git_executable(project: Path, runtime_root: Path) -> str:
    """Pin an absolute host binary without cwd or repository PATH shadowing.

    The shared resolver (lumi/executables.py) skips the working folder,
    relative PATH entries and both folders. The pinned binary is checked
    again here, where a home-folder project counts too.
    """
    found = find_program("git", exclude=[project, runtime_root])
    if found:
        try:
            candidate = Path(found).resolve(strict=True)
        except OSError:
            candidate = None
        if (candidate is not None and not candidate.is_relative_to(project)
                and not candidate.is_relative_to(runtime_root) and candidate.is_file()
                and os.access(candidate, os.X_OK)):
            return str(candidate)
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

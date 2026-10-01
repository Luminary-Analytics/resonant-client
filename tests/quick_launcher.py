"""A child that exits at once, leaving a program it started: what a late job assignment loses.

Lumi used to start a process, then assign it to a Windows job. A child that
had exited by then made the assignment fail ("Access is denied"), and what it
had started ran outside the job, out of reach of Stop and kill-on-close. On a
busy host another thread can hold the GIL between the two for that long.
``lumi.processes.popen_in_kill_job`` starts the child suspended instead.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from lumi import processes

# In the sleeper's command line, so a process that reused its id isn't taken for it.
MARKER = "lumi-quick-launcher-sleeper"


def assign_jobs_late(monkeypatch, seconds: float = .5) -> None:
    """Assign each job ``seconds`` after its process was created, as a loaded host can."""
    assign = processes.windows_kill_job

    def late(process, **kwargs):
        time.sleep(seconds)  # a quick launcher has started its sleeper and exited by now
        return assign(process, **kwargs)

    monkeypatch.setattr(processes, "windows_kill_job", late)


def quick_launcher(folder: Path, *, holds_output: bool = False) -> tuple[list[str], Path]:
    """A child that starts a minute-long sleeper and exits: ``(argv, pid_file)``.

    The launcher writes the sleeper's process id to ``pid_file``. With
    ``holds_output`` the sleeper keeps the launcher's standard output and
    error, so a reader waits for it as well. ``-I`` leaves out the user's site
    packages, whose startup hooks can outlast the late assignment.
    """
    pid_file = folder / "sleeper.pid"
    script = folder / "quick_launcher.py"
    output = "None" if holds_output else "subprocess.DEVNULL"
    script.write_text(
        "import subprocess, sys\n"
        f"sleeper = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)  # {MARKER}'],\n"
        f"                           stdin=subprocess.DEVNULL, stdout={output}, stderr={output})\n"
        f"open({str(pid_file)!r}, 'w').write(str(sleeper.pid))\n",
        encoding="utf-8",
    )
    return [sys.executable, "-I", str(script)], pid_file


def sleeper_ended(pid_file: Path, seconds: float = 10.0) -> bool:
    """Whether the launcher's sleeper ends within ``seconds``.

    One still running then is ended here, so a failing test leaves nothing.
    """
    psutil = pytest.importorskip("psutil")
    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + seconds
    while _running(psutil, pid):
        if time.monotonic() >= deadline:
            try:
                psutil.Process(pid).kill()
            except psutil.Error:
                pass
            return False
        time.sleep(.05)
    return True


def _running(psutil, pid: int) -> bool:
    try:
        process = psutil.Process(pid)
        return process.status() != psutil.STATUS_ZOMBIE and MARKER in " ".join(process.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False

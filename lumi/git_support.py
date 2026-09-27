"""Whether Git is installed, and what to say when it isn't.

Git is optional. A new Windows computer usually has none, and Lumi has to work
there: sessions, coding turns, read-only teams and saved conversations run
without it. What needs Git says so and degrades instead of failing:

* a turn's checkpoint keeps the files in an archive instead of a Git ref
  (engine/checkpoint_timeline.py): restoring works, comparing doesn't;
* writer teams, agent worktrees, mission checkpoints (Settings > Checkpoints
  & recovery) and model comparisons are unavailable;
* the agent's git tools answer that Git isn't installed.

Code that runs ``git`` itself treats a program that can't start (``OSError``,
usually ``FileNotFoundError``) like any failed Git command and never lets it
end a session. ``status()`` is what the app tells the page.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from typing import Sequence

GIT_FOR_WINDOWS_URL = "https://git-scm.com/download/win"
GIT_DOWNLOAD_URL = "https://git-scm.com/downloads"

# Exit status of a Git command whose program could not start; shells use the
# same number for "command not found".
MISSING_EXIT_CODE = 127


def git_executable() -> str | None:
    """The ``git`` program on PATH, or None when Git isn't installed."""
    return shutil.which("git")


def git_available() -> bool:
    return git_executable() is not None


def download_url() -> str:
    return GIT_FOR_WINDOWS_URL if sys.platform == "win32" else GIT_DOWNLOAD_URL


def product_name() -> str:
    return "Git for Windows" if sys.platform == "win32" else "Git"


def missing_message(needs: str = "") -> str:
    """What to tell someone without Git; ``needs`` names what requires it ("Writer teams need")."""
    if needs:
        return (f"{needs} {product_name()}, which isn't installed on this computer. "
                f"Install it from {download_url()} and restart Lumi.")
    return (f"Git isn't installed on this computer. Install {product_name()} from "
            f"{download_url()} and restart Lumi.")


def missing_result(args: Sequence[str], needs: str = "") -> subprocess.CompletedProcess:
    """A failed ``CompletedProcess`` standing in for a Git command that couldn't start."""
    return subprocess.CompletedProcess(list(args), MISSING_EXIT_CODE, "", missing_message(needs))


def status() -> dict:
    """What the page needs to explain a computer without Git (no paths)."""
    available = git_available()
    return {
        "available": available,
        "download_url": download_url(),
        # What doesn't work without it, in the order a notice lists them.
        "unavailable": [] if available else ["writer_teams", "worktrees", "mission_checkpoints", "model_comparisons"],
    }

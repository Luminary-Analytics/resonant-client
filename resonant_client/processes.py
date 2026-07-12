"""Cross-platform subprocess defaults for background agent work."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def background_process_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    """Return platform kwargs for an invisible background child process.

    Resonant is a GUI application. On Windows, console children must not flash
    terminal windows while the agent works. A separate process group remains
    optional because the main tool runner uses it for tree-aware cancellation.
    """

    if sys.platform != "win32":
        return {"start_new_session": True} if new_process_group else {}

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {"creationflags": flags, "startupinfo": startupinfo}

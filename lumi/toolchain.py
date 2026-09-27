"""Which developer programs this computer has, so the agent's prompt doesn't guess.

The runtime layer of every system prompt (engine/session.py) used to tell the
model "Use 'python' not 'python3'" on every Windows computer. A new Windows
computer has no Python, Git or Node.js, and a model told to use them runs
commands that fail. The hints now follow what PATH actually has.

Windows' "App execution aliases" put ``python.exe`` and ``python3.exe`` into
``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` even when Python isn't installed:
running one opens the Microsoft Store. Those count as unverified, not installed.
"""

from __future__ import annotations

import os
import shutil
import sys

PROGRAMS = ("python", "python3", "py", "node", "npm", "git")

_cache: dict[str, dict[str, str]] = {}


def _store_alias(path: str) -> bool:
    return sys.platform == "win32" and f"{os.sep}microsoft{os.sep}windowsapps{os.sep}" in os.path.normcase(path)


def programs() -> dict[str, str]:
    """``{"python": "yes" | "store-alias" | "", ...}`` for PROGRAMS, cached per PATH."""
    key = os.environ.get("PATH", "")
    cached = _cache.get(key)
    if cached is not None:
        return dict(cached)
    found: dict[str, str] = {}
    for name in PROGRAMS:
        path = shutil.which(name)
        found[name] = "" if not path else ("store-alias" if _store_alias(path) else "yes")
    _cache.clear()
    _cache[key] = found
    return dict(found)


def clear_cache() -> None:
    _cache.clear()


def prompt_hints() -> str:
    """The platform lines of the runtime prompt, from the programs this computer has."""
    found = programs()
    if sys.platform != "win32":
        if found["python3"] == "yes":
            python = "Use 'python3'/'pip3'."
        elif found["python"] == "yes":
            python = "Use 'python' and 'python -m pip'."
        else:
            python = "Python isn't installed here: don't run python or pip."
        lines = [python]
    else:
        if found["python"] == "yes":
            python = "Use 'python' not 'python3'. Use 'pip' not 'pip3'."
        elif found["py"] == "yes":
            python = "Python runs through the 'py' launcher here: 'py script.py', 'py -m pip install ...'."
        elif found["python"] == "store-alias" or found["python3"] == "store-alias":
            python = ("'python' here may be only the Microsoft Store placeholder: check `python --version` "
                      "before relying on Python.")
        else:
            python = "Python isn't installed here: don't run python or pip."
        lines = [
            python,
            "Paths use backslashes; shell commands run in cmd.exe.",
            "Unix tools like `tail`, `head`, `sed`, `awk`, `grep`, `wc`, `find` are NOT available — use "
            "`file_read` for inspection, the `grep` agent tool for content search, and `glob` for path listing "
            "instead of shelling out.",
        ]
    missing = [label for name, label in (("git", "Git"), ("node", "Node.js")) if found[name] != "yes"]
    if missing:
        lines.append(f"Not installed here: {', '.join(missing)} — commands that need "
                     f"{'them' if len(missing) > 1 else 'it'} fail.")
    return " ".join(lines)

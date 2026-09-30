"""Keep apart the processes that share ``~/.lumi/logs/lumi-startup.log``.

A windowless packaged Lumi has no console, so ``lumi/__main__.py`` sends its
output there. Every launch appends to the same file: the desktop app, a
terminal UI started without arguments, ``lumi run`` from a scheduled task, an
``updates`` check. Their lines used to interleave with nothing to tell them
apart. Each line now starts with the process's role and id, "[gui 4242] ".
(Team workers never write there: their output goes nowhere by design,
engine/swarming/worker_child.py.)
"""

from __future__ import annotations

import os
import threading
from typing import Sequence

_COMMANDS = frozenset({"gui", "run", "schedule", "gateway", "usage", "updates", "editor", "license", "extension"})


def process_role(argv: Sequence[str]) -> str:
    """What this launch is, from its arguments: a command name, "cli" or "tui"."""
    command = argv[1] if len(argv) > 1 else ""
    if command in _COMMANDS:
        return command
    if command in ("--version", "-V", "--print-pubkey"):
        return "cli"
    return "tui"


class TaggedLog:
    """A text stream that starts every line it writes with ``[role pid] ``.

    Everything else (``fileno``, ``encoding``, ``buffer``...) is the wrapped
    file's, so code that expects a real stream keeps working.
    """

    def __init__(self, stream, role: str, pid: int | None = None) -> None:
        self._stream = stream
        self._tag = f"[{role} {pid if pid is not None else os.getpid()}] "
        self._lock = threading.Lock()
        self._line_start = True

    def write(self, text: str) -> int:
        if not text:
            return 0
        with self._lock:
            pieces = []
            for part in text.splitlines(keepends=True):
                if self._line_start:
                    pieces.append(self._tag)
                pieces.append(part)
                self._line_start = part.endswith(("\n", "\r"))
            self._stream.write("".join(pieces))
        return len(text)

    def writelines(self, lines) -> None:
        for line in lines:
            self.write(line)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return False

    def __getattr__(self, name: str):
        return getattr(self._stream, name)

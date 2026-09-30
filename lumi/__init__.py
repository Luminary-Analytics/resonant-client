"""Lumi — durable agentic coding runtime and desktop client (formerly Resonant)."""
import os

__version__ = "0.20.0.dev0"


def _mirror_legacy_environment() -> None:
    """Honor RESONANT_* settings from before the rebrand as their LUMI_* names.

    Runs at first import, before any module reads its configuration. An
    explicit LUMI_* value always wins over the legacy one.
    """
    for name, value in list(os.environ.items()):
        if name.startswith("RESONANT_"):
            os.environ.setdefault("LUMI_" + name[len("RESONANT_"):], value)


_mirror_legacy_environment()

# Every entry point (the app, the terminal UI, `lumi run`, workers, the
# gateway, scheduled runs) imports this package before it starts any
# process: from here on Windows never looks for a program in the working
# folder, which may be a repository (lumi/executables.py).
from . import executables as _executables  # noqa: E402

_executables.harden_process()

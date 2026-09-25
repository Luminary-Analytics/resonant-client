"""Where Lumi keeps its files, including the move from pre-rebrand locations.

Per-user state lives in ``~/.lumi`` (formerly ``~/.resonant``). Every path is
resolved when it is used, never at import, so tests that redirect the home
directory and a migration at startup both take effect.

Per-project files live in ``<project>/.lumi``. A project that already has a
``.resonant`` folder keeps using it: those files belong to the user's
repository, so Lumi never renames them.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_DIR_NAME = ".lumi"
LEGACY_HOME_DIR_NAME = ".resonant"
PROJECT_DIR_NAME = ".lumi"
LEGACY_PROJECT_DIR_NAME = ".resonant"


def state_home() -> Path:
    """The per-user state directory.

    ``LUMI_STATE_HOME`` wins (a legacy ``RESONANT_STATE_HOME`` is mirrored into
    it at import). Otherwise ``~/.lumi``, unless only ``~/.resonant`` exists:
    an install whose data has not moved yet keeps using it.
    """
    override = os.environ.get("LUMI_STATE_HOME")
    if override:
        return Path(override)
    home = Path.home()
    current = home / HOME_DIR_NAME
    legacy = home / LEGACY_HOME_DIR_NAME
    if not current.exists() and legacy.is_dir():
        return legacy
    return current


def migrate_legacy_home() -> Path | None:
    """Move ``~/.resonant`` to ``~/.lumi`` once, if nothing is in the way.

    Call before anything opens a file under the state directory. Returns the
    new directory when it moved. When the rename fails, for example because a
    still-running older build holds a log or its browser profile open,
    everything stays where it is: ``state_home()`` keeps using the old
    directory and the next launch tries again.
    """
    if os.environ.get("LUMI_STATE_HOME"):
        return None
    home = Path.home()
    current = home / HOME_DIR_NAME
    legacy = home / LEGACY_HOME_DIR_NAME
    if current.exists() or not legacy.is_dir():
        return None
    try:
        legacy.rename(current)
    except OSError:
        return None
    return current


def project_dir(project_path: str | os.PathLike) -> Path:
    """The project's own Lumi folder: ``.lumi``, or an existing ``.resonant``."""
    root = Path(project_path)
    current = root / PROJECT_DIR_NAME
    legacy = root / LEGACY_PROJECT_DIR_NAME
    if not current.exists() and legacy.is_dir():
        return legacy
    return current


def project_dirs(project_path: str | os.PathLike) -> list[Path]:
    """Both possible project folders, current name first, for readers that check each."""
    root = Path(project_path)
    return [root / PROJECT_DIR_NAME, root / LEGACY_PROJECT_DIR_NAME]

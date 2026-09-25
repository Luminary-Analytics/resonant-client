"""A lock shared by every Lumi process that appends to the same file set."""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@contextmanager
def exclusive(path: Path) -> Iterator[None]:
    """Hold an OS lock on ``path`` that every Lumi process respects.

    Used by the audit log and the usage records, which the GUI, the terminal
    UI and the gateway append to. If the lock can't be taken the caller still
    runs, unlocked: a record written out of turn is detectable (the audit
    chain breaks), a lost one isn't.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+b")
    except OSError:
        logger.warning("Could not open the lock file %s", path, exc_info=True)
        yield
        return
    with handle:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            locked = True
        except OSError:
            logger.warning("Could not lock %s", path, exc_info=True)
        try:
            yield
        finally:
            if locked:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass

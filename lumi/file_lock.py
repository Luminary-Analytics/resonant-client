"""A lock shared by every Lumi process that appends to the same file set."""

from __future__ import annotations

import errno
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

# How long a Lumi process waits for another's lock, on every platform. The lock
# is taken with non-blocking attempts, so the wait ends on time wherever it is.
LOCK_SECONDS = 10.0
_RETRY_SECONDS = 0.05
# The errors a non-blocking attempt gives while another process holds the lock
# (Windows' locking says EACCES or EDEADLOCK; flock says EWOULDBLOCK).
_HELD = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}


class LockTimeout(OSError):
    """The lock wasn't had in time: another process kept it, or its drive stopped answering."""


# The open and lock calls threads of this process are in now: (lock file, when the call began), by thread. A
# call that hasn't returned for longer than the wait is a drive that stopped answering (a network share, say):
# no other thread joins it there.
_calls: dict[int, tuple[str, float]] = {}
_calls_guard = threading.Lock()


@contextmanager
def _in_call(key: str) -> Iterator[None]:
    ident = threading.get_ident()
    with _calls_guard:
        _calls[ident] = (key, time.monotonic())
    try:
        yield
    finally:
        with _calls_guard:
            _calls.pop(ident, None)


def _stuck(key: str, wait: float) -> bool:
    now = time.monotonic()
    with _calls_guard:
        return any(held == key and now - since > wait for held, since in _calls.values())


def _attempt(handle) -> bool:
    """One try for the lock without waiting: whether it's ours. OSError when the file can't be locked at all."""
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in _HELD:
            return False
        raise


def _release(handle) -> None:
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


@contextmanager
def exclusive(path: Path, *, wait: float | None = None, required: bool = False) -> Iterator[bool]:
    """Hold an OS lock on ``path`` that every Lumi process respects; yields whether it's held.

    Waits at most ``wait`` seconds (LOCK_SECONDS by default) on every platform.
    The audit log and the usage records, which the GUI, the terminal UI and the
    gateway append to, go on unlocked when the lock can't be had: a record
    written out of turn is detectable (the audit chain breaks), a lost one
    isn't. A caller whose files can't be written out of turn passes
    ``required``, and gets LockTimeout instead. A thread never joins another of
    this process whose open or lock call on the file hasn't returned for longer
    than the wait (a drive that stopped answering): it gives up at once, so
    such a drive ties up one thread, not every one that comes after it.
    """
    wait = LOCK_SECONDS if wait is None else max(0.0, float(wait))
    key = os.path.normcase(os.path.abspath(path))

    def unavailable(why: str) -> Iterator[bool]:
        if required:
            raise LockTimeout(why)
        logger.warning("%s Going on without the lock.", why)
        yield False

    if _stuck(key, wait):
        yield from unavailable(f"A call on the lock file {path} hasn't returned for over {wait:g} seconds.")
        return
    try:
        with _in_call(key):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(path, "a+b")
    except OSError as exc:
        yield from unavailable(f"Could not open the lock file {path} ({exc}).")
        return
    with handle:
        locked = False
        deadline = time.monotonic() + wait
        try:
            while True:
                with _in_call(key):
                    locked = _attempt(handle)
                if locked or time.monotonic() >= deadline:
                    break
                time.sleep(_RETRY_SECONDS)
        except OSError as exc:  # this file system can't lock at all
            yield from unavailable(f"Could not lock {path} ({exc}).")
            return
        if not locked:
            yield from unavailable(f"Another Lumi process kept {path} locked for {wait:g} seconds.")
            return
        try:
            yield True
        finally:
            _release(handle)

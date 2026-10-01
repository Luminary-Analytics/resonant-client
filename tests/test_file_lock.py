"""The lock every Lumi process takes over shared files (lumi/file_lock.py): bounded on every platform.

A second process used to wait for ever on macOS and Linux (``flock`` without
``LOCK_NB``) and about ten seconds on Windows (``LK_LOCK``). Now every
platform tries without blocking until LOCK_SECONDS pass; then the audit log
and usage records go on unlocked, as they always did, and a caller that
requires the lock gets LockTimeout. A thread never joins another whose open
or lock call hasn't returned (a drive that stopped answering).
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lumi import file_lock

HOLD = ("import sys, time\n"
        "from pathlib import Path\n"
        "from lumi import file_lock\n"
        "with file_lock.exclusive(Path(sys.argv[1])) as held:\n"
        "    print('held' if held else 'not held', flush=True)\n"
        "    time.sleep(float(sys.argv[2]))\n")


@pytest.fixture
def holder(tmp_path):
    """Another process that holds a lock file for a while (the reviewer's lock_stall.py)."""
    started: list[subprocess.Popen] = []

    def hold(path: Path, seconds: float) -> subprocess.Popen:
        env = {**os.environ, "USERPROFILE": str(tmp_path), "HOME": str(tmp_path), "LUMI_KEYCHAIN": "off",
               "LUMI_STATE_HOME": str(tmp_path / "state"), "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        process = subprocess.Popen([sys.executable, "-c", HOLD, str(path), str(seconds)], env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started.append(process)
        assert process.stdout.readline().strip() == "held", process.stderr.read()
        return process

    yield hold
    for process in started:
        process.kill()
        process.communicate(timeout=30)


def test_a_lock_another_process_keeps_is_waited_for_a_bounded_time(tmp_path, holder, monkeypatch):
    lock = tmp_path / "shared" / ".lock"
    holder(lock, 30)
    monkeypatch.setattr(file_lock, "LOCK_SECONDS", 0.5)
    started = time.monotonic()
    with file_lock.exclusive(lock) as held:  # the audit log's way: on, unlocked, after the wait
        waited = time.monotonic() - started
    assert held is False and 0.4 <= waited < 5
    started = time.monotonic()
    with pytest.raises(file_lock.LockTimeout, match="kept .* locked"):
        with file_lock.exclusive(lock, required=True):
            pass
    assert time.monotonic() - started < 5
    with file_lock.exclusive(lock, wait=0) as held:  # no wait at all: one try
        assert held is False


def test_the_lock_is_held_and_released(tmp_path, monkeypatch):
    lock = tmp_path / ".lock"
    monkeypatch.setattr(file_lock, "LOCK_SECONDS", 0.3)
    with file_lock.exclusive(lock, required=True) as held:
        assert held is True
        # Another handle (another process's, or this process's) can't have it meanwhile.
        with file_lock.exclusive(lock) as other:
            assert other is False
    with file_lock.exclusive(lock, required=True) as held:  # free again
        assert held is True


def test_a_call_on_a_drive_that_stopped_answering_ties_up_one_thread_only(tmp_path, monkeypatch):
    """A thread stuck in the lock file's open or lock call (a hung network share): others give up at once."""
    lock = tmp_path / ".lock"
    key = os.path.normcase(os.path.abspath(lock))
    stuck = threading.Event()
    release = threading.Event()
    real_open = open

    def hanging_open(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(path)) == key and not release.is_set():
            stuck.set()
            release.wait(30)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(file_lock, "open", hanging_open, raising=False)
    first = threading.Thread(target=lambda: file_lock.exclusive(lock, wait=0.2).__enter__(), daemon=True)
    first.start()
    assert stuck.wait(10)
    time.sleep(0.3)  # longer than the wait: the call counts as stuck
    started = time.monotonic()
    with file_lock.exclusive(lock, wait=0.2) as held:
        assert held is False
    with pytest.raises(file_lock.LockTimeout, match="hasn't returned"):
        with file_lock.exclusive(lock, wait=0.2, required=True):
            pass
    assert time.monotonic() - started < 0.2  # at once, not after another wait
    release.set()
    first.join(10)

import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from lumi import processes
from lumi.engine.tools import (
    _exec_bash,
    _normalize_managed_bash_command,
    _run_subprocess_with_cancel,
)
from tests.quick_launcher import assign_jobs_late, quick_launcher, sleeper_ended


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_is_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.15):
            return True
    except OSError:
        return False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows start /B behavior")
def test_screenshot_server_command_is_kept_in_managed_foreground():
    command = r"cd D:\Repos\battleship-2d && start /B node server.js 2>&1"

    normalized = _normalize_managed_bash_command(command)

    assert normalized == r"cd D:\Repos\battleship-2d && node server.js 2>&1"


@pytest.mark.parametrize("job_object", [True, False], ids=["job-object", "without-job-object"])
def test_cancel_terminates_long_running_shell_process_tree(tmp_path, monkeypatch, job_object):
    if not job_object:
        # Best effort: a command the job can't take still runs, and Windows
        # stops it with taskkill /T instead.
        def no_job(process, **kwargs):
            raise OSError("no job object")

        monkeypatch.setattr(processes, "windows_kill_job", no_job)
    port = _free_port()
    cancel = threading.Event()
    command = f'"{sys.executable}" -m http.server {port} --bind 127.0.0.1'

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _run_subprocess_with_cancel,
            command,
            timeout=30,
            shell=True,
            text=True,
            cwd=str(tmp_path),
            cancel_event=cancel,
        )

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _port_is_open(port):
            time.sleep(0.05)
        assert _port_is_open(port), "HTTP server child process never started"

        cancel.set()
        returncode, _stdout, _stderr, timed_out = future.result(timeout=6)

    assert returncode != 0
    assert not timed_out
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and _port_is_open(port):
        time.sleep(0.05)
    assert not _port_is_open(port), "cancel left the HTTP server child process alive"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_the_timeout_stops_what_a_quick_command_left_holding_its_output(tmp_path, monkeypatch):
    # The command exits at once, leaving a program that holds its output, so
    # it runs until its timeout, when its job stops that program. The job used
    # to be assigned after the command started: one that had exited by then
    # was left out of it, and the tool waited for the program to end by itself.
    assign_jobs_late(monkeypatch)
    argv, pid_file = quick_launcher(tmp_path, holds_output=True)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _run_subprocess_with_cancel,
            argv,
            timeout=3,
            shell=False,
            text=True,
            cwd=str(tmp_path),
        )
        try:
            timed_out = future.result(timeout=20)[3]
        finally:
            # Also ends a program the timeout missed, so the call can return.
            ended = sleeper_ended(pid_file)

    assert timed_out
    assert ended


@pytest.mark.skipif(sys.platform != "win32", reason="Windows start /B behavior")
@pytest.mark.parametrize("launch_prefix", ["start /B ", 'start "Lumi Server" /B '])
def test_bash_keeps_start_b_server_attached_for_cancellation(tmp_path, launch_prefix):
    port = _free_port()
    cancel = threading.Event()
    command = f"{launch_prefix}python -m http.server {port} --bind 127.0.0.1"

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _exec_bash,
            {"command": command, "timeout": 30, "cwd": str(tmp_path)},
            time.time(),
            cancel,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not _port_is_open(port):
            time.sleep(0.05)
        assert _port_is_open(port)
        cancel.set()
        result = future.result(timeout=6)

    assert result.metadata.get("cancelled") is True
    assert not _port_is_open(port)

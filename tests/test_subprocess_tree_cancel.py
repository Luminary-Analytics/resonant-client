import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from resonant_client.engine.tools import (
    _exec_bash,
    _normalize_managed_bash_command,
    _run_subprocess_with_cancel,
)


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


def test_cancel_terminates_long_running_shell_process_tree(tmp_path):
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows start /B behavior")
@pytest.mark.parametrize("launch_prefix", ["start /B ", 'start "Resonant Server" /B '])
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

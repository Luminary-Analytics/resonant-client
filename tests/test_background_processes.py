import os
from pathlib import Path
import subprocess
import sys

import pytest

from lumi import processes


def test_background_process_kwargs_are_empty_on_non_windows(monkeypatch):
    monkeypatch.setattr(processes.sys, "platform", "linux")
    assert processes.background_process_kwargs() == {}
    assert processes.background_process_kwargs(new_process_group=True) == {
        "start_new_session": True,
    }


def test_background_process_kwargs_hide_windows_console(monkeypatch):
    class FakeStartupInfo:
        def __init__(self):
            self.dwFlags = 0
            self.wShowWindow = None

    monkeypatch.setattr(processes.sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "STARTUPINFO", FakeStartupInfo, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200, raising=False)
    monkeypatch.setattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001, raising=False)
    monkeypatch.setattr(subprocess, "SW_HIDE", 0, raising=False)

    kwargs = processes.background_process_kwargs(new_process_group=True)

    assert kwargs["creationflags"] == 0x08000200
    assert kwargs["startupinfo"].dwFlags & 0x00000001
    assert kwargs["startupinfo"].wShowWindow == 0


@pytest.mark.parametrize("owned_group", [False, True])
def test_main_tool_runner_combines_hidden_window_and_process_group_policy(tmp_path, monkeypatch, owned_group):
    from lumi.engine import tools
    captured = []
    original = subprocess.Popen
    def launch(*args, **kwargs):
        captured.append(kwargs)
        return original(*args, **kwargs)
    monkeypatch.setattr(tools.subprocess, "Popen", launch)
    result = tools._run_subprocess_with_cancel(
        [sys.executable, "-c", "print('Observed fixture child')"], timeout=10,
        shell=False, text=True, cwd=str(tmp_path), owned_process_group=owned_group)
    assert result == (0, "Observed fixture child\n", "", False)
    assert len(captured) == 1
    options = captured[0]
    if sys.platform == "win32":
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert bool(options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP) is (not owned_group)
        assert options["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
        assert options["startupinfo"].wShowWindow == subprocess.SW_HIDE
    else:
        assert options.get("start_new_session", False) is (not owned_group)
    assert options["stdin"] == (subprocess.DEVNULL if owned_group else None)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_popen_in_kill_job_owns_a_quick_child_and_its_descendants_despite_a_late_assignment(monkeypatch):
    # Assigning a job after Popen returns loses to a child that exits first
    # ("Access is denied"), and whatever the child started meanwhile is
    # outside the job. A suspended start leaves nothing to race.
    import time

    import psutil

    hidden = processes.background_process_kwargs(new_process_group=True)
    assign = processes.windows_kill_job
    finished = subprocess.Popen([sys.executable, "-c", "pass"], **hidden)
    finished.wait(timeout=60)
    with pytest.raises(PermissionError):
        assign(finished)

    def late(process, **kwargs):
        time.sleep(.5)  # the child below would have started its own and exited by now
        return assign(process, **kwargs)
    monkeypatch.setattr(processes, "windows_kill_job", late)
    code = ("import subprocess, sys; "
            "print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid)")
    process, job = processes.popen_in_kill_job([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, **hidden)
    try:
        output, _ = process.communicate(timeout=60)
        assert process.returncode == 0
        descendant = psutil.Process(int(output))
        assert descendant.is_running()
    finally:
        processes.close_windows_job(job)  # kill-on-close ends everything in the job
    descendant.wait(timeout=30)
    assert not descendant.is_running()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_a_normal_owned_launch_leaves_no_launch_record():
    process, job = processes.popen_in_kill_job([sys.executable, "-c", "pass"],
                                               **processes.background_process_kwargs(new_process_group=True))
    try:
        assert process.wait(timeout=60) == 0
    finally:
        processes.close_windows_job(job)
    assert not list(processes._LAUNCH_RECORDS.glob(f"{os.getpid()}-{process.pid}-*"))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_a_child_left_suspended_outside_its_job_by_a_dead_host_is_ended_later():
    # A host that dies after creating a child suspended and before assigning
    # its job leaves the child suspended forever, outside any job. Its launch
    # record names both by pid and creation time, and a later host ends it.
    import psutil

    source = Path(__file__).resolve().parents[1]
    code = f"""
import subprocess, sys, time
sys.path.insert(0, {str(source)!r})
from lumi import processes
def stuck(process, **kwargs):
    print(process.pid, flush=True)
    time.sleep(600)  # killed here, before the child has a job
processes.windows_kill_job = stuck
processes.popen_in_kill_job([sys.executable, "-c", "import time; time.sleep(600)"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
"""
    host = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                            **processes.background_process_kwargs(new_process_group=True))
    child, created = None, None
    try:
        child = psutil.Process(int(host.stdout.readline()))
        created = child.create_time()
        records = list(processes._LAUNCH_RECORDS.glob(f"{host.pid}-{child.pid}-*.json"))
        assert records
        host.kill()
        host.wait(timeout=30)
        assert child.is_running() and child.create_time() == created  # suspended, and no job will end it
        assert processes.reap_orphaned_launches(force=True) >= 1
        child.wait(timeout=30)
        assert not any(path.exists() for path in records)
    finally:
        if host.poll() is None:
            host.kill()
            host.wait(timeout=30)
        host.stdout.close()
        if child is not None:
            try:
                if child.is_running() and child.create_time() == created:
                    child.kill()
            except psutil.NoSuchProcess:
                pass


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_a_live_hosts_launch_record_is_left_alone():
    # Its host is between creating the child and assigning its job right now.
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], creationflags=0x4,  # CREATE_SUSPENDED
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    record = processes._record_launch(child)
    try:
        assert record is not None
        processes.reap_orphaned_launches(force=True)
        assert child.poll() is None and record.exists()
    finally:
        child.kill()
        child.wait(timeout=30)
        processes._forget_launch(record)

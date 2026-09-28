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
            "print(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], stdin=subprocess.DEVNULL, "
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


def _refuse_jobs_late(monkeypatch):
    """A job that can't take its process, found out only after a while."""
    import time

    seen = []

    def refuse(process, **kwargs):
        seen.append(process)
        time.sleep(.5)  # a process that was running has left its mark by now
        raise OSError("no job object")
    monkeypatch.setattr(processes, "windows_kill_job", refuse)
    return seen


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_popen_in_kill_job_never_runs_a_process_its_job_refused(tmp_path, monkeypatch):
    seen = _refuse_jobs_late(monkeypatch)
    mark = tmp_path / "ran"
    with pytest.raises(OSError, match="no job object"):
        processes.popen_in_kill_job([sys.executable, "-I", "-c", f"open({str(mark)!r}, 'w').close()"],
                                    **processes.background_process_kwargs(new_process_group=True))
    assert not mark.exists()
    assert seen[0].poll() is not None  # ended, not left suspended


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_popen_in_kill_job_best_effort_runs_a_process_its_job_refused(tmp_path, monkeypatch):
    # For callers with a fallback: hooks (taskkill), language servers and the
    # agent's shell.
    _refuse_jobs_late(monkeypatch)
    mark = tmp_path / "ran"
    process, job = processes.popen_in_kill_job(
        [sys.executable, "-I", "-c", f"open({str(mark)!r}, 'w').close()"], best_effort=True,
        **processes.background_process_kwargs(new_process_group=True))
    assert job is None
    assert process.wait(timeout=60) == 0 and mark.exists()

"""Cross-platform subprocess defaults for background agent work."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Any
import uuid


def windows_kill_job(process, *, kill_on_close: bool = True, name=None):
    """Own a process tree until this handle closes, including application exit.

    With ``kill_on_close=False`` the job only groups the tree: closing the
    handle leaves it running, and ``terminate_windows_job`` stops it. A
    ``name`` gives the job a system-wide identity; if a job with that name
    already exists, this refuses instead of joining another owner's job.
    """
    if sys.platform != 'win32':
        return None
    import ctypes
    from ctypes import wintypes
    class Basic(ctypes.Structure):
        _fields_ = [('user_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                    ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t),
                    ('max_working', ctypes.c_size_t), ('active', wintypes.DWORD),
                    ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_ulonglong * 6), ('memory', ctypes.c_size_t * 4)]
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    api.CreateJobObjectW.restype = wintypes.HANDLE
    api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateJobObjectW(None, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if name and ctypes.get_last_error() == 183:
        api.CloseHandle(handle)
        raise OSError("Owned process job identity already exists")
    info = Extended()
    info.basic.flags = 0x2000 if kill_on_close else 0
    if not api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not api.AssignProcessToJobObject(handle, int(process._handle)):
        error = ctypes.get_last_error()
        api.CloseHandle(handle)
        raise ctypes.WinError(error)
    return (api, handle)


def popen_in_kill_job(args, *, name=None, **popen_kwargs):
    """Start a process that runs none of its code outside its job: ``(process, job)``.

    Assigning the job after ``Popen`` returns races a short-lived child. Once
    the child has exited, AssignProcessToJobObject fails with "Access is
    denied", and on a loaded host, where other threads hold the GIL between
    ``Popen`` and the assignment, a quick ``git`` read often exits first. So on
    Windows the process starts suspended, joins a kill-on-close job, then
    resumes; anything it starts is inside the job from the first instruction.
    Elsewhere there is no job (``None``); callers own POSIX trees through
    their process group.

    If this host dies after creating the child and before its job is assigned,
    the child stays suspended outside any job. A record of each child between
    creation and resumption lets the next Lumi process end such a leftover
    (``reap_orphaned_launches``).
    """
    if sys.platform != "win32":
        return subprocess.Popen(args, **popen_kwargs), None
    reap_orphaned_launches()
    popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
    process = subprocess.Popen(args, **popen_kwargs)
    record = _record_launch(process)
    job = None
    try:
        job = windows_kill_job(process, name=name)
        _resume_suspended(process)
    except BaseException:
        # Suspended, so it has run nothing: end it before closing the job.
        process.kill()
        process.wait(timeout=5)
        close_windows_job(job)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise
    finally:
        # Once assigned, kill-on-close ends the child with this host, so the
        # record is only needed until then; it is removed after resuming.
        _forget_launch(record)
    return process, job


# Children created suspended whose job assignment isn't confirmed yet, one
# small file each: {"host": [pid, creation], "child": [pid, creation]}.
_LAUNCH_RECORDS = Path(tempfile.gettempdir()) / "lumi-owned-launches"
_reap_lock = threading.Lock()
_reaped = False


def _process_created(handle) -> int:
    """A process's creation time (FILETIME ticks), which a reused pid never shares."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(ctypes.c_ulonglong)] * 4
    kernel32.GetProcessTimes.restype = ctypes.c_int
    times = [ctypes.c_ulonglong() for _ in range(4)]
    if not kernel32.GetProcessTimes(ctypes.c_void_p(handle), *(ctypes.byref(value) for value in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return times[0].value


def _record_launch(process) -> Path | None:
    """Record a suspended child before its job is assigned; best effort."""
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        value = {"host": [os.getpid(), _process_created(kernel32.GetCurrentProcess())],
                 "child": [process.pid, _process_created(int(process._handle))]}
        _LAUNCH_RECORDS.mkdir(parents=True, exist_ok=True)
        path = _LAUNCH_RECORDS / f"{os.getpid()}-{process.pid}-{uuid.uuid4().hex[:8]}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value), encoding="utf-8")
        os.replace(temporary, path)  # a reader never sees a partial record
        return path
    except (OSError, ValueError):
        return None


def _forget_launch(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass


def _open_exact(pid: int, created: int, access: int):
    """Open the process with this pid only if it is the one created then; else None.

    Returns ``False`` when the pid can't be inspected (another user's process),
    so a caller can tell "gone" from "unknown".
    """
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel32.OpenProcess(access | 0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None if ctypes.get_last_error() == 87 else False  # ERROR_INVALID_PARAMETER: no such pid
    try:
        if _process_created(handle) == created:
            return handle
    except OSError:
        pass
    kernel32.CloseHandle(handle)
    return None


def reap_orphaned_launches(*, force: bool = False) -> int:
    """End children a dead Lumi host left suspended outside their job.

    A host that died between creating a child and assigning its job leaves the
    child suspended forever, holding its working folder. Each record names the
    host and child by pid and creation time; a record whose host is gone and
    whose child still runs is such a leftover. A live host's records are left
    alone, since it is between those steps now. Runs once per process unless
    ``force`` (recovery asks again). Returns how many children it ended.
    """
    global _reaped
    if sys.platform != "win32":
        return 0
    with _reap_lock:
        if _reaped and not force:
            return 0
        _reaped = True
    try:
        paths = sorted(_LAUNCH_RECORDS.glob("*.json"))
    except OSError:
        return 0
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.WaitForSingleObject.restype = ctypes.c_uint32
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    ended = 0
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            (host_pid, host_created), (child_pid, child_created) = record["host"], record["child"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
        host = _open_exact(host_pid, host_created, 0x100000)  # SYNCHRONIZE
        if host is False:
            continue  # can't tell whether its host lives: leave it
        if host is not None:
            alive = kernel32.WaitForSingleObject(host, 0) == 0x102  # WAIT_TIMEOUT: still running
            kernel32.CloseHandle(host)
            if alive:
                continue
        child = _open_exact(child_pid, child_created, 0x100000 | 0x1)  # SYNCHRONIZE | PROCESS_TERMINATE
        if child is False:
            continue
        if child is not None:
            try:
                if kernel32.WaitForSingleObject(child, 0) == 0x102 and kernel32.TerminateProcess(child, 1):
                    kernel32.WaitForSingleObject(child, 5000)
                    ended += 1
            finally:
                kernel32.CloseHandle(child)
        _forget_launch(path)
    return ended


def _resume_suspended(process) -> None:
    """Resume the only thread of a process created with CREATE_SUSPENDED.

    ``subprocess`` closes the primary thread handle, so this resumes through
    the process handle (NtResumeProcess, which psutil's ``resume`` also uses).
    """
    import ctypes
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    status = ntdll.NtResumeProcess(ctypes.c_void_p(int(process._handle)))
    if status < 0:
        raise OSError(f"Could not resume the suspended process (NTSTATUS {status & 0xFFFFFFFF:#010x})")


def terminate_windows_job(job, exit_code: int = 1):
    """Stop every process in the job now."""
    if job:
        job[0].TerminateJobObject(job[1], exit_code)


def close_windows_job(job):
    if job:
        job[0].CloseHandle(job[1])


def background_process_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    """Return platform kwargs for an invisible background child process.

    Lumi is a GUI application. On Windows, console children must not flash
    terminal windows while the agent works. A separate process group remains
    optional because the main tool runner uses it for tree-aware cancellation.
    """

    if sys.platform != "win32":
        return {"start_new_session": True} if new_process_group else {}

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {"creationflags": flags, "startupinfo": startupinfo}

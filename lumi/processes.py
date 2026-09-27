"""Cross-platform subprocess defaults for background agent work."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


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


def popen_in_kill_job(args, *, kill_on_close: bool = True, name=None, **popen_kwargs):
    """Start a process that runs none of its code outside its job: ``(process, job)``.

    Assigning the job after ``Popen`` returns races a short-lived child. Once
    the child has exited, AssignProcessToJobObject fails with "Access is
    denied", and on a loaded host, where other threads hold the GIL between
    ``Popen`` and the assignment, a quick ``git`` read often exits first. So on
    Windows the process starts suspended, joins the job, then resumes; anything
    it starts is inside the job from the first instruction. Elsewhere there is
    no job (``None``); callers own POSIX trees through their process group.
    """
    if sys.platform != "win32":
        return subprocess.Popen(args, **popen_kwargs), None
    popen_kwargs["creationflags"] = popen_kwargs.get("creationflags", 0) | 0x4  # CREATE_SUSPENDED
    process = subprocess.Popen(args, **popen_kwargs)
    job = None
    try:
        job = windows_kill_job(process, kill_on_close=kill_on_close, name=name)
        _resume_suspended(process)
    except BaseException:
        # Suspended, so it has run nothing; a job without kill-on-close would
        # not stop it on close, so terminate it directly.
        process.kill()
        process.wait(timeout=5)
        close_windows_job(job)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        raise
    return process, job


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

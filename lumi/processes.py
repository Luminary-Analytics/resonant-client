"""Cross-platform subprocess defaults for background agent work."""

from __future__ import annotations

import subprocess
import sys
from typing import Any


def windows_kill_job(process):
    """Own a process tree until this handle closes, including application exit."""
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
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    info.basic.flags = 0x2000
    if not api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not api.AssignProcessToJobObject(handle, int(process._handle)):
        error = ctypes.get_last_error()
        api.CloseHandle(handle)
        raise ctypes.WinError(error)
    return (api, handle)


def close_windows_job(job):
    if job:
        job[0].CloseHandle(job[1])


def background_process_kwargs(*, new_process_group: bool = False) -> dict[str, Any]:
    """Return platform kwargs for an invisible background child process.

    Resonant is a GUI application. On Windows, console children must not flash
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

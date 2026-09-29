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
import unicodedata
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


def _windows_code_page(function: str, default: str) -> str:
    try:
        import ctypes

        page = int(getattr(ctypes.windll.kernel32, function)())
    except Exception:
        page = 0
    return f"cp{page}" if page else default


def _oem_code_page() -> str:
    """The Windows OEM code page (cp437, cp850, ...): a console's default."""
    return _windows_code_page("GetOEMCP", "cp437")


def _ansi_code_page() -> str:
    """The Windows ANSI code page (cp1252, ...): what Python writes to a pipe outside UTF-8 mode."""
    return _windows_code_page("GetACP", "cp1252")


def _plausibility(text: str) -> int:
    """How much a legacy decoding of a line reads like text: Latin letters for it, undefined bytes against.

    One byte means different characters in the two code pages: 0xF6 is "ö"
    in cp1252 and "÷" in cp437, 0x94 is "”" in cp1252 and "ö" in cp437, and
    0x81 is undefined in cp1252. The decoding with more letters wins.
    """
    score = 0
    for char in text:
        if char < "\x80":
            continue
        if char == "\ufffd":
            score -= 8
        elif unicodedata.category(char) in ("Lu", "Ll") and "LATIN" in unicodedata.name(char, ""):
            score += 2
    return score


def _decode_line(line: bytes, pages: tuple[str, ...]) -> str:
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best, best_score = None, 0
    for page in pages:  # the first wins a tie
        try:
            text = line.decode(page, errors="replace")
        except LookupError:
            continue
        score = _plausibility(text)
        if best is None or score > best_score:
            best, best_score = text, score
    return best if best is not None else line.decode("utf-8", errors="replace")


def decode_output(data: bytes | str | None, *, oem_code_page: str | None = None,
                  ansi_code_page: str | None = None) -> str:
    """The text of a command's output, whatever produced it; never raises.

    Children are asked to write UTF-8 (``utf8_env``, ``utf8_shell``), and Git
    and Node always do, so output is UTF-8 first. What still isn't comes
    from a program that ignores that: Python with ``PYTHONUTF8=0`` writes the
    ANSI code page (cp1252), cmd.exe outside ``utf8_shell`` and findstr's file
    names the console's OEM one (cp437, cp850). One command can mix them, so
    each line is decoded on its own: as UTF-8 when it is valid UTF-8, else
    with whichever of the ANSI and OEM code pages reads as more letters
    (``_plausibility``). Off Windows the fallback is UTF-8 with U+FFFD.
    Line endings become "\\n", as in a text-mode pipe. The code page
    arguments are for tests.
    """
    if data is None:
        return ""
    if isinstance(data, str):
        text = data
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if sys.platform == "win32" or oem_code_page or ansi_code_page:
                pages = tuple(dict.fromkeys((ansi_code_page or _ansi_code_page(), oem_code_page or _oem_code_page())))
                text = "\n".join(_decode_line(line, pages) for line in data.split(b"\n"))
            else:
                text = data.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _utf8_boundary(data: bytes) -> int:
    """Where ``data`` can be cut without splitting a UTF-8 character or a CR LF pair."""
    end = start = len(data)
    while start > 0 and end - start < 3 and data[start - 1] & 0xC0 == 0x80:
        start -= 1
    if start > 0:
        lead = data[start - 1]
        size = 2 if 0xC0 <= lead < 0xE0 else 3 if 0xE0 <= lead < 0xF0 else 4 if 0xF0 <= lead < 0xF8 else 1
        if size > 1 and start - 1 + size > end:
            end = start - 1
    if end > 1 and data[end - 1] == 0x0D:
        end -= 1
    return end or len(data)


class OutputDecoder:
    """``decode_output`` for output read in pieces, such as a job's or preview's log.

    A read can end inside a character ("ü" is two bytes in UTF-8) and one
    line's code page can differ from the next one's, so only whole lines are
    decoded; the rest waits for the next read, or ``final``. A line longer than
    ``limit`` bytes is decoded in parts, never inside a UTF-8 character.
    """

    def __init__(self, limit: int = 8192, **code_pages: str):
        self._pending = b""
        self._limit = limit
        self._code_pages = code_pages

    def decode(self, data: bytes, *, final: bool = False) -> str:
        self._pending += data
        if final:
            end = len(self._pending)
        else:
            end = self._pending.rfind(b"\n") + 1
            if not end and len(self._pending) >= self._limit:
                end = _utf8_boundary(self._pending)
        ready, self._pending = self._pending[:end], self._pending[end:]
        return decode_output(ready, **self._code_pages) if ready else ""


# The command utf8_shell passes to the inner cmd.exe (see there).
SHELL_COMMAND_VARIABLE = "LUMI_SHELL_COMMAND"


def utf8_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """A child's environment in which Python writes UTF-8 to a pipe: ``PYTHONUTF8=1``.

    Without it a child Python writes the ANSI code page (cp1252), and "Jöhn"
    came back as "J÷hn" when read as the OEM one. A value the person set,
    ``PYTHONUTF8=0`` included, is kept; ``decode_output`` reads that too.
    """
    environment = dict(os.environ if env is None else env)
    if not any(key.upper() == "PYTHONUTF8" for key in environment):
        environment["PYTHONUTF8"] = "1"
    return environment


def _cmd_exe() -> str:
    """Windows' own cmd.exe, by its full path (never a program named cmd in the working folder)."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    return os.path.join(root, "System32", "cmd.exe")


def utf8_shell(command: str, env: dict[str, str] | None = None) -> tuple[str, bool, dict[str, str]]:
    """How to run a shell command whose output is read as text: ``(args, shell, env)`` for Popen.

    Elsewhere that is the command itself with ``shell=True`` and ``utf8_env``.
    On Windows cmd.exe and its built-ins (echo, dir, set) write the console's
    code page, the OEM one (cp437: "ü" is 0x81). ``chcp 65001`` switches the
    console to UTF-8, but the cmd.exe that runs it keeps writing the old code
    page for the rest of its command line; a cmd.exe started after it writes
    UTF-8. So the outer cmd.exe switches the code page and starts a second one
    for the command. The command reaches it through an environment variable
    that the outer cmd.exe expands only after parsing its own line
    (``/v:on``, ``!name!``), so the outer one never interprets the command's
    quotes, ``&``, ``|``, ``%`` or ``!``; the inner cmd.exe (``/s /c``) runs it
    exactly as ``cmd.exe /c`` did before, with the command's exit status.
    Programs that follow the console's code page (find, sort, where, .NET)
    then write UTF-8 too. Both cmd.exe paths are absolute, and the outer one
    skips AutoRun (``/d``) so the person's AutoRun command runs once, in the
    inner one, as before.
    """
    environment = utf8_env(env)
    if sys.platform != "win32":
        return command, True, environment
    cmd = _cmd_exe()
    environment[SHELL_COMMAND_VARIABLE] = command
    line = f'"{cmd}" /d /v:on /s /c "chcp 65001>nul 2>&1 & "{cmd}" /s /c "!{SHELL_COMMAND_VARIABLE}!""'
    return line, False, environment

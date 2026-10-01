"""Cross-platform subprocess defaults for background agent work."""

from __future__ import annotations

import codecs
from collections.abc import Mapping
import functools
import json
import os
from pathlib import Path
import re
import signal
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


@functools.lru_cache(maxsize=None)
def _oem_code_page() -> str:
    """The Windows OEM code page (cp437, cp850, ...): what cmd.exe and console programs write to a pipe."""
    return _windows_code_page("GetOEMCP", "cp437")


@functools.lru_cache(maxsize=None)
def _ansi_code_page() -> str:
    """The Windows ANSI code page (cp1252, ...): old text files, Java, Python outside UTF-8."""
    return _windows_code_page("GetACP", "cp1252")


# What a byte is in a code page, for telling which one a line was written in
# (_byte_classes): an ASCII letter ("l", "U"), another Latin letter ("m",
# "M"), a letter of another script ("n", "N"), a box-drawing line or block
# ("h"), other box drawing ("b"), a byte the code page leaves undefined
# ("!"), a space, or anything else ("."). A wrong code page reads as capitals
# inside words ("GenŠve" for "Genève", "SÒo" for "São"), and as box drawing
# that isn't drawn the way tree and tables draw it: lines in runs, corners
# and junctions against a line, a line before a space ("├─── src", "│   a";
# redirected, tree writes the ANSI code page's "¦   a").
_BOX_LINES = frozenset("\u00a6\u2500\u2501\u2502\u2503\u2504\u2505\u2506\u2507\u2508\u2509\u250a\u250b"
                       "\u2550\u2551\u254c\u254d\u254e\u254f\u2580\u2584\u2588\u258c\u2590\u2591\u2592\u2593")
_CASE_BREAK = re.compile(rb"[lmn][MN]|[mn]U|[UMN][MN](?=[lmn])")
_BOX_RUN = re.compile(rb"h(?=[hb ])|(?<=[hb])h|b(?=[h ])|(?<=h)b")


@functools.lru_cache(maxsize=32)
def _byte_classes(page: str) -> bytes | None:
    """Each byte's class in a single-byte code page, as a ``bytes.translate`` table; None for any other."""
    if codecs.lookup(page).name.startswith("utf"):
        return None
    high = bytes(range(0x80, 0x100)).decode(page, errors="replace")
    if len(high) != 0x80:
        return None  # a multi-byte code page (cp932, cp936): one byte alone isn't a character
    table = bytearray()
    for byte in range(0x100):
        char = chr(byte) if byte < 0x80 else high[byte - 0x80]
        category = unicodedata.category(char)
        if "a" <= char <= "z":
            kind = "l"
        elif "A" <= char <= "Z":
            kind = "U"
        elif char in " \t":
            kind = " "
        elif char == "\ufffd":
            kind = "!"
        elif char in _BOX_LINES:
            kind = "h"
        elif "\u2500" <= char <= "\u259f":
            kind = "b"
        elif category in ("Ll", "Lu"):
            latin = "LATIN" in unicodedata.name(char, "")
            kind = ("m" if latin else "n") if category == "Ll" else ("M" if latin else "N")
        else:
            kind = "."
        table.append(ord(kind))
    return bytes(table)


def _score(line: bytes, page: str) -> int:
    """How much ``line`` reads as text in ``page``, counted in C: letters and box runs for it, breaks against.

    One byte is a different character in each code page: 0x94 is "ö" in
    cp437 and "”" in cp1252, 0xF6 is "ö" in cp1252 and "÷" in cp437, 0x81
    is "ü" in cp437 and undefined in cp1252. Latin letters count 2, other
    scripts' letters 1, box drawing drawn as such 2, a capital inside a word
    -4 and an undefined byte -8.
    """
    table = _byte_classes(page)
    if table is None:
        return -8 * line.decode(page, errors="replace").count("\ufffd")
    kinds = line.translate(table)
    return (2 * (kinds.count(b"m") + kinds.count(b"M")) + kinds.count(b"n") + kinds.count(b"N")
            + 2 * len(_BOX_RUN.findall(kinds)) - 4 * len(_CASE_BREAK.findall(kinds)) - 8 * kinds.count(b"!"))


def _decode_line(line: bytes, pages: tuple[str, ...]) -> str:
    """One line: as UTF-8 when it is valid UTF-8, else in the code page it reads best in (the first wins a tie)."""
    try:
        return line.decode("utf-8")
    except UnicodeDecodeError:
        pass
    best, best_score = "", 0
    for page in pages:
        try:
            score = _score(line, page)
        except LookupError:
            continue
        if not best or score > best_score:
            best, best_score = page, score
    return line.decode(best or "utf-8", errors="replace")


def decode_output(data: bytes | str | None, *, prefer: str = "oem", oem_code_page: str | None = None,
                  ansi_code_page: str | None = None) -> str:
    """The text of a command's output, whatever produced it; never raises.

    Python children write UTF-8 (``utf8_env``), and so do Git and Node, so
    output is UTF-8 first: all of it at once when it is, else line by line.
    A line that isn't comes from a program that writes a Windows code page:
    cmd.exe, its built-ins and console programs (find, sort, tree,
    PowerShell) the console's OEM one (cp437, cp850), and old text files,
    Java or a Python the person set up otherwise the ANSI one (cp1252). One
    command can mix them, so each such line is decoded in whichever of the
    two reads more like text (``_score``), and ``prefer`` wins a tie: the OEM
    code page for what commands print, the ANSI one for files' own text
    (search results). A line seen before in the same output is decided once.
    Off Windows the fallback is UTF-8 with U+FFFD. Line endings become
    "\\n", as in a text-mode pipe. The code page arguments are for tests.
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
                oem, ansi = oem_code_page or _oem_code_page(), ansi_code_page or _ansi_code_page()
                pages = tuple(dict.fromkeys((ansi, oem) if prefer == "ansi" else (oem, ansi)))
                decided: dict[bytes, str] = {}
                lines = []
                for line in data.split(b"\n"):
                    found = decided.get(line)
                    if found is None:
                        found = decided[line] = _decode_line(line, pages)
                    lines.append(found)
                text = "\n".join(lines)
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


def utf8_env(env: Mapping[str, str]) -> dict[str, str]:
    """``env`` with ``PYTHONIOENCODING=utf-8``, so a child Python writes UTF-8 to a pipe.

    ``env`` is the child's environment, chosen by the caller: for anything
    the person's project runs, ``secrets_store.child_env()``, never Lumi's
    own, which holds provider keys (so there is no default). Without the
    setting a child Python writes the ANSI code page (cp1252). Only its
    standard streams change; ``PYTHONUTF8=1`` would also make ``open()`` read
    and write UTF-8 by default, so a person's script reading a cp1252 CSV
    would fail, or write other bytes, only when Lumi ran it. A
    ``PYTHONIOENCODING`` or ``PYTHONUTF8`` the person set is kept: either
    says how they want their Python to write, and decode_output reads it.
    """
    environment = dict(env)
    if not any(key.upper() in ("PYTHONIOENCODING", "PYTHONUTF8") for key in environment):
        environment["PYTHONIOENCODING"] = "utf-8"
    return environment


# cmd.exe refuses a command line longer than 8,191 characters, counting its
# own path, "/c" and quotes, so ``shell=True`` runs commands of up to 8,158
# characters here. Lumi refuses longer ones itself, in cmd.exe's words and
# with its exit status 1, before starting anything (cmd_too_long).
CMD_COMMAND_LIMIT = 8150
CMD_TOO_LONG = "The command line is too long."


def cmd_too_long(command: str) -> bool:
    """Whether a shell command is longer than cmd.exe takes (Windows only: bash takes far longer ones)."""
    return sys.platform == "win32" and len(command) > CMD_COMMAND_LIMIT


def _end_tree(process: subprocess.Popen, job) -> None:
    """End ``process`` and everything it started: its job on Windows, its process group elsewhere."""
    if job is not None:
        terminate_windows_job(job)
    elif sys.platform != "win32":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass  # the group is gone already
    elif process.poll() is None:
        process.kill()


def run_command(args, *, shell: bool = False, timeout: float | None = None, input: bytes | None = None,
                cwd: str | os.PathLike | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """``subprocess.run(args, capture_output=True)`` whose timeout ends everything the command started.

    ``subprocess.run`` kills only its own child when the timeout passes:
    cmd.exe or bash, not the programs they started, which keep the output
    pipes open, so it went on waiting for them. Here the child starts inside
    a kill-on-close job on Windows (popen_in_kill_job) or its own process
    group elsewhere: a timeout ends the whole tree and raises
    ``subprocess.TimeoutExpired`` with the output so far, and whatever the
    command leaves running ends when it does. Output is bytes (decode it with
    decode_output); stdin is empty unless ``input`` is given. A shell command
    longer than cmd.exe takes gets cmd.exe's own answer, exit status 1 and
    CMD_TOO_LONG, and nothing starts. A program named without a path is the
    installed one, never one in the working folder or ``cwd``
    (lumi/executables.py; ``ProgramNotFound`` when there's none); a shell is
    the person's own (``shell=True``: ``COMSPEC``, or ``/bin/sh``).
    """
    if shell and isinstance(args, str) and cmd_too_long(args):
        return subprocess.CompletedProcess(args, 1, b"", (CMD_TOO_LONG + "\r\n").encode("ascii"))
    if not shell and isinstance(args, (list, tuple)) and args:
        from .executables import is_absolute, program

        if not is_absolute(os.fspath(args[0])):
            args = [program(args[0], exclude=[cwd] if cwd else ()), *args[1:]]
    kwargs = {"shell": shell, "cwd": cwd, "env": env, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
              "stdin": subprocess.DEVNULL if input is None else subprocess.PIPE,
              **background_process_kwargs(new_process_group=sys.platform != "win32")}
    if sys.platform == "win32":
        process, job = popen_in_kill_job(args, **kwargs)
    else:
        process, job = subprocess.Popen(args, **kwargs), None
    try:
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            _end_tree(process, job)
            stdout, stderr = process.communicate()
            raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from None
        except BaseException:
            _end_tree(process, job)
            process.wait()
            raise
        if job is None:
            _end_tree(process, job)  # what it left in its group; on Windows closing the job does that
    finally:
        close_windows_job(job)
    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)

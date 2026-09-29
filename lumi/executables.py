"""Where Lumi's own programs come from.

Lumi starts other programs for its own work: Git for the status bar and
checkpoints, ripgrep for search, the operating system's tools, editors'
command lines, language and MCP servers. Named by a bare name (``git``),
Windows looks for such a program in the parent process's working folder
before the system folders and PATH: CreateProcess does, cmd.exe does for its
commands, ``os.startfile`` (ShellExecute) does, and so did ``shutil.which``
before Python 3.12. A project is often that folder, and a repository with a
``git.exe`` or ``git.bat`` at its root must never run because Lumi asked Git
something.

Three layers keep Lumi's own launches out of the project:

* ``harden_process``, run when the ``lumi`` package is first imported (so in
  every entry point, before anything starts a process), sets
  ``NoDefaultCurrentDirectoryInExePath``. CreateProcess, cmd.exe and
  ``shutil.which`` then leave the working folder out; ShellExecute doesn't
  honor it.
* The app never makes a project its working folder
  (``leave_working_folder``); each command names its own folder instead.
* Lumi's own launches name an absolute path from here: ``system_program``
  for the operating system's tools, ``find_program``/``program`` for the
  rest, taken only from absolute PATH entries outside the working folder and
  the open project.

Servers Lumi starts for itself (MCP and language servers, provider
extensions, automatic lint and tests, the CLI agents) keep the hardening in
their environment (``secrets_store.server_env``), since their launchers
often run ``node`` or ``python`` by bare name. A trusted project's own tools
(``.venv``, ``node_modules/.bin``) come from ``project_tool``, Lumi's own
Git from ``lumi/safe_git.py``, and a file opens with ``open_path``, which
refuses a file that opening would run (``opens_as_program``).

Commands the model or the person asked for are different by design: the
agent's shell, checks, jobs, previews and hooks run in the project with the
person's own environment (``person_environment``), as in their own terminal.
``tests/test_launch_scan.py`` fails when a launch whose program doesn't come
from here appears anywhere else.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import sys
import threading
from typing import Iterable, Mapping, Sequence

logger = logging.getLogger(__name__)

NO_CURRENT_FOLDER = "NoDefaultCurrentDirectoryInExePath"
# The person's own NO_CURRENT_FOLDER, for the commands they or the model run:
# "unset", or "set:" and its value. Kept in the environment so that Lumi's
# own child processes (workers, `lumi run`), which inherit the hardened one,
# still know what the person had.
PERSON_SETTING = "LUMI_PERSON_EXE_SEARCH"

_WINDOWS = sys.platform == "win32"
# The extensions CreateProcess starts. PATHEXT also lists .vbs, .js, .py and
# more, which need another program, so they never count as programs here.
_WINDOWS_PROGRAMS = (".com", ".exe", ".bat", ".cmd")
_POSIX_SYSTEM_FOLDERS = ("/usr/bin", "/bin", "/usr/sbin", "/sbin")
_URL = re.compile(r"(?i)^https?://[^\s]+$")
_PLAIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")


class ProgramNotFound(FileNotFoundError):
    """A program isn't installed where Lumi may take it from."""


# ── The process ─────────────────────────────────────────────────────────────


def harden_process() -> None:
    """Keep Windows from finding programs in this process's working folder.

    Idempotent. Records the person's own setting first (``PERSON_SETTING``),
    unless a parent Lumi process already did, so ``person_environment`` can
    give it back to the commands they run. Also takes the working folder out
    of this process's DLL search (``SetDllDirectoryW("")``; child processes
    don't inherit it, and extension modules load from their own folders).
    Does nothing elsewhere: POSIX systems search PATH only.
    """
    if not _WINDOWS:
        return
    if PERSON_SETTING not in os.environ:
        original = os.environ.get(NO_CURRENT_FOLDER)  # os.environ ignores case on Windows
        os.environ[PERSON_SETTING] = "unset" if original is None else "set:" + original
    os.environ[NO_CURRENT_FOLDER] = "1"
    try:
        import ctypes

        set_dll_directory = ctypes.windll.kernel32.SetDllDirectoryW  # type: ignore[attr-defined]
        set_dll_directory.argtypes = [ctypes.c_wchar_p]
        set_dll_directory("")
    except (AttributeError, OSError):
        logger.debug("SetDllDirectoryW unavailable", exc_info=True)


def person_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """``env`` (this process's environment by default) as the person's own terminal has it.

    For children that run what the model or the person asked for: the
    agent's shell, checks, jobs, previews, hooks and the CLI agents. A
    command such as ``gradlew build`` in cmd.exe then finds the project's
    script as it would outside Lumi, while Lumi's own lookups stay hardened
    (this process keeps the setting).
    """
    result = dict(os.environ if env is None else env)
    setting = os.environ.get(PERSON_SETTING)
    if setting is None:
        return result
    names = {NO_CURRENT_FOLDER.upper(), PERSON_SETTING.upper()}
    for name in [key for key in result if key.upper() in names]:
        del result[name]
    if setting.startswith("set:"):
        result[NO_CURRENT_FOLDER] = setting[len("set:"):]
    return result


_launch_directory: str | None = None


def leave_working_folder() -> str:
    """Make a folder no repository controls this process's working folder.

    For the app, which works in many projects at once and gives each command
    its folder. Returns the folder Lumi was started in (``launch_directory``).
    Windows still searches the working folder for DLLs and in ShellExecute,
    which ``NoDefaultCurrentDirectoryInExePath`` doesn't cover, and a project
    that is the working folder can't be renamed or deleted while Lumi runs.
    """
    global _launch_directory
    if _launch_directory is None:
        try:
            _launch_directory = os.getcwd()
        except OSError:  # the folder Lumi started in was deleted
            _launch_directory = ""
    try:
        os.chdir(system_folder() if _WINDOWS else "/")
    except OSError:
        logger.warning("Could not leave the working folder", exc_info=True)
    return _launch_directory


class NoProject(ValueError):
    """A command needs the project's folder and none was given."""


def current_project(folder: str | os.PathLike | None) -> str:
    """``folder``, the project a command works in, or the working folder where that is the project.

    ``lumi run`` and the terminal UI work in the folder they were started in.
    The app left its working folder (``leave_working_folder``) for the
    system folder, which is never a project: there a missing folder raises
    ``NoProject`` instead of meaning that.
    """
    if folder:
        return os.fspath(folder)
    if _launch_directory is not None:
        raise NoProject("Open a project folder first.")
    return os.getcwd()


def launch_directory() -> str:
    """The folder Lumi was started in, which a terminal launch may mean as the project."""
    if _launch_directory is not None:
        return _launch_directory
    try:
        return os.getcwd()
    except OSError:
        return ""


# ── The operating system's folders and tools ──────────────────────────────


_folders: dict[str, str] = {}


def _windows_folder_api(function_name: str) -> str:
    if function_name in _folders:
        return _folders[function_name]
    import ctypes
    from ctypes import wintypes

    function = getattr(ctypes.WinDLL("kernel32", use_last_error=True), function_name)
    function.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    function.restype = wintypes.UINT
    size = 260
    for _ in range(4):
        buffer = ctypes.create_unicode_buffer(size)
        length = function(buffer, size)
        if not length:
            raise ctypes.WinError(ctypes.get_last_error())
        if length < size:
            _folders[function_name] = buffer.value
            return buffer.value
        size = length + 1  # too small: the length it needs
    raise OSError(f"{function_name} returned no folder")


def windows_folder() -> str:
    """The Windows folder, as the operating system reports it; never from the environment.

    GetSystemWindowsDirectoryW rather than GetWindowsDirectoryW: under
    Terminal Services the latter can name a per-user copy for programs that
    don't declare themselves aware of it. The two agree everywhere else.
    """
    return _windows_folder_api("GetSystemWindowsDirectoryW")


def system_folder() -> str:
    """The system folder (``C:\\Windows\\System32``), as the operating system reports it."""
    return _windows_folder_api("GetSystemDirectoryW")


def system_program(name: str) -> str:
    """The absolute path of a program the operating system provides.

    Windows: ``explorer`` from the Windows folder, ``powershell`` from Windows
    PowerShell's folder and everything else (``cmd``, ``taskkill``,
    ``schtasks``, ``clip``, ``findstr``…) from the system folder. The path is
    returned whether or not the file exists, so a missing tool fails to
    start instead of being looked for elsewhere. Other systems: the first of
    /usr/bin, /bin, /usr/sbin and /sbin that has it, then ``find_program``;
    raises ``ProgramNotFound`` when none does.
    """
    if not _PLAIN_NAME.fullmatch(name or ""):
        raise ValueError(f"Not a program name: {name!r}")
    if _WINDOWS:
        base = name.lower()
        if base.endswith(".exe"):
            base = base[: -len(".exe")]
        if base == "explorer":
            return os.path.join(windows_folder(), "explorer.exe")
        if base == "powershell":
            return os.path.join(system_folder(), "WindowsPowerShell", "v1.0", "powershell.exe")
        return os.path.join(system_folder(), base + ".exe")
    for folder in _POSIX_SYSTEM_FOLDERS:
        candidate = os.path.join(folder, name)
        if _runnable(candidate):
            return candidate
    return program(name)


# ── Programs on PATH ──────────────────────────────────────────────────────


def _has_separator(value: str) -> bool:
    return "/" in value or (_WINDOWS and "\\" in value)


def is_absolute(path: str) -> bool:
    """Whether ``path`` names one place however the working folder changes.

    On Windows that takes a drive and a root (``C:\\``) or a UNC share:
    ``C:tools`` is relative to that drive's working folder, and ``\\tools``
    to the current drive, whatever ``os.path.isabs`` says about the latter
    in older Pythons.
    """
    path = os.fspath(path)
    if not _WINDOWS:
        return path.startswith("/")
    drive, rest = os.path.splitdrive(path)
    if drive.startswith(("\\\\", "//")):
        return True
    return bool(drive) and rest.startswith(("\\", "/"))


def _key(path: str | os.PathLike) -> str:
    """A folder or file as a comparable real path, or "" when it can't be read."""
    try:
        return os.path.normcase(os.path.realpath(os.fspath(path)))
    except (OSError, ValueError, TypeError):
        return ""


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("\\/") + os.sep)


def _broad(folder: str) -> bool:
    """A folder that holds the person's own installs, not a repository's files.

    A drive or file-system root, the home folder or a folder above it (per-user
    installs live under ``%LOCALAPPDATA%\\Programs``, ``~/.cargo`` and the
    like), or the Windows folder. Excluding one of those would hide Git from
    someone who opened their home folder as a project; nothing is lost, since
    the working folder itself is never searched.
    """
    if os.path.dirname(folder) == folder:
        return True
    try:
        home = _key(Path.home())
    except (OSError, RuntimeError):  # no home folder can be determined
        home = ""
    if home and _inside(home, folder):
        return True
    if _WINDOWS:
        try:
            windows = _key(windows_folder())
        except OSError:
            windows = ""
        if windows and _inside(folder, windows):
            return True
    return False


def _exclusion_roots(exclude: Iterable[str | os.PathLike | None]) -> tuple[str, ...]:
    folders: list[str | os.PathLike] = [folder for folder in exclude if folder]
    try:
        folders.append(os.getcwd())
    except OSError:
        pass
    roots: list[str] = []
    for folder in folders:
        key = _key(folder)
        if key and key not in roots and not _broad(key):
            roots.append(key)
    return tuple(roots)


def _path_entries(raw: str | None = None) -> list[str]:
    if raw is None:
        raw = os.environ.get("PATH")
    if raw is None:
        raw = os.defpath
    entries = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip('"')
        # Empty, "." and other relative entries mean the working folder.
        if entry and is_absolute(entry):
            entries.append(entry)
    return entries


def _names(name: str, scripts: bool) -> list[str]:
    """The file names ``name`` may be on this system, in the order to try them."""
    if not _WINDOWS:
        return [name]
    allowed = _WINDOWS_PROGRAMS if scripts else (".exe",)
    extension = os.path.splitext(name)[1].lower()
    if extension in _WINDOWS_PROGRAMS:
        return [name] if extension in allowed else []
    order = [part.strip().lower() for part in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";")]
    extensions = [part for part in order if part in allowed]
    extensions += [part for part in allowed if part not in extensions]
    return [name + part for part in extensions]


def _runnable(path: str) -> bool:
    return os.path.isfile(path) and (_WINDOWS or os.access(path, os.X_OK))


_cache: dict[tuple, str] = {}
_cache_lock = threading.Lock()


def find_program(name: str | os.PathLike, *, exclude: Iterable[str | os.PathLike | None] = (),
                 scripts: bool = False, path: str | None = None) -> str | None:
    """The absolute path of program ``name``, or None when Lumi may not take it from anywhere.

    A bare name (``git``) is looked for on PATH. An absolute path is used as
    given when the file exists. A relative path is refused: it would be
    relative to the working folder.

    PATH's empty, ``.`` and other relative entries are skipped, and so is
    any entry inside the working folder or inside ``exclude`` (the open
    project), apart from folders that hold the person's own installs (see
    ``_broad``). On Windows a name without an extension is ``name.exe``, as
    CreateProcess takes it. ``scripts`` also allows ``.com``, ``.bat`` and
    ``.cmd``, in PATHEXT's order, for command lines installed as batch files
    (``code``, ``npx``, ``az``): cmd.exe parses their arguments, so pass only
    arguments Lumi controls or the person configured. ``path`` replaces the
    PATH variable's value.
    """
    try:
        name = os.fspath(name) if name else ""
    except TypeError:
        return None
    if not name or "\0" in name:
        return None
    if is_absolute(name):
        return name if os.path.isfile(name) else None
    if _has_separator(name) or (_WINDOWS and ":" in name):
        return None
    roots = _exclusion_roots(exclude)
    search = os.environ.get("PATH") if path is None else path
    key = (name, scripts, search, os.environ.get("PATHEXT"), roots)
    with _cache_lock:
        cached = _cache.get(key)
    if cached and os.path.isfile(cached):
        return cached
    candidates = _names(name, scripts)
    for folder in _path_entries(search):
        folder_key = _key(folder)
        if not folder_key or any(_inside(folder_key, root) for root in roots):
            continue
        for candidate_name in candidates:
            candidate = os.path.join(folder, candidate_name)
            if not _runnable(candidate):
                continue
            real = _key(candidate)  # a link into the project doesn't count either
            if not real or any(_inside(real, root) for root in roots):
                continue
            with _cache_lock:
                if len(_cache) > 256:
                    _cache.clear()
                _cache[key] = candidate
            return candidate
    return None


def program(name: str | os.PathLike, *, exclude: Iterable[str | os.PathLike | None] = (),
            scripts: bool = False) -> str:
    """``find_program``, raising ``ProgramNotFound`` when there's none."""
    found = find_program(name, exclude=exclude, scripts=scripts)
    if not found:
        raise ProgramNotFound(f"{os.fspath(name)} isn't installed, or isn't on PATH.")
    return found


def configured_program(command: str, *, folder: str | os.PathLike | None = None,
                       scripts: bool = False) -> str | None:
    """A program the person configured (a server, a test command) as an absolute path, or None.

    An absolute path is used as given, and a relative one is relative to
    ``folder`` (the project the command runs in): naming a path is the
    person's choice. A bare name comes from ``find_program``, never from
    ``folder`` or the working folder.
    """
    command = str(command or "").strip()
    if not command or "\0" in command:
        return None
    if is_absolute(command):
        return _with_exe(command)
    if _WINDOWS and ":" in command:
        return None  # relative to a drive's working folder (C:tools\x.exe)
    if _has_separator(command):
        return _with_exe(os.path.normpath(os.path.join(os.fspath(folder), command))) if folder else None
    return find_program(command, exclude=[folder], scripts=scripts)


# A project's own environments, where its tools (ruff, pytest, eslint, a
# language server) are installed for it.
_PROJECT_TOOL_FOLDERS = ((".venv", "Scripts" if _WINDOWS else "bin"), ("venv", "Scripts" if _WINDOWS else "bin"),
                         ("node_modules", ".bin"))


def _project_copy(name: str, project: str | os.PathLike | None, scripts: bool) -> tuple[str, str]:
    """``name`` in one of ``project``'s own environments: (path, "the project's .venv"), or ("", "")."""
    if not project or not _PLAIN_NAME.fullmatch(name):
        return "", ""
    root = os.path.abspath(os.fspath(project))
    for folder, bin_folder in _PROJECT_TOOL_FOLDERS:
        base = os.path.join(root, folder, bin_folder)
        for candidate_name in _names(name, scripts or folder == "node_modules"):
            candidate = os.path.join(base, candidate_name)
            if _runnable(candidate):
                return candidate, f"the project's {folder}"
    return "", ""


def project_tool(name: str, project: str | os.PathLike | None, *, trusted: bool,
                 scripts: bool = False) -> tuple[str | None, str, str]:
    """A tool Lumi runs on ``project`` (automatic lint and tests, a language server).

    Returns (path or None, where it came from, a notice). A trusted project's
    own environments come first: ``.venv``, ``venv`` and ``node_modules/.bin``
    ("from the project's .venv"). Otherwise, and when they don't have it, only
    PATH outside the project ("from PATH"). An untrusted project that has its
    own copy gets a notice saying to trust it. Each call looks again with
    the trust decision it is given; nothing is cached across decisions. Never
    for Git, ripgrep, the system's tools or the CLI agents, which always come
    from ``find_program`` or ``system_program``.
    """
    local, where = _project_copy(name, project, scripts)
    if local and trusted:
        return local, f"from {where}", ""
    found = find_program(name, exclude=[project], scripts=scripts)
    notice = f"{where[0].upper()}{where[1:]} has its own {name}; trust the project to use it." if local else ""
    return found, ("from PATH" if found else ""), notice


# cmd.exe reads these in a batch file's command line even between quotes
# (% and !) or after a stray quote: an argument with one could run a command.
_CMD_SPECIAL = frozenset('&|<>^%!\r\n')


def batch_argument_problem(program_path: str, args: Sequence[str]) -> str:
    """Why ``args`` can't go to ``program_path``, or "".

    A ``.bat`` or ``.cmd`` program (an npm command such as ``codex.cmd``)
    starts through cmd.exe, which parses its command line again: an argument
    with ``&``, ``|``, ``<``, ``>``, ``^``, ``%``, ``!`` or a line break could
    start another command. Other programs get their arguments as they are.
    """
    if not _WINDOWS or os.path.splitext(str(program_path))[1].lower() not in (".bat", ".cmd"):
        return ""
    for arg in args:
        found = sorted(set(str(arg)) & _CMD_SPECIAL)
        if found:
            shown = " ".join(repr(char) if char.isprintable() else "a line break" for char in found)
            return (f"{os.path.basename(program_path)} is a batch file, and cmd.exe would read {shown} in an "
                    f"argument ({str(arg)[:60]!r}) as part of a command. Use a name or path without those characters.")
    return ""


def _with_exe(path: str) -> str:
    """``path``, or ``path.exe`` where CreateProcess would have added the extension."""
    if (_WINDOWS and not os.path.splitext(path)[1] and not os.path.isfile(path)
            and os.path.isfile(path + ".exe")):
        return path + ".exe"
    return path


def project_command(argv: Sequence[str], cwd: str | os.PathLike) -> list[str]:
    """``argv`` for a command the model or the person asked to run in ``cwd``.

    On Windows, CreateProcess resolves a relative program, and looks for a
    bare one, in this process's working folder, never in ``cwd``. Lumi's
    working folder used to be the project, so a job such as
    ``gradlew.bat bootRun`` or ``.\\build\\app.exe`` ran from there; it runs
    from ``cwd`` now, as it would in the person's terminal. Anything else is
    left to CreateProcess (without the working folder). POSIX systems
    already look relative to ``cwd``.
    """
    argv = list(argv)
    if not _WINDOWS or not argv:
        return argv
    first = str(argv[0])
    if is_absolute(first):
        return argv
    folder = os.fspath(cwd)
    if _has_separator(first):
        return [os.path.normpath(os.path.join(folder, first)), *argv[1:]]
    if ":" in first:
        return argv
    local = os.path.join(folder, first if os.path.splitext(first)[1] else first + ".exe")
    return [local, *argv[1:]] if os.path.isfile(local) else argv


# ── Opening things for the person ─────────────────────────────────────────


def _background() -> dict:
    import subprocess

    from .processes import background_process_kwargs

    return {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
            **background_process_kwargs()}


def open_url(url: str) -> bool:
    """Open a web address in the default browser. False when nothing here can open one.

    Instead of ``webbrowser``, whose POSIX openers are found by name. On
    Windows ShellExecute hands an http(s) address to its protocol's handler
    without looking for a program.
    """
    import subprocess

    if not _URL.match(str(url or "")):
        raise ValueError("Only http and https addresses open in the browser.")
    try:
        if _WINDOWS:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        opener = system_program("open") if sys.platform == "darwin" else find_program("xdg-open")
        if not opener:
            return False
        subprocess.Popen([opener, url], **_background())
        return True
    except OSError:  # no program handles web addresses here
        return False


# Files the default "open" action runs rather than shows. On Windows PATHEXT's
# extensions count too, and so does any type whose registered command runs
# the file itself or hands it to a script host (_association_runs).
_WINDOWS_RUNS = {
    ".exe": "a program", ".com": "a program", ".scr": "a screen saver program", ".pif": "a program shortcut",
    ".bat": "a batch file", ".cmd": "a batch file", ".cpl": "a Control Panel item", ".msc": "a management console",
    ".msi": "an installer", ".msp": "an installer patch", ".mst": "an installer transform",
    ".lnk": "a shortcut", ".url": "an Internet shortcut", ".website": "a pinned site shortcut",
    ".scf": "a shell command file", ".settingcontent-ms": "a settings shortcut", ".library-ms": "a library",
    ".searchconnector-ms": "a search connector", ".application": "a ClickOnce application",
    ".appref-ms": "a ClickOnce shortcut", ".appx": "an app package", ".appxbundle": "an app package",
    ".msix": "an app package", ".msixbundle": "an app package", ".diagcab": "a troubleshooter",
    ".js": "a script", ".jse": "a script", ".vbs": "a VBScript", ".vbe": "a VBScript", ".wsf": "a script",
    ".wsh": "a script host setting", ".ws": "a script", ".sct": "a scriptlet", ".wsc": "a script component",
    ".hta": "an HTML application", ".chm": "a compiled help file", ".hlp": "a help file",
    ".ps1": "a PowerShell script", ".psm1": "a PowerShell module", ".psd1": "a PowerShell data file",
    ".py": "a Python script", ".pyw": "a Python script", ".pyz": "a Python program", ".pyzw": "a Python program",
    ".jar": "a Java program", ".reg": "a registry file", ".inf": "a setup information file",
    ".gadget": "a desktop gadget", ".xbap": "a browser application",
}
_MAC_RUNS = {
    ".app": "an application", ".command": "a Terminal script", ".terminal": "a Terminal setting",
    ".tool": "a Terminal script", ".pkg": "an installer package", ".mpkg": "an installer package",
    ".workflow": "an Automator workflow", ".action": "an Automator action", ".prefpane": "a settings pane",
    ".jar": "a Java program", ".fileloc": "a file shortcut", ".webloc": "a web shortcut",
}
_LINUX_RUNS = {
    ".desktop": "a desktop launcher", ".appimage": "an AppImage program", ".run": "an installer",
    ".sh": "a shell script", ".jar": "a Java program",
}
# Programs whose job is to run the file they are given.
_SCRIPT_HOSTS = frozenset({
    "wscript.exe", "cscript.exe", "mshta.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "py.exe", "pyw.exe",
    "python.exe", "pythonw.exe", "java.exe", "javaw.exe", "node.exe", "regedit.exe", "msiexec.exe",
    "rundll32.exe", "regsvr32.exe", "mmc.exe", "control.exe", "hh.exe", "bash.exe", "wsl.exe", "sh.exe",
    "perl.exe", "ruby.exe", "php.exe", "deno.exe", "bun.exe", "installutil.exe", "appinstaller.exe",
})


class OpensAsProgram(ValueError):
    """Opening this file would run it; show it in its folder instead."""

    def __init__(self, path: str, kind: str):
        self.path, self.kind = path, kind
        super().__init__(f"{os.path.basename(path)} is {kind}: opening it would run it.")


def _association_runs(extension: str) -> bool:
    """Whether Windows' command for opening ``extension`` runs the file (``"%1"``) or gives it to a script host."""
    import ctypes

    try:
        size = ctypes.c_ulong(0)
        query = ctypes.windll.shlwapi.AssocQueryStringW  # type: ignore[attr-defined]
        # ASSOCF_NOTRUNCATE, ASSOCSTR_COMMAND; the default verb, as ShellExecute uses it.
        if query(0x20, 1, extension, None, None, ctypes.byref(size)) not in (0, 1) or not size.value:
            return False
        buffer = ctypes.create_unicode_buffer(size.value + 1)
        if query(0x20, 1, extension, None, buffer, ctypes.byref(size)) != 0:
            return False
    except (AttributeError, OSError):
        return False
    command = buffer.value.strip()
    if command.startswith('"'):
        first = command[1:].split('"', 1)[0]
    else:
        first = command.split(" ", 1)[0]
    first = os.path.expandvars(first).strip()
    return first in ("%1", "%L") or os.path.basename(first).lower() in _SCRIPT_HOSTS


def opens_as_program(path: str | os.PathLike) -> str:
    """What ``path`` is ("a batch file") when opening it would run it; "" for a document or folder."""
    target = os.fspath(path)
    extension = os.path.splitext(target.rstrip("\\/"))[1].lower()
    if _WINDOWS:
        if os.path.isdir(target):
            return ""
        if extension in _WINDOWS_RUNS:
            return _WINDOWS_RUNS[extension]
        pathext = {part.strip().lower() for part in (os.environ.get("PATHEXT") or "").split(";") if part.strip()}
        if extension in pathext or (extension and _association_runs(extension)):
            return f"a {extension} file, which runs when opened"
        return ""
    kinds = _MAC_RUNS if sys.platform == "darwin" else _LINUX_RUNS
    if extension in kinds:
        return kinds[extension]  # an .app is a folder
    if os.path.isfile(target) and os.access(target, os.X_OK):
        return "an executable file"  # the Finder runs one in Terminal
    return ""


def open_path(path: str | os.PathLike) -> None:
    """Open an existing document or folder with its default program; ``path`` must be absolute.

    Raises ``OpensAsProgram`` for a file that opening would run (a program,
    a script, a shortcut, an installer): show it in its folder instead
    (``show_in_folder``).
    """
    import subprocess

    target = os.fspath(path)
    if not is_absolute(target):
        raise ValueError("Give the full path of the file to open.")
    kind = opens_as_program(target)
    if kind:
        raise OpensAsProgram(target, kind)
    if _WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]  # an absolute path: nothing is looked for
        return
    opener = system_program("open") if sys.platform == "darwin" else program("xdg-open")
    subprocess.Popen([opener, target], **_background())


def show_in_folder_command(path: str | os.PathLike) -> list[str]:
    """The program and arguments that show ``path`` selected in the file manager."""
    target = os.fspath(path)
    if not is_absolute(target):
        raise ValueError("Give the full path of the file to show.")
    if _WINDOWS:
        return [system_program("explorer"), "/select,", target]
    if sys.platform == "darwin":
        return [system_program("open"), "-R", target]
    return [program("xdg-open"), os.path.dirname(target) or target]


def show_in_folder(path: str | os.PathLike) -> None:
    """Show ``path`` in Explorer, the Finder or the file manager, selected where possible."""
    import subprocess

    subprocess.Popen(show_in_folder_command(path), **_background())

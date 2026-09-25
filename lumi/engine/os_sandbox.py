"""Keep the agent's shell commands from writing outside the project (macOS and Linux).

With ``security.shell_sandbox`` set to ``"project"`` (Settings > Privacy &
security, or an organization's policy), each command the agent runs through
``bash`` or ``check_run``, and each managed job or preview, runs in an
operating-system sandbox that can write only to:

* the project folder (and any other folder the session's path sandbox allows);
* the temporary folders (the system's and this user's);
* the terminal and null devices a shell needs.

Git's own folder (``.git``) stays read-only inside those. Hooks and settings
written there run later outside the sandbox, when anyone commits; the
agent's Git tools commit instead.

Reading files, running programs and the network work as before. The path
checks, file exclusions, execution rules and guardrails still apply.

* **macOS:** ``sandbox-exec`` with a Seatbelt profile.
* **Linux:** ``bwrap`` (bubblewrap), which needs unprivileged user
  namespaces: the file system is mounted read-only and the allowed folders
  writable.
* **Windows:** not available yet. A write sandbox there needs an
  AppContainer or folder permission changes, which break common developer
  tools.

When the setting is on but no sandbox can run here, commands are refused
rather than run unprotected: an organization that requires the sandbox
relies on that. Tools that cache in the home folder (npm, pip, cargo) can't
write those caches inside the sandbox; point them at the project or a
temporary folder.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from typing import Any, Iterable

MODES = ("off", "project")

_lock = threading.Lock()  # the mode and whether the check has started
_probe_lock = threading.Lock()  # one check at a time; it can take seconds
_mode = "off"
_availability: Availability | None = None
_probe_started = False


class SandboxUnavailable(ValueError):
    """The sandbox is required, and can't run on this computer."""


@dataclass(frozen=True)
class Availability:
    available: bool
    kind: str = ""  # "seatbelt" or "bubblewrap"
    executable: str = ""
    reason: str = ""


def configure(settings: Any) -> str:
    """Read ``security.shell_sandbox`` (a policy can lock it); returns the mode.

    Also starts the check for whether a sandbox can run here, in the
    background, so Settings can say before anyone turns it on.
    """
    global _mode
    value = settings.get("security", "shell_sandbox", "off") if settings is not None else "off"
    with _lock:
        _mode = value if value in MODES else "off"
        current = _mode
    _probe_soon()
    return current


def mode() -> str:
    return _mode


def set_for_tests(value: str, availability_: Availability | None = None) -> None:
    """Set the mode and what the probe found, without probing."""
    global _mode, _availability, _probe_started
    with _lock:
        _mode = value
        _availability = availability_
        _probe_started = availability_ is not None


def availability() -> Availability:
    """Whether a sandbox can run on this computer, checked once by starting one."""
    global _availability
    with _probe_lock:
        if _availability is None:
            _availability = _probe()
        return _availability


def _probe_soon() -> None:
    global _probe_started
    with _lock:
        if _probe_started or _availability is not None:
            return
        _probe_started = True
    threading.Thread(target=availability, name="lumi-sandbox-probe", daemon=True).start()


def _probe() -> Availability:
    try:
        if sys.platform == "darwin":
            exe = shutil.which("sandbox-exec") or ("/usr/bin/sandbox-exec"
                                                   if os.path.exists("/usr/bin/sandbox-exec") else "")
            if not exe:
                return Availability(False, reason="sandbox-exec isn't on this Mac.")
            result = subprocess.run([exe, "-p", seatbelt_profile([]), "/usr/bin/true"],
                                    capture_output=True, timeout=10)
            if result.returncode == 0:
                return Availability(True, "seatbelt", exe)
            return Availability(False, reason="sandbox-exec couldn't start a sandbox.")
        if sys.platform.startswith("linux"):
            exe = shutil.which("bwrap")
            if not exe:
                return Availability(False, reason="Install bubblewrap (the bwrap command) to use the shell sandbox.")
            result = subprocess.run([exe, *_bwrap_base(), "/bin/true"], capture_output=True, timeout=10)
            if result.returncode == 0:
                return Availability(True, "bubblewrap", exe)
            detail = (result.stderr.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
            return Availability(False, reason=("bubblewrap can't create a sandbox here, often because "
                                               "unprivileged user namespaces are turned off. " + detail).strip())
        return Availability(False, reason="Lumi has no shell sandbox on Windows yet.")
    except (OSError, subprocess.SubprocessError) as exc:
        return Availability(False, reason=f"Checking for a sandbox failed: {exc}")


def status() -> dict:
    """What Settings shows. Never waits for the check: ``available`` is None until it's done."""
    info = _availability
    if info is None:
        _probe_soon()
        return {"mode": mode(), "available": None, "kind": "", "reason": ""}
    return {"mode": mode(), "available": info.available, "kind": info.kind, "reason": info.reason}


def writable_roots(roots: Iterable[str]) -> list[str]:
    """The folders a sandboxed command may write to, as real paths (Seatbelt matches real paths)."""
    candidates = [*roots, tempfile.gettempdir(), os.environ.get("TMPDIR", "")]
    if sys.platform == "darwin":
        # Each user's temporary and cache folders are under /private/var/folders.
        candidates += ["/private/tmp", "/private/var/folders"]
    else:
        candidates += ["/tmp", "/var/tmp"]
    result: list[str] = []
    for root in candidates:
        if not root:
            continue
        real = os.path.realpath(root)
        if os.path.isdir(real) and real not in result:
            result.append(real)
    return result


def _read_only_inside(root: str) -> list[str]:
    git = os.path.join(root, ".git")
    return [os.path.realpath(git)] if os.path.exists(git) else []


def _seatbelt_string(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(roots: list[str]) -> str:
    """A profile that allows everything except writing outside ``roots``, or to a .git in them.

    The .git exceptions cover every allowed folder, not just the one they
    are in: a project inside the temporary folder is also under that root.
    """
    places = [f"(subpath {_seatbelt_string(root)})" for root in roots]
    places += ['(literal "/dev/null")', '(literal "/dev/zero")', '(literal "/dev/dtracehelper")',
               '(regex #"^/dev/tty")', '(regex #"^/dev/fd/")', '(literal "/dev/ptmx")']
    allowed = "(require-any\n    " + "\n    ".join(places) + ")"
    protected = [path for root in roots for path in _read_only_inside(root)]
    if protected:
        exceptions = "".join(f"\n  (require-not (subpath {_seatbelt_string(path)}))" for path in protected)
        allowed = f"(require-all\n  {allowed}{exceptions})"
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f"(allow file-write*\n  {allowed})\n"
    )


def _bwrap_base() -> list[str]:
    return ["--die-with-parent", "--unshare-pid", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev",
            "--proc", "/proc"]


def _prefix(roots: Iterable[str], cwd: str) -> list[str]:
    writable = writable_roots(roots)
    info = availability()
    if info.kind == "seatbelt":
        return [info.executable or "sandbox-exec", "-p", seatbelt_profile(writable)]
    args = [info.executable or "bwrap", *_bwrap_base()]
    for root in writable:
        args += ["--bind", root, root]
    for root in writable:
        for protected in _read_only_inside(root):
            args += ["--ro-bind", protected, protected]
    return [*args, "--chdir", cwd]


def wrap_shell(command: str, *, roots: Iterable[str], cwd: str) -> list[str]:
    """The program and arguments that run ``command`` with ``/bin/sh -c`` in the sandbox."""
    return [*_prefix(roots, cwd), "/bin/sh", "-c", command]


def wrap_argv(argv: list[str], *, roots: Iterable[str], cwd: str) -> list[str]:
    """The same for a program and its arguments."""
    return [*_prefix(roots, cwd), *argv]


def _require() -> None:
    info = availability()
    if not info.available:
        raise SandboxUnavailable(
            "The shell sandbox is on (Settings > Privacy & security, or your organization's "
            f"policy), but it can't run on this computer: {info.reason}"
        )


def prepare_shell(command: str, *, roots: Iterable[str], cwd: str) -> list[str] | None:
    """The sandboxed command line, or None to run ``command`` as before.

    Raises SandboxUnavailable when the sandbox is on and can't run here.
    """
    if mode() != "project":
        return None
    _require()
    return wrap_shell(command, roots=roots, cwd=cwd)


def prepare_argv(argv: list[str], *, roots: Iterable[str], cwd: str) -> list[str]:
    """``argv`` itself, or sandboxed when the sandbox is on; raises like prepare_shell."""
    if mode() != "project":
        return argv
    _require()
    return wrap_argv(argv, roots=roots, cwd=cwd)

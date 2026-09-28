"""Entry point for Lumi."""
import os
import sys

# NOTE: absolute imports (not `from . import`). When PyInstaller bundles
# __main__.py as the entry point, relative imports fail because the module
# runs as `__main__`, not `lumi.__main__`. Absolute imports work in both
# `python -m lumi` and the frozen exe.
from lumi import __version__
from lumi.paths import migrate_legacy_home, state_home

# A private worker must dispatch before the state migration below, windowless
# stream logging, updater or UI initialization. Its inherited pipes carry the
# bounded host protocol, and it never moves or opens the user's state folder.
if __name__ == "__main__" and len(sys.argv) == 2 and sys.argv[1] == "--swarm-worker":
    from lumi.engine.swarming.worker_child import main as swarm_worker_main
    raise SystemExit(swarm_worker_main())
if __name__ == "__main__" and len(sys.argv) == 2 and sys.argv[1] == "--swarm-effect":
    from lumi.engine.swarming.effect_child import main as swarm_effect_main
    raise SystemExit(swarm_effect_main())

# Move pre-rebrand state (~/.resonant -> ~/.lumi) before anything below opens
# a file in it; on Windows an open log makes the rename fail.
migrate_legacy_home()


def _redacted_startup_arguments(arguments):
    """Keep operator configuration paths out of the windowless startup log."""
    result, redact_next = [], False
    for argument in arguments:
        if redact_next:
            result.append("<protected-managed-config>")
            redact_next = False
        elif argument == "--swarm-managed-config":
            result.append(argument)
            redact_next = True
        elif argument.startswith("--swarm-managed-config="):
            result.append("--swarm-managed-config=<protected-managed-config>")
        else:
            result.append(argument)
    return result


def _managed_startup_arguments(arguments):
    """Extract the single explicit host-only option before GUI argument parsing."""
    remaining, path = [], None
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--swarm-managed-config" or argument.startswith("--swarm-managed-config="):
            if path is not None:
                raise ValueError("Managed configuration must be selected once")
            if "=" in argument:
                path = argument.split("=", 1)[1]
            else:
                index += 1
                if index >= len(arguments):
                    raise ValueError("Managed configuration requires an absolute file")
                path = arguments[index]
            if not path or not os.path.isabs(path):
                raise ValueError("Managed configuration requires an absolute file")
        else:
            remaining.append(argument)
        index += 1
    if path is not None and (len(remaining) < 2 or remaining[1] != "gui"):
        raise ValueError("Managed configuration is supported by the GUI launcher")
    return remaining, path

# Bug #19 + #20 fix — frozen-no-console std-stream redirect to log file.
#
# When PyInstaller's `console=False` bundles run, the OS doesn't attach a
# console to the process, so sys.stdout / sys.stderr / sys.stdin are None.
# Many libraries crash early: uvicorn's ColourizedFormatter calls
# sys.stderr.isatty() at logging-config time → AttributeError before main()
# even runs.
#
# Originally (v0.2.4) we redirected to /dev/null. That fixed the crash but
# made every runtime error invisible — bug #20 ("Internal Server Error" with
# no traceback to debug). v0.2.5+ redirects to a real log file at
#     ~/.lumi/logs/lumi-startup.log
# so uvicorn errors / startup tracebacks / unhandled exceptions land
# somewhere readable. Rotated only by hand for now (single file appends).
#
# Only fires when frozen + at least one stream is None — leaves dev runs
# (`python -m lumi`) untouched so output still hits the terminal. An app that
# LaunchServices opened on macOS (Finder, the Dock, `open`, Sparkle's
# relaunch) gets streams open on /dev/null instead of none, so for such a
# launch, and only for one, those count too: without this a Mac tester's
# startup errors would go nowhere. A command-line run whose output was sent
# to /dev/null on purpose (`lumi run … > /dev/null` from a script) keeps it
# there, so nothing it prints, a model's output included, lands on disk.


def _bundle_identifier(executable: str) -> str:
    """The CFBundleIdentifier of the app bundle ``executable`` runs from (Contents/MacOS/…), or ""."""
    import plistlib

    contents = os.path.dirname(os.path.dirname(os.path.abspath(executable)))
    try:
        with open(os.path.join(contents, "Info.plist"), "rb") as handle:
            value = plistlib.load(handle).get("CFBundleIdentifier")
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _launched_by_launchservices(argv=None, *, platform=None, frozen=None, terminal=None, parent=None,
                                environ=None, bundle_id=None) -> bool:
    """Lumi.app started by LaunchServices: Finder, the Dock, `open`, or Sparkle's relaunch.

    Such a launch is a child of launchd, without a terminal, with
    ``__CFBundleIdentifier`` set to this app's bundle identifier (old macOS
    versions also pass a ``-psn_…`` argument). A run of the same executable
    from a terminal, a script, cron or a launchd job is none of that, and
    keeps the command line's behavior. The keyword arguments stand in for
    this process's own facts in tests.
    """
    if (sys.platform if platform is None else platform) != "darwin":
        return False
    if not (getattr(sys, "frozen", False) if frozen is None else frozen):
        return False
    argv = sys.argv if argv is None else argv
    if any(argument.startswith("-psn_") for argument in argv[1:]):
        return True
    if (os.getppid() if parent is None else parent) != 1:
        return False
    if os.isatty(0) if terminal is None else terminal:
        return False
    launched_as = (os.environ if environ is None else environ).get("__CFBundleIdentifier", "")
    if not launched_as:
        return False
    return launched_as == (_bundle_identifier(sys.executable) if bundle_id is None else bundle_id)


def _discarded(stream, launched_by_launchservices: bool) -> bool:
    """A stream the startup log should replace: missing, or /dev/null in an app LaunchServices opened."""
    if stream is None:
        return True
    if not launched_by_launchservices:
        return False
    try:
        return os.path.samestat(os.fstat(stream.fileno()), os.stat(os.devnull))
    except (OSError, ValueError, AttributeError):
        return False


_LAUNCHED_BY_LAUNCHSERVICES = _launched_by_launchservices()

if getattr(sys, "frozen", False) and (
    _discarded(sys.stdout, _LAUNCHED_BY_LAUNCHSERVICES) or _discarded(sys.stderr, _LAUNCHED_BY_LAUNCHSERVICES)
    or sys.stdin is None
):
    _log_dir = str(state_home() / "logs")
    try:
        os.makedirs(_log_dir, exist_ok=True)
        _log_path = os.path.join(_log_dir, "lumi-startup.log")
        _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
        # Marker so we can find the start of each session in the log.
        _log_file.write(f"\n{'=' * 60}\n=== lumi {_redacted_startup_arguments(sys.argv)} pid={os.getpid()}\n")
        _log_file.flush()
    except OSError:
        # If we can't open the log file (read-only home, weird perms),
        # fall back to NUL — better silently-broken than crashing on stderr.
        _log_file = open(os.devnull, "w", encoding="utf-8")
    if _discarded(sys.stdout, _LAUNCHED_BY_LAUNCHSERVICES):
        sys.stdout = _log_file
    if _discarded(sys.stderr, _LAUNCHED_BY_LAUNCHSERVICES):
        sys.stderr = _log_file
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def _opened_as_mac_app(argv=None, **facts) -> bool:
    """Lumi.app opened from Finder, the Dock, `open` or Sparkle's relaunch: the GUI, not the terminal UI.

    Such a launch passes no arguments of its own. The same executable run
    from a terminal or a script without arguments is the terminal UI, as
    ``lumi.exe`` is. ``facts`` stand in for this process's own in tests
    (see ``_launched_by_launchservices``).
    """
    if argv is None and not facts:
        launched, argv = _LAUNCHED_BY_LAUNCHSERVICES, sys.argv
    else:
        argv = sys.argv if argv is None else argv
        launched = _launched_by_launchservices(argv, **facts)
    return launched and not [argument for argument in argv[1:] if not argument.startswith("-psn_")]


def main():
    if _opened_as_mac_app():
        sys.argv = [sys.argv[0], "gui"]

    # Surface --version / -V before any other dispatch so it works without
    # loading the heavier TUI / GUI subsystems.
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"lumi {__version__}")
        return

    # Debug: dump the EdDSA public key baked into the binary. Useful when
    # diagnosing "improperly signed" update errors (verifies the key the
    # binary will check against matches the key used to sign updates).
    if len(sys.argv) > 1 and sys.argv[1] == "--print-pubkey":
        from lumi.updater import APPCAST_URL, EDDSA_PUBLIC_KEY, MACOS_APPCAST_URL
        print(f"EDDSA_PUBLIC_KEY={EDDSA_PUBLIC_KEY}")
        # The stable feed this build reads; macOS has its own (disk images).
        print(f"APPCAST_URL={MACOS_APPCAST_URL if sys.platform == 'darwin' else APPCAST_URL}")
        return

    # Reading the usage records and unattended runs need neither the
    # updater nor a UI.
    if len(sys.argv) > 1 and sys.argv[1] == "usage":
        from lumi.usage import main as usage_main
        raise SystemExit(usage_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        from lumi.headless import main as run_main
        raise SystemExit(run_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "schedule":
        from lumi.schedules import main as schedule_main
        raise SystemExit(schedule_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "editor":
        from lumi.code_editors.cli import main as editor_main
        raise SystemExit(editor_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "updates":
        from lumi.update_channels import main as updates_main
        raise SystemExit(updates_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "license":
        from lumi.license import main as license_main
        raise SystemExit(license_main(sys.argv[2:]))
    # Lumi's terms: whether they're accepted here, their texts, and accepting them (lumi/terms.py).
    if len(sys.argv) > 1 and sys.argv[1] == "terms":
        from lumi.terms import main as terms_main
        raise SystemExit(terms_main(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "extension":
        from lumi.extension_check import main as extension_main
        raise SystemExit(extension_main(sys.argv[2:]))

    # This is an operator startup option, never browser state or a discovered
    # credential. Reject invalid setup before updater/UI startup; do not quietly
    # fall back to a personal run after an explicit managed configuration fails.
    try:
        arguments, managed_path = _managed_startup_arguments(sys.argv)
        if managed_path is not None:
            from lumi.engine.swarming.managed_desktop import ManagedDesktop, load_configuration
            managed_desktop = ManagedDesktop(load_configuration(managed_path))
            from lumi.gui.app import configure_managed_startup
            configure_managed_startup(managed_desktop)
        sys.argv = arguments
    except Exception:
        print("Managed setup failed. Verify the protected configuration and certificate files.", file=sys.stderr)
        raise SystemExit(2) from None

    # Kick off the WinSparkle background updater. No-op on Linux or when the
    # DLL isn't bundled (dev runs from source). Fire-and-forget; WinSparkle
    # owns its own thread and surfaces a native dialog only when a newer
    # version is found in the appcast. On macOS only the app starts Sparkle,
    # here on the main thread: it runs its checks and windows on the app's
    # run loop, which the terminal UI and the chat gateway don't turn.
    if sys.platform != "darwin" or (len(sys.argv) > 1 and sys.argv[1] == "gui"):
        try:
            from lumi.updater import init_updater
            init_updater()
        except Exception:
            # Updater failures must never block app startup.
            import logging
            logging.getLogger(__name__).exception("Updater init failed (non-fatal)")

    # Check for GUI subcommand before parsing full args
    if len(sys.argv) > 1 and sys.argv[1] == "gui":
        # Strip "gui" from argv so the GUI's argparse works cleanly
        sys.argv = [sys.argv[0]] + sys.argv[2:]
        # Absolute imports for PyInstaller compat — see comment at top of file.
        from lumi.gui.server import main as gui_main
        gui_main()
    elif len(sys.argv) > 1 and sys.argv[1] == "gateway":
        from lumi.gateway.cli import main as gateway_main
        gateway_main(sys.argv[2:])
    else:
        from lumi.tui import main as tui_main
        tui_main()


if __name__ == "__main__":
    main()

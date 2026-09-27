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
# (`python -m lumi`) untouched so output still hits the terminal.
if getattr(sys, "frozen", False) and (
    sys.stdout is None or sys.stderr is None or sys.stdin is None
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
    # Every launch appends here (the app, a terminal UI, `lumi run`...):
    # tag each line with this process's role and id (lumi/startup_log.py).
    from lumi.startup_log import TaggedLog, process_role

    _log_file = TaggedLog(_log_file, process_role(sys.argv))
    if sys.stdout is None:
        sys.stdout = _log_file
    if sys.stderr is None:
        sys.stderr = _log_file
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")


def main():
    # Surface --version / -V before any other dispatch so it works without
    # loading the heavier TUI / GUI subsystems.
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"lumi {__version__}")
        return

    # Debug: dump the EdDSA public key baked into the binary. Useful when
    # diagnosing "improperly signed" update errors (verifies the key the
    # binary will check against matches the key used to sign updates).
    if len(sys.argv) > 1 and sys.argv[1] == "--print-pubkey":
        from lumi.updater import EDDSA_PUBLIC_KEY, APPCAST_URL
        print(f"EDDSA_PUBLIC_KEY={EDDSA_PUBLIC_KEY}")
        print(f"APPCAST_URL={APPCAST_URL}")
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

    # Kick off the WinSparkle background updater. No-op on non-Windows or
    # when the DLL isn't bundled (dev runs from source). Fire-and-forget;
    # WinSparkle owns its own thread and surfaces a native dialog only when
    # a newer version is found in the appcast.
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

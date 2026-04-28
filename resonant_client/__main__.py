"""Entry point for the Resonant Client."""
import os
import sys

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
#     ~/.resonant/logs/resonant-startup.log
# so uvicorn errors / startup tracebacks / unhandled exceptions land
# somewhere readable. Rotated only by hand for now (single file appends).
#
# Only fires when frozen + at least one stream is None — leaves dev runs
# (`python -m resonant_client`) untouched so output still hits the terminal.
if getattr(sys, "frozen", False) and (
    sys.stdout is None or sys.stderr is None or sys.stdin is None
):
    _log_dir = os.path.join(os.path.expanduser("~"), ".resonant", "logs")
    try:
        os.makedirs(_log_dir, exist_ok=True)
        _log_path = os.path.join(_log_dir, "resonant-startup.log")
        _log_file = open(_log_path, "a", encoding="utf-8", buffering=1)
        # Marker so we can find the start of each session in the log.
        _log_file.write(f"\n{'=' * 60}\n=== resonant {sys.argv} pid={os.getpid()}\n")
        _log_file.flush()
    except OSError:
        # If we can't open the log file (read-only home, weird perms),
        # fall back to NUL — better silently-broken than crashing on stderr.
        _log_file = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = _log_file
    if sys.stderr is None:
        sys.stderr = _log_file
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")

# NOTE: absolute import (not `from . import`). When PyInstaller bundles
# __main__.py as the entry point, relative imports fail because the module
# runs as `__main__`, not `resonant_client.__main__`. Absolute import works
# in both `python -m resonant_client` and the frozen exe.
from resonant_client import __version__


def main():
    # Surface --version / -V before any other dispatch so it works without
    # loading the heavier TUI / GUI subsystems.
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print(f"resonant-client {__version__}")
        return

    # Debug: dump the EdDSA public key baked into the binary. Useful when
    # diagnosing "improperly signed" update errors (verifies the key the
    # binary will check against matches the key used to sign updates).
    if len(sys.argv) > 1 and sys.argv[1] == "--print-pubkey":
        from resonant_client.updater import EDDSA_PUBLIC_KEY, APPCAST_URL
        print(f"EDDSA_PUBLIC_KEY={EDDSA_PUBLIC_KEY}")
        print(f"APPCAST_URL={APPCAST_URL}")
        return

    # Kick off the WinSparkle background updater. No-op on non-Windows or
    # when the DLL isn't bundled (dev runs from source). Fire-and-forget;
    # WinSparkle owns its own thread and surfaces a native dialog only when
    # a newer version is found in the appcast.
    try:
        from resonant_client.updater import init_updater
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
        from resonant_client.gui.server import main as gui_main
        gui_main()
    else:
        from resonant_client.tui import main as tui_main
        tui_main()


if __name__ == "__main__":
    main()
